# Chronicle Architect
A self‑hosted, private novel writing studio with AI readiness. Lightweight, secure, and accessible anywhere via Tailscale.

## Current Features (v0.4.2)
- **Projects, Volumes, Chapters** – full hierarchical structure with rich text editing (TipTap)
- **Worldbuilding Wiki** – entries for characters, locations, items, factions, lore, **religions**, and **power systems**. Preset subcategories, custom subcategories, attributes, AI context snippet, private notepad, **multi‑parent linking**
- **Live Counters** – words, characters, and tokens (server‑side tokenizer, accurate for GPT‑4/Claude). Consistent across editor, sidebar, and cards.
- **Smart Sorting** – chapters and volumes auto‑sort by numeric order
- **Drag and Drop** – move chapters between volumes
- **Manuscript View** – A4‑paper layout with dark warm theme (charcoal + amber)
- **Auto‑save** – per‑chapter, with status indicator
- **Authentication** – first user becomes owner; JWT sessions; account lockout & IP rate limiting
- **Admin Panel** – user management, security settings, audit logs, token management, position‑based hierarchy, configurable admin permissions
- **Docker ready** – easy deployment on mini PC or cloud; Tailscale integration guide
- **No AI yet** (planned for v0.5+)

## Quick Start
### Run with Docker (recommended)
```bash
git clone https://github.com/Saif4582/chronicle-architect.git
cd chronicle-architect
docker compose up -d
```
Open http://localhost:8000 and register. First account becomes **Owner**.

Persistent data (database, config) is stored in `./data`. Stop with:
```bash
docker compose down
```

### Run without Docker
```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

## Update
```bash
git pull
docker compose up -d --build
```
Or if using the pre‑built image:
```bash
docker compose pull && docker compose up -d
```
The app will notify you of new releases.

## License
GNU General Public License v3.0 – see LICENSE file.
