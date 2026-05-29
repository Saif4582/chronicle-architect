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

        # New tables for AI Chat System and User Communication
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS ai_endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key_encrypted TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ai_endpoint_models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint_id INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                multiplier_requests REAL NOT NULL DEFAULT 1.0,
                multiplier_tokens REAL NOT NULL DEFAULT 1.0,
                max_context_tokens INTEGER DEFAULT NULL,
                FOREIGN KEY (endpoint_id) REFERENCES ai_endpoints(id) ON DELETE CASCADE,
                UNIQUE(endpoint_id, model_name)
            );

            CREATE TABLE IF NOT EXISTS ai_endpoint_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                limit_type TEXT NOT NULL DEFAULT 'requests',
                limit_value_requests INTEGER DEFAULT NULL,
                limit_value_tokens INTEGER DEFAULT NULL,
                reset_schedule TEXT NOT NULL DEFAULT 'daily',
                reset_time TEXT DEFAULT NULL,
                is_shared_pool INTEGER NOT NULL DEFAULT 0,
                shared_pool_id INTEGER DEFAULT NULL,
                FOREIGN KEY (endpoint_id) REFERENCES ai_endpoints(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(endpoint_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS ai_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                token_count INTEGER NOT NULL DEFAULT 0,
                recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                period_start TEXT NOT NULL,
                FOREIGN KEY (endpoint_id) REFERENCES ai_endpoints(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_ai_usage_user_period ON ai_usage(user_id, period_start);
            CREATE INDEX IF NOT EXISTS idx_ai_usage_endpoint ON ai_usage(endpoint_id, period_start);

            CREATE TABLE IF NOT EXISTS ai_chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint_id INTEGER NOT NULL,
                model_name TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT 'New Chat',
                context_selection TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (endpoint_id) REFERENCES ai_endpoints(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS ai_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                token_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (session_id) REFERENCES ai_chat_sessions(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dm_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                recipient_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (recipient_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_dm_conversation ON dm_messages(sender_id, recipient_id);

            CREATE TABLE IF NOT EXISTS chat_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (creator_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS chat_group_members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'accepted', 'declined', 'removed')),
                joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (group_id) REFERENCES chat_groups(id) ON DELETE CASCADE,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                UNIQUE(group_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS chat_group_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                sender_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (group_id) REFERENCES chat_groups(id) ON DELETE CASCADE,
                FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS global_chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )
        await db.commit()

        # Migration: add reasoning to ai_chat_messages if missing
        try:
            await db.execute("ALTER TABLE ai_chat_messages ADD COLUMN reasoning TEXT DEFAULT NULL")
        except:
            pass  # Column already exists

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

        # Migration: add last_login_at to users if missing
        try:
            await db.execute("ALTER TABLE users ADD COLUMN last_login_at TEXT DEFAULT NULL")
        except:
            pass  # Column already exists

        # Migration: add parents column to wiki_entries if missing
        try:
            await db.execute("ALTER TABLE wiki_entries ADD COLUMN parents TEXT DEFAULT NULL")
        except:
            pass  # Column already exists

        # Migration: add position column to wiki_entries if missing
        try:
            await db.execute("ALTER TABLE wiki_entries ADD COLUMN position INTEGER DEFAULT 0")
        except:
            pass  # Column already exists

        # Migration: add indexes to wiki_entries
        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_wiki_category_position ON wiki_entries(category, position)")
            await db.commit()
        except:
            pass  # Index already exists

        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_wiki_project ON wiki_entries(project_id)")
            await db.commit()
        except:
            pass  # Index already exists

        try:
            await db.execute("CREATE INDEX IF NOT EXISTS idx_wiki_parent_id ON wiki_entries(parent_id)")
            await db.commit()
        except:
            pass  # Index already exists

        # Migration: create ai_endpoint_configs table if missing
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ai_endpoint_configs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint_id     INTEGER NOT NULL,
                    owner_user_id   INTEGER NOT NULL,
                    name            TEXT    NOT NULL,
                    limit_type      TEXT    NOT NULL DEFAULT 'requests',
                    limit_value_requests INTEGER DEFAULT NULL,
                    limit_value_tokens   INTEGER DEFAULT NULL,
                    reset_schedule  TEXT    NOT NULL DEFAULT 'daily',
                    reset_time      TEXT    DEFAULT NULL,
                    is_shared_pool  INTEGER NOT NULL DEFAULT 0,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (endpoint_id)   REFERENCES ai_endpoints(id) ON DELETE CASCADE,
                    FOREIGN KEY (owner_user_id) REFERENCES users(id)      ON DELETE CASCADE
                )
            """)
            await db.execute("""
                CREATE INDEX IF NOT EXISTS idx_ai_endpoint_configs_endpoint
                    ON ai_endpoint_configs(endpoint_id)
            """)
        except:
            pass

        # Migration: add last_accessed_at to ai_endpoint_configs if missing
        try:
            await db.execute("ALTER TABLE ai_endpoint_configs ADD COLUMN last_accessed_at TEXT DEFAULT NULL")
        except:
            pass  # Column already exists

        # Migration: add is_admin_endpoint to ai_endpoints if missing
        try:
            await db.execute("ALTER TABLE ai_endpoints ADD COLUMN is_admin_endpoint INTEGER NOT NULL DEFAULT 0")
        except:
            pass  # Column already exists

        # Update existing admin/owner-created endpoints (separate try block so it always runs)
        try:
            await db.execute(
                "UPDATE ai_endpoints SET is_admin_endpoint = 1 "
                "WHERE owner_user_id IN (SELECT id FROM users WHERE role IN ('admin', 'owner'))"
            )
        except:
            pass

        # Migration: create ai_endpoint_config_users bridge table if missing
        try:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ai_endpoint_config_users (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_id       INTEGER NOT NULL,
                    user_id         INTEGER NOT NULL,
                    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (config_id) REFERENCES ai_endpoint_configs(id) ON DELETE CASCADE,
                    FOREIGN KEY (user_id)   REFERENCES users(id)             ON DELETE CASCADE,
                    UNIQUE(config_id, user_id)
                )
            """)
        except:
            pass

        # Migration: add config_id to ai_chat_sessions if missing
        try:
            await db.execute("ALTER TABLE ai_chat_sessions ADD COLUMN config_id INTEGER DEFAULT NULL")
        except:
            pass  # Column already exists

        await db.commit()


async def get_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        db.row_factory = aiosqlite.Row
        yield db
