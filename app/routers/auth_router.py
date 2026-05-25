from datetime import datetime, timedelta, timezone
import json
import os
from fastapi import APIRouter, Depends, HTTPException, status, Request
from app.database import get_db
from app.auth import hash_password, verify_password, create_jwt, get_current_user, log_action
from app.config import get_settings
from app.models import UserCreate, UserLogin, TokenResponse, SetupResponse, UserProfileUpdate

LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION_MINUTES = 30

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADMIN_CONFIG_PATH = os.path.join(BASE_DIR, "data", "admin_config.json")

router = APIRouter(prefix="")


def _load_admin_config() -> dict:
    if os.path.exists(ADMIN_CONFIG_PATH):
        with open(ADMIN_CONFIG_PATH, "r") as f:
            return json.load(f)
    return {}


@router.get("/api/check_setup", response_model=SetupResponse)
async def check_setup(db=Depends(get_db)):
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM users")
    row = await cursor.fetchone()
    count = row["cnt"] if row else 0
    return SetupResponse(setup_required=(count == 0))


@router.post("/api/register", response_model=TokenResponse)
async def register(body: UserCreate, db=Depends(get_db)):
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM users")
    row = await cursor.fetchone()
    count = row["cnt"] if row else 0

    if count > 0:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="A user already exists. Registration is disabled.")

    password_hash = hash_password(body.password)
    cursor = await db.execute(
        "INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, 'owner', ?)",
        (body.username, password_hash, getattr(body, 'display_name', None) or getattr(body, 'display_name', '')),
    )
    await db.commit()
    user_id = cursor.lastrowid

    settings = get_settings()
    admin_config = _load_admin_config()
    session_only = admin_config.get("session_only", False)
    expiry_days = admin_config.get("jwt_expiry_days", 30) if not session_only else None
    token = create_jwt({
        "sub": str(user_id),
        "username": body.username,
        "role": "owner",
        "display_name": getattr(body, 'display_name', None) or '',
        "token_version": 0,
    }, settings["SECRET_KEY"], expiry_days=expiry_days, session_only=session_only)
    return TokenResponse(token=token)


@router.post("/api/login")
async def login(body: UserLogin, request: Request, db=Depends(get_db)):
    cursor = await db.execute("SELECT * FROM users WHERE username = ?", (body.username,))
    user = await cursor.fetchone()

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    user_dict = dict(user)

    # Check if account is locked
    locked_until = user_dict.get("locked_until")
    if locked_until:
        try:
            lock_time = datetime.fromisoformat(locked_until)
            if lock_time > datetime.utcnow():
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Account locked. Try again later.")
        except ValueError:
            pass  # Invalid date format, treat as not locked

    if not verify_password(body.password, user_dict["password_hash"]):
        # Increment failed login attempts
        failed_attempts = user_dict.get("failed_login_attempts", 0) + 1
        if failed_attempts >= LOCKOUT_THRESHOLD:
            locked_until = (datetime.utcnow() + timedelta(minutes=LOCKOUT_DURATION_MINUTES)).isoformat()
            await db.execute(
                "UPDATE users SET failed_login_attempts = ?, locked_until = ? WHERE id = ?",
                (failed_attempts, locked_until, user_dict["id"]),
            )
        else:
            await db.execute(
                "UPDATE users SET failed_login_attempts = ? WHERE id = ?",
                (failed_attempts, user_dict["id"]),
            )
        await db.commit()
        remaining = LOCKOUT_THRESHOLD - failed_attempts
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid username or password. {max(0, remaining)} attempt{'s' if remaining != 1 else ''} left before account is locked for {LOCKOUT_DURATION_MINUTES} minutes."
        )

    # Successful login — reset counters
    await db.execute(
        "UPDATE users SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
        (user_dict["id"],),
    )
    await db.commit()

    # Log action
    last_ip = request.client.host if request.client else "unknown"
    await log_action(db, user_dict["id"], user_dict["username"], "login_success", f"Successful login from {last_ip}")

    # Update last login info
    last_ua = (request.headers.get("user-agent", "") or "")[:200]
    await db.execute(
        "UPDATE users SET last_ip = ?, last_user_agent = ? WHERE id = ?",
        (last_ip, last_ua, user_dict["id"]),
    )
    await db.commit()

    settings = get_settings()
    admin_config = _load_admin_config()
    session_only = admin_config.get("session_only", False)
    expiry_days = admin_config.get("jwt_expiry_days", 30) if not session_only else None
    token = create_jwt({
        "sub": str(user_dict["id"]),
        "username": user_dict["username"],
        "role": user_dict.get("role", "user"),
        "display_name": user_dict.get("display_name", ""),
        "token_version": user_dict.get("token_version", 0),
    }, settings["SECRET_KEY"], expiry_days=expiry_days, session_only=session_only)
    return {
        "token": token,
        "session_only": admin_config.get("session_only", False),
    }


@router.get("/api/me")
async def get_me(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "role": user.get("role", "user"),
        "display_name": user.get("display_name", ""),
    }


@router.get("/api/user/profile")
async def get_profile(user: dict = Depends(get_current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name", ""),
        "role": user.get("role", "user"),
    }


@router.patch("/api/user/profile")
async def update_profile(
    body: UserProfileUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    from app.auth import create_jwt, hash_password, log_action
    from app.routers.admin_router import _load_config as _load_admin_config

    updates = []
    params = []

    if body.new_username is not None:
        cursor = await db.execute("SELECT id FROM users WHERE username = ? AND id != ?", (body.new_username, user["id"]))
        if await cursor.fetchone():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken.")
        updates.append("username = ?")
        params.append(body.new_username)

    if body.display_name is not None:
        updates.append("display_name = ?")
        params.append(body.display_name)

    if body.new_password is not None and body.new_password.strip():
        password_hash = hash_password(body.new_password)
        updates.append("password_hash = ?")
        params.append(password_hash)

    if not updates:
        return {"ok": True, "changed": False}

    params.append(user["id"])
    await db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    await db.commit()

    settings = get_settings()
    admin_config = _load_admin_config()
    session_only = admin_config.get("session_only", False)
    expiry_days = admin_config.get("jwt_expiry_days", 30) if not session_only else None

    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user["id"],))
    updated_user = await cursor.fetchone()
    updated_dict = dict(updated_user)

    new_token = create_jwt({
        "sub": str(updated_dict["id"]),
        "username": updated_dict["username"],
        "role": updated_dict.get("role", "user"),
        "display_name": updated_dict.get("display_name", ""),
        "token_version": updated_dict.get("token_version", 0),
    }, settings["SECRET_KEY"], expiry_days=expiry_days, session_only=session_only)

    # Log profile changes
    if body.new_password is not None and body.new_password.strip():
        await log_action(db, user["id"], user["username"], "change_password", "Password changed")
    if body.display_name is not None:
        await log_action(db, user["id"], user["username"], "change_display_name", f"Display name changed to '{body.display_name}'")
    if body.new_username is not None:
        await log_action(db, user["id"], user["username"], "change_username", f"Username changed to '{body.new_username}'")

    return {"ok": True, "token": new_token, "changed": True}


@router.post("/api/logout")
async def logout(user: dict = Depends(get_current_user), db=Depends(get_db)):
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE users SET last_logout_at = ? WHERE id = ?", (now, user["id"]))
    await db.commit()
    return {"ok": True}
