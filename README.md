# Chronicle Architect

A self‑hosted, private novel writing studio with rich‑text editing, world‑building wiki, AI chat, user communication, and full admin panel. Lightweight, secure, and accessible anywhere via Tailscale.

## Current Features (v0.5.0)

### 📚 Projects, Volumes & Chapters
- Full hierarchical structure: Project → Volume → Chapter.
- Rich‑text editing with TipTap (bold, italic, underline, strikethrough, headings, lists, blockquotes, fonts, colours, alignment, undo/redo, horizontal rule).
- Auto‑save per chapter (2‑second debounce) with status indicator.
- Live word, character, and token counters (server‑side tokenizer, accurate for GPT‑4/Claude).
- Intelligent sorting (chapters and volumes auto‑sort by numeric order).
- Drag‑and‑drop chapters between volumes.
- Manuscript view (A4 paper layout, dark warm theme).

### 🌍 Worldbuilding Wiki
- Entries grouped by categories: Characters, Locations, Items, Factions, Lore, Religions, Power System + custom categories.
- Preset subcategories per category (e.g., for Characters: Appearance, Personality, Background…).
- Custom subcategories (add, rename, delete) with content‑preserving template system.
- Preset attributes per category (Age, Species, etc.).
- AI Context Snippet (plain text, word limit) and Author's Private Notepad (rich text, never sent to AI).
- **Multi‑parent linking** – entries can have multiple parents (stored as JSON array). First element is the Primary Parent, determining position in the sidebar tree; others are secondary cross‑links.
- **Hierarchical sidebar tree** – entries nest under their primary parent with connector lines (T‑shapes/L‑shapes) via inline `<span>` elements.
- **Drag‑and‑drop parent reassignment** – split‑line visual indicator: drop on left half to detach all parents (make top‑level), drop on right half to adopt target as new primary parent. Cycle prevention falls back to detach.
- Persistent ordering and search.

### 🤖 AI Chat
- Multi‑session AI assistant with streaming responses.
- Per‑model reasoning toggle with proper API‑level parameter mapping (DeepSeek V4, GLM, MiMo, Kimi, OpenAI o‑series).
- Context selection – choose specific volumes, chapters, wiki entries (with search) to include in prompts.
- Per‑user request/token limits with shared pool option and reset schedules.
- Admin endpoint management (CRUD, model selection, multipliers, max context).
- User self‑service endpoints (add your own, share with others).
- Stop button to abort AI responses mid‑stream.

### 💬 User Communication
- **Direct Messages** – send/receive between any two users, contact list with last message preview.
- **Group Chat** – create groups, invite users (accept/decline flow), message history, member list.
- **Global Chat** – room for all users.
- All chat types use WebSocket for real‑time delivery.

### 🔐 Authentication & Security
- First user becomes Owner; subsequent users are regular (can be promoted to admin).
- JWT login with configurable expiry (default 30 days) – stored in localStorage or sessionStorage.
- Account lockout after configurable failed attempts (default 5) for configurable duration (default 30 min).
- IP‑based rate limiting on login/register (5 req/min → 15‑min block).
- Users see remaining attempts on login failure.

### 🛠️ Admin Panel
- **Users Tab** – manage users (create, edit, delete, lock/unlock, promote/demote). Shows display name, role, access, activity (Online/Idle/Offline), last IP, device, words, tokens, size. Drag‑and‑drop hierarchy.
- **Settings Tab** – configure lockout threshold/duration, JWT expiry, session‑only login, admin permissions (create/edit/delete users, see logs, see tokens, see stats, manage AI, delete AI chats, delete global chat, manage updates).
- **Tokens Tab** – view user token versions, revoke individual or all tokens.
- **Logs Tab** – audit trail with filter dropdown, clear logs (owner only).
- **AI Tab** – endpoint CRUD, model management, user assignments, configurations.
- **Update Tab** – check for new releases and apply updates directly (Docker‑based, requires Docker socket mount).
- **Storage Tab** – shows AI endpoint/model/assignment counts.
- **Live updates** via WebSocket – Users, Tokens, and Logs tabs auto‑refresh. No manual refresh needed. Activity status, login/logout, audit logs all live.

### 🐳 Deployment & Updates
- **Docker ready** – `docker-compose.yml` with persistent data volume.
- **GitHub Actions CI** – builds and pushes image to `ghcr.io` on push.
- **Tailscale‑friendly** – expose via `tailscale serve` or `tailscale funnel` for secure remote access.
- **Auto‑update check** – app notifies you when a new version is available.
- **Admin Update Tab** – apply updates from the admin panel (requires Docker socket mounted).

### 📦 Quick Start (Docker)
```bash
git clone https://github.com/Saif4582/chronicle-architect.git
cd chronicle-architect
docker compose up -d
```
Open `http://localhost:8000` and register. First account becomes Owner.

### 🔄 Update
```bash
git pull
docker compose down
docker compose up -d --build
```
Or using the pre‑built image:
```bash
docker compose pull && docker compose up -d
```

### ⚙️ Run Without Docker
```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

### License
GNU General Public License v3.0 – see LICENSE.
