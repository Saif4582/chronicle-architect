import os
import secrets

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "chronicle.db")
SECRET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", ".secret")
ENCRYPTION_KEY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", ".encryption_key")


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


def _load_or_create_encryption_key() -> bytes:
    """Load or create a Fernet encryption key for API key storage."""
    import base64, os as _os
    from cryptography.fernet import Fernet
    _os.makedirs(_os.path.dirname(ENCRYPTION_KEY_PATH), exist_ok=True)
    if _os.path.exists(ENCRYPTION_KEY_PATH):
        with open(ENCRYPTION_KEY_PATH, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    with open(ENCRYPTION_KEY_PATH, "wb") as f:
        f.write(key)
    return key


# Import and cache the key
ENCRYPTION_KEY = _load_or_create_encryption_key()


def get_settings() -> dict:
    return {
        "SECRET_KEY": SECRET_KEY,
        "DB_PATH": DB_PATH,
        "ENCRYPTION_KEY": ENCRYPTION_KEY,
    }
