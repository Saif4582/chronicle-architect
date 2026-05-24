"""
Chronicle Architect CLI — password reset and user management.
"""
import argparse
import asyncio
import getpass
import aiosqlite
from app.config import get_settings
from app.auth import hash_password


async def reset_password():
    settings = get_settings()
    db_path = settings["DB_PATH"]

    password = getpass.getpass("Enter new password: ")
    confirm = getpass.getpass("Confirm new password: ")

    if password != confirm:
        print("Error: Passwords do not match.")
        return

    if len(password) < 6:
        print("Error: Password must be at least 6 characters.")
        return

    password_hash = hash_password(password)

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT id FROM users LIMIT 1")
        user = await cursor.fetchone()
        if user is None:
            print("Error: No user found in the database.")
            return

        await db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (password_hash, user[0]))
        await db.commit()

    print("Password reset successfully.")


async def show_user():
    settings = get_settings()
    db_path = settings["DB_PATH"]

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute("SELECT id, username, created_at FROM users LIMIT 1")
        user = await cursor.fetchone()

    if user is None:
        print("No users registered yet.")
    else:
        print(f"User ID: {user[0]}")
        print(f"Username: {user[1]}")
        print(f"Created at: {user[2]}")


def main():
    parser = argparse.ArgumentParser(description="Chronicle Architect CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("reset_password", help="Reset the current user's password")
    subparsers.add_parser("show_user", help="Show current user info")

    args = parser.parse_args()

    if args.command == "reset_password":
        asyncio.run(reset_password())
    elif args.command == "show_user":
        asyncio.run(show_user())


if __name__ == "__main__":
    main()
