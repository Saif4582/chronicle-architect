import os
import secrets

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chronicle.db")
SECRET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", ".secret")


def _load_or_create_secret() -> str:
    os.makedirs(os.path.dirname(SECRET_PATH), exist_ok=True)
    if os.path.exists(SECRET_PATH):
        with open(SECRET_PATH, "r") as f:
            return f.read().strip()
    key = secrets.token_hex(32)
    with open(SECRET_PATH, "w") as f:
        f.write(key)
    return key


SECRET_KEY = _load_or_create_secret()


def get_settings() -> dict:
    return {
        "SECRET_KEY": SECRET_KEY,
        "DB_PATH": DB_PATH,
    }
