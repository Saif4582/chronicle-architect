# Chronicle Architect

A self‑hosted, private novel writing studio with AI readiness. Lightweight, secure, and accessible anywhere via Tailscale.

## Current Features (v0.3.1)

- **Projects, Volumes, Chapters** – full hierarchical structure with rich text editing (TipTap)
- **Worldbuilding Wiki** – entries for characters, locations, items, factions, and lore. Preset subcategories, custom subcategories, attributes, AI context snippet, private notepad
- **Live Counters** – words, characters, and tokens (server‑side tokenizer, accurate for GPT‑4/Claude)
- **Smart Sorting** – chapters and volumes auto‑sort by numeric order (e.g., Chapter 1, 2, 10)
- **Drag and Drop** – move chapters between volumes
- **Manuscript View** – A4‑style paper layout with dark warm theme (charcoal + amber)
- **Auto‑save** – per‑chapter, with status indicator
- **Authentication** – first user registered becomes owner; JWT sessions
- **Docker ready** – easy deployment on mini PC or cloud
- **No AI yet** (planned for v0.4+)

## Roadmap

- AI integration: chat with your story, context injection from wiki
- Admin dashboard: usage limits, user management
- Export to .docx / .epub
- Mobile responsive polish

## Quick Start

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Open http://localhost:8000 and register.

## Update

```bash
git pull
```

The app will notify you of new releases.

## License

MIT License – see LICENSE file.
