# Chronicle Architect

A **self-hosted** novel writing web application — minimalist, dark-themed, and built for serious writers.

## Architecture: The Thin Server

Chronicle Architect is built on a **Thin Server** principle: the server is purely a data and sync hub. It stores data, manages authentication, serves the web UI, and can orchestrate future AI calls by proxying to external APIs (OpenAI, Claude, local Ollama). **The server NEVER loads or runs an LLM.** No AI libraries (torch, transformers, ollama, langchain) are used. This keeps your host PC cool, quiet, and low-resource.

## Tech Stack

- **Backend:** FastAPI (async Python, SQLite via aiosqlite)
- **Frontend:** Single HTML file, vanilla JavaScript — no build tools, no npm, no React
- **Auth:** JWT (30-day expiry), bcrypt password hashing
- **Database:** SQLite (`data/chronicle.db`)

## Quick Start

```bash
cd chronicle-architect
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open [http://localhost:8000](http://localhost:8000). The first visit shows the registration page. After registering, you are logged in and can create projects and write.

## CLI Tool

A standalone CLI script is provided for password recovery:

```bash
# Reset the current user's password
python cli.py reset_password

# Show the current registered user
python cli.py show_user
```

The CLI reads the database directly and uses the same bcrypt hashing as the main application.

## Project Structure

```
chronicle-architect/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, lifespan, static mount
│   ├── config.py            # Settings (SECRET_KEY, DB_PATH)
│   ├── database.py          # Async SQLite init, table creation
│   ├── models.py            # Pydantic schemas
│   ├── auth.py              # JWT create/decode, password hashing, get_current_user
│   └── routers/
│       ├── __init__.py
│       ├── auth_router.py   # /register, /login, /check_setup
│       └── projects_router.py # CRUD for projects
├── static/
│   └── index.html           # The entire frontend
├── data/                    # Created on first run (DB + .secret)
├── cli.py                   # Password reset utility
├── requirements.txt
├── .gitignore
└── README.md
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/api/check_setup` | No | Returns `{"setup_required": bool}` |
| POST | `/api/register` | No | Register first (and only) user |
| POST | `/api/login` | No | Login, returns JWT |
| GET | `/api/projects` | Yes | List user's projects |
| POST | `/api/projects` | Yes | Create a new project |
| GET | `/api/projects/{id}` | Yes | Get full project with content |
| PUT | `/api/projects/{id}` | Yes | Update project |

## Design System

"Midnight Tide" — a dark, cyan-accented theme. Purely functional, no animations.

## License

MIT
