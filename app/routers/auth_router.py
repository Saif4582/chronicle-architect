from fastapi import APIRouter, Depends, HTTPException, status
from app.database import get_db
from app.auth import hash_password, verify_password, create_jwt
from app.config import get_settings
from app.models import UserCreate, UserLogin, TokenResponse, SetupResponse

router = APIRouter(prefix="")


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
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'owner')",
        (body.username, password_hash),
    )
    await db.commit()
    user_id = cursor.lastrowid

    settings = get_settings()
    token = create_jwt({"sub": str(user_id), "username": body.username}, settings["SECRET_KEY"])
    return TokenResponse(token=token)


@router.post("/api/login", response_model=TokenResponse)
async def login(body: UserLogin, db=Depends(get_db)):
    cursor = await db.execute("SELECT * FROM users WHERE username = ?", (body.username,))
    user = await cursor.fetchone()

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    user_dict = dict(user)
    if not verify_password(body.password, user_dict["password_hash"]):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid username or password")

    settings = get_settings()
    token = create_jwt({"sub": str(user_dict["id"]), "username": user_dict["username"]}, settings["SECRET_KEY"])
    return TokenResponse(token=token)
