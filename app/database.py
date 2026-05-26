import os
import aiosqlite
from app.config import get_settings

DB_PATH = get_settings()["DB_PATH"]


async def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'Untitled',
                description TEXT DEFAULT '',
                content TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS chapters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'Untitled',
                content TEXT DEFAULT '',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS volumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL DEFAULT 'Untitled Volume',
                position INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS wiki_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                parent_id INTEGER DEFAULT NULL,
                content TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY (parent_id) REFERENCES wiki_entries(id) ON DELETE SET NULL
            );
            CREATE INDEX IF NOT EXISTS idx_wiki_project_name ON wiki_entries(project_id, name);
            """
        )
        await db.commit()

        # Migration: add metadata_json to wiki_entries if missing
        try:
            await db.execute("ALTER TABLE wiki_entries ADD COLUMN metadata_json TEXT DEFAULT '{}'")
        except:
            pass  # Column already exists

        # Migration: add volume_id to chapters if missing
        try:
            await db.execute("ALTER TABLE chapters ADD COLUMN volume_id INTEGER DEFAULT NULL REFERENCES volumes(id) ON DELETE SET NULL")
        except:
            pass  # Column already exists

        # Migration: add last_accessed to projects if missing
        try:
            await db.execute("ALTER TABLE projects ADD COLUMN last_accessed TIMESTAMP DEFAULT NULL")
        except:
            pass  # Column already exists

        # Migration: add failed_login_attempts to users if missing
        try:
            await db.execute("ALTER TABLE users ADD COLUMN failed_login_attempts INTEGER NOT NULL DEFAULT 0")
        except:
            pass  # Column already exists

        # Migration: add locked_until to users if missing
        try:
            await db.execute("ALTER TABLE users ADD COLUMN locked_until TEXT DEFAULT NULL")
        except:
            pass  # Column already exists

        # Migration: add role column to users if missing (legacy databases)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'user'")
        except:
            pass  # Column already exists

        # Migration: add token_version to users if missing
        try:
            await db.execute("ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0")
        except:
            pass  # Column already exists

        # Migration: add display_name to users if missing
        try:
            await db.execute("ALTER TABLE users ADD COLUMN display_name TEXT DEFAULT ''")
        except:
            pass  # Column already exists

        # Migration: add last_ip to users if missing
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_ip TEXT DEFAULT ''")
        except:
            pass  # Column already exists

        # Migration: add last_user_agent to users if missing
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_user_agent TEXT DEFAULT ''")
        except:
            pass  # Column already exists

        # Migration: create logs table if not exists
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    user_id INTEGER,
                    username TEXT,
                    action TEXT,
                    details TEXT
                )
            """)
        except:
            pass

        # Migration: ensure no NULL roles exist — set 'user' as default for any legacy rows
        try:
            await db.execute("UPDATE users SET role = 'user' WHERE role IS NULL")
        except:
            pass  # Column might not exist yet

        # Ensure first user is owner – both hardcoded and dynamic fallback
        await db.execute("UPDATE users SET role = 'owner' WHERE id = 1")
        await db.execute("UPDATE users SET role = 'owner' WHERE id = (SELECT MIN(id) FROM users)")

        # Migration: if chapters table exists but is empty, create default chapters
        # from existing projects with non-empty content
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM chapters")
        row = await cursor.fetchone()
        chapter_count = row[0] if row else 0

        if chapter_count == 0:
            # Find projects with non-empty content that don't already have chapters
            cursor = await db.execute(
                "SELECT p.id, p.content FROM projects p WHERE p.content IS NOT NULL AND p.content != '' AND p.id NOT IN (SELECT DISTINCT project_id FROM chapters)"
            )
            projects_with_content = await cursor.fetchall()

            for project in projects_with_content:
                project_id = project[0]
                content = project[1]
                await db.execute(
                    "INSERT INTO chapters (project_id, title, content, position) VALUES (?, ?, ?, ?)",
                    (project_id, "Chapter 1", content, 0),
                )
            await db.commit()

        # Migration: add position to users if missing (for drag-and-drop reorder)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN position INTEGER DEFAULT 0")
        except:
            pass  # Column already exists

        # Migration: add last_active_at to users if missing
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_active_at TEXT DEFAULT NULL")
        except:
            pass  # Column already exists

        # Migration: add last_logout_at to users if missing
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_logout_at TEXT DEFAULT NULL")
        except:
            pass  # Column already exists

        # Migration: add parents column to wiki_entries if missing
        try:
            await db.execute("ALTER TABLE wiki_entries ADD COLUMN parents TEXT DEFAULT NULL")
        except:
            pass  # Column already exists

        await db.commit()


async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row
        yield db
