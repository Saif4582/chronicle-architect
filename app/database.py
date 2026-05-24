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

        # Migration: if chapters table exists but is empty, create default chapters
        # from existing projects with non-empty content
        cursor = await db.execute("SELECT COUNT(*) as cnt FROM chapters")
        row = await cursor.fetchone()
        chapter_count = row[0] if row else 0

        if chapter_count == 0:
            # Find projects with non-empty content
            cursor = await db.execute(
                "SELECT id, content FROM projects WHERE content IS NOT NULL AND content != ''"
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


async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row
        yield db
