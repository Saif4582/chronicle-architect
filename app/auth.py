import hashlib
import jwt
from datetime import datetime, timedelta, timezone
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.config import get_settings
from app.database import get_db

ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 30

security = HTTPBearer()


def hash_password(password: str) -> str:
    sha = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return bcrypt.hashpw(sha.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed: str) -> bool:
    sha = hashlib.sha256(plain_password.encode("utf-8")).hexdigest()
    return bcrypt.checkpw(sha.encode("utf-8"), hashed.encode("utf-8"))


def create_jwt(data: dict, secret: str, expiry_days: int | None = None, session_only: bool = False) -> str:
    payload = data.copy()
    if session_only:
        payload["exp"] = datetime.now(timezone.utc) + timedelta(hours=1)
    else:
        days = expiry_days if expiry_days is not None else TOKEN_EXPIRE_DAYS
        payload["exp"] = datetime.now(timezone.utc) + timedelta(days=days)
    payload["iat"] = datetime.now(timezone.utc)
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_jwt(token: str, secret: str) -> dict | None:
    try:
        return jwt.decode(token, secret, algorithms=[ALGORITHM])
    except jwt.PyJWTError:
        return None


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db=Depends(get_db),
):
    settings = get_settings()
    payload = decode_jwt(credentials.credentials, settings["SECRET_KEY"])
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    user_id = payload.get("sub")
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = await cursor.fetchone()

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")

    user_dict = dict(user)

    # Check token_version to detect revoked tokens
    token_version = payload.get("token_version", 0)
    if user_dict.get("token_version", 0) != token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token has been revoked. Please log in again.")

    # Update last_active_at on every authenticated request
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("UPDATE users SET last_active_at = ? WHERE id = ?", (now, user_id))
    await db.commit()

    # Re-read the user to get the updated last_active_at
    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = await cursor.fetchone()
    user_dict = dict(user)

    return user_dict


async def get_current_admin_or_owner(
    current_user: dict = Depends(get_current_user),
):
    if current_user["role"] not in ("admin", "owner"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required.")
    return current_user


async def log_action(db, user_id: int, username: str, action: str, details: str = ""):
    from datetime import datetime
    timestamp = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "INSERT INTO logs (timestamp, user_id, username, action, details) VALUES (?, ?, ?, ?, ?)",
        (timestamp, user_id, username, action, details),
    )
    await db.commit()
