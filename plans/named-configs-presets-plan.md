# Named Configurations / Presets System for AI Endpoints

## Overview

Add a **Named Configurations (Presets)** system to the Manage Endpoint panel, allowing owners/admins to define reusable limit configurations that can be applied when assigning users to endpoints. This eliminates the need to re-enter the same limit values for every user.

---

## 1. Database Schema

### 1.1 New Table: `ai_endpoint_configs`

```sql
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
);
CREATE INDEX IF NOT EXISTS idx_ai_endpoint_configs_endpoint
    ON ai_endpoint_configs(endpoint_id);
```

**Design Notes:**
- `endpoint_id` FK cascades — deleting an endpoint removes its configs.
- `owner_user_id` tracks who created the preset (same as `ai_endpoints.owner_user_id`).
- `name` is a user-visible label (e.g. "High-volume writer", "Basic reader").
- All limit fields mirror the `ai_endpoint_users` schema exactly so a preset can be used as a template for assignment.
- No `shared_pool_id` field — presets define *template values*, not pool membership. Pool assignment is done per-user at assign time.

### 1.2 DB Migration (in `database.py`)

Add after the existing AI table creation block (around line 128), using the same pattern:

```python
# Migration: create ai_endpoint_configs table if missing
try:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS ai_endpoint_configs (
            ...
        )
    """)
    await db.execute("""
        CREATE INDEX IF NOT EXISTS idx_ai_endpoint_configs_endpoint
            ON ai_endpoint_configs(endpoint_id)
    """)
except:
    pass
```

---

## 2. Pydantic Models (in `app/models.py`)

Add after line 205 (after `AIEndpointUserUpdate`):

```python
class AIConfigCreate(BaseModel):
    name: str
    limit_type: str = "requests"
    limit_value_requests: Optional[int] = None
    limit_value_tokens: Optional[int] = None
    reset_schedule: str = "daily"
    reset_time: Optional[str] = None
    is_shared_pool: bool = False

class AIConfigUpdate(BaseModel):
    name: Optional[str] = None
    limit_type: Optional[str] = None
    limit_value_requests: Optional[int] = None
    limit_value_tokens: Optional[int] = None
    reset_schedule: Optional[str] = None
    reset_time: Optional[str] = None
    is_shared_pool: Optional[bool] = None
```

---

## 3. REST API Endpoints (in `app/routers/ai_router.py`)

Add a new section **SECTION G** before the last usage section, or between existing admin endpoint sections and user sections.

All endpoints require `get_current_admin_or_owner` dependency — only admins/owners can manage presets.

### 3.1 Endpoint Signatures

```python
# ===========================================================================
# SECTION G — ADMIN/OWNER: Named Configurations (Presets)
# ===========================================================================

@router.get("/admin/endpoints/{endpoint_id}/configs")
async def admin_list_configs(
    endpoint_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """List all named configurations for an endpoint."""
    # Verify endpoint exists
    cursor = await db.execute("SELECT id FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_configs WHERE endpoint_id = ? ORDER BY name",
        (endpoint_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.post("/admin/endpoints/{endpoint_id}/configs", status_code=201)
async def admin_create_config(
    endpoint_id: int,
    body: AIConfigCreate,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Create a named configuration for an endpoint."""
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    ep = await cursor.fetchone()
    if ep is None:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    
    cursor = await db.execute(
        """INSERT INTO ai_endpoint_configs 
           (endpoint_id, owner_user_id, name, limit_type, limit_value_requests,
            limit_value_tokens, reset_schedule, reset_time, is_shared_pool)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (endpoint_id, admin["id"], body.name, body.limit_type,
         body.limit_value_requests, body.limit_value_tokens,
         body.reset_schedule, body.reset_time,
         1 if body.is_shared_pool else 0),
    )
    await db.commit()
    
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_configs WHERE id = ?", (cursor.lastrowid,)
    )
    return dict(await cursor.fetchone())


@router.put("/admin/endpoints/{endpoint_id}/configs/{config_id}")
async def admin_update_config(
    endpoint_id: int,
    config_id: int,
    body: AIConfigUpdate,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Update a named configuration."""
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_configs WHERE id = ? AND endpoint_id = ?",
        (config_id, endpoint_id),
    )
    config = await cursor.fetchone()
    if config is None:
        raise HTTPException(status_code=404, detail="Configuration not found")
    config = dict(config)
    
    new_name = body.name if body.name is not None else config["name"]
    new_limit_type = body.limit_type if body.limit_type is not None else config["limit_type"]
    new_limit_req = body.limit_value_requests if body.limit_value_requests is not None else config["limit_value_requests"]
    new_limit_tok = body.limit_value_tokens if body.limit_value_tokens is not None else config["limit_value_tokens"]
    new_schedule = body.reset_schedule if body.reset_schedule is not None else config["reset_schedule"]
    new_reset_time = body.reset_time if body.reset_time is not None else config["reset_time"]
    new_shared = 1 if body.is_shared_pool is True else (0 if body.is_shared_pool is False else config["is_shared_pool"])
    
    await db.execute(
        """UPDATE ai_endpoint_configs SET name=?, limit_type=?, limit_value_requests=?,
           limit_value_tokens=?, reset_schedule=?, reset_time=?, is_shared_pool=?,
           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (new_name, new_limit_type, new_limit_req, new_limit_tok,
         new_schedule, new_reset_time, new_shared, config_id),
    )
    await db.commit()
    
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_configs WHERE id = ?", (config_id,)
    )
    return dict(await cursor.fetchone())


@router.delete("/admin/endpoints/{endpoint_id}/configs/{config_id}",
               status_code=204)
async def admin_delete_config(
    endpoint_id: int,
    config_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Delete a named configuration."""
    cursor = await db.execute(
        "SELECT id FROM ai_endpoint_configs WHERE id = ? AND endpoint_id = ?",
        (config_id, endpoint_id),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=404, detail="Configuration not found")
    
    await db.execute("DELETE FROM ai_endpoint_configs WHERE id = ?", (config_id,))
    await db.commit()
    return None
```

### 3.2 Import Updates

Add to the existing import block in [`ai_router.py`](app/routers/ai_router.py:16):
```python
from app.models import (
    ...
    AIConfigCreate,
    AIConfigUpdate,
)
```

---

## 4. UI/UX — Frontend Changes (in `static/index.html`)

### 4.1 Component Tree

```
showAdminEndpointDetail(ep)
  └─ Tab bar: [Models] [Users] [Configurations]  ← NEW tab
       │
       ├─ Models tab (unchanged)
       │
       ├─ Users tab (unchanged, but "Add User" form gets preset dropdown)
       │
       └─ Configurations tab  ← NEW
            ├─ "Create Configuration" button
            ├─ Config list (vertical rows)
            │   ├─ Row: [name] [limit summary] [Edit] [Rename] [Delete]
            │   └─ ...more rows
            ├─ showCreateConfigForm(ep, onCreated)  ← NEW modal
            ├─ showEditConfigForm(ep, config, onUpdated)  ← NEW modal
            └─ showRenameConfigForm(ep, config, onUpdated)  ← NEW modal
```

### 4.2 New Functions

#### 4.2.1 `renderConfigTab()` — Configurations Tab Content

Inline render function inside [`showAdminEndpointDetail()`](static/index.html:4878), similar to the Models/Users tab rendering.

- Fetches configs via `apiGet('/api/ai/admin/endpoints/' + ep.id + '/configs')`
- Renders a "Create Configuration" button that calls `showCreateConfigForm(ep, loadConfigs)`
- Lists each config with:
  - `config.name` (bold, primary color)
  - Summary line: `{limit_type} · {reset_schedule}` with limit values if set
  - Action buttons: **Edit** (opens edit form with all limit fields), **Rename** (quick rename modal), **Delete** (with confirmation)

#### 4.2.2 `showCreateConfigForm(ep, onCreated)` — Create Preset Modal

Copies the form layout from [`showAddUserToEndpointForm()`](static/index.html:5024) but:
- No user selection (just defining a template)
- Has a `name` text input (required)
- All limit fields: limit_type, limit_value_requests, limit_value_tokens, reset_schedule, reset_time, is_shared_pool
- Submit calls `apiPost('/api/ai/admin/endpoints/' + ep.id + '/configs', body)`
- On success, calls `onCreated()` to refresh the config list

#### 4.2.3 `showEditConfigForm(ep, config, onUpdated)` — Edit Preset Modal

Copies the form layout from [`showEditUserLimitsForm()`](static/index.html:5172) but:
- Pre-fills from `config` object
- Has a `name` text input (pre-filled)
- All limit fields
- Submit calls `apiPut('/api/ai/admin/endpoints/' + ep.id + '/configs/' + config.id, body)`
- On success, calls `onUpdated()` to refresh

#### 4.2.4 `showRenameConfigForm(ep, config, onUpdated)` — Quick Rename Modal

Simple modal with a single text input for the name. Calls the same PUT endpoint with only `{ name: newName }`.

### 4.3 Integration Point: Preset Dropdown in Assign User Form

In [`showAddUserToEndpointForm()`](static/index.html:5024), add a **"Apply Configuration"** dropdown above the limit fields.

**Data flow:**
1. On form open, fetch configs: `apiGet('/api/ai/admin/endpoints/' + ep.id + '/configs')`
2. Store in a local `let configs = []` variable
3. Render a `<select>` dropdown with:
   - Option: `"-- Custom (manual) --"` (default)
   - Then each `config.name` as an option
4. When user selects a config:
   - Pre-fill all limit fields (limitType, limitValueRequests, limitValueTokens, resetSchedule, resetTime, isSharedPool) from the selected config's values
   - Re-render the form (so conditional fields show/hide correctly)
5. User can still manually edit any field after pre-fill — selecting a preset is just a convenience starting point
6. If user switches back to `"-- Custom (manual) --"`, clear the fields

**Important:** The preset selection does NOT create a DB relationship. When "Assign" is clicked, the current field values (whether from a preset or custom-entered) are sent as the POST body exactly as today. This means:
- No changes needed to the [`AIEndpointUserAssign`](app/models.py:188) model
- No changes needed to the `admin_assign_user()` endpoint
- No breaking changes to existing functionality

### 4.4 Integration Point: Preset Button in Edit User Limits Form

In [`showEditUserLimitsForm()`](static/index.html:5172), add a small **"Load from Preset"** select:

1. Fetch configs on form open
2. Render a small `<select>` with `"-- Select preset --"` as default and each config name
3. On change, pre-fill all fields (but do NOT overwrite until user confirms)
4. Show a subtle "Preset loaded — review and save" indicator

### 4.5 Tab Implementation Details

In the [`renderDetail()`](static/index.html:4919) function of [`showAdminEndpointDetail()`](static/index.html:4878):

1. Add a third tab button alongside the existing "Models" and "Users" tabs:

```javascript
el('button', {
    style: {
        padding: '8px 16px', fontSize: '13px', fontWeight: '600',
        cursor: 'pointer', border: 'none', background: 'none',
        color: detailTab === 'configs' ? 'var(--accent-primary)' : 'var(--text-muted)',
        borderBottom: detailTab === 'configs' ? '2px solid var(--accent-primary)' : '2px solid transparent',
        fontFamily: 'inherit'
    },
    onclick: () => { detailTab = 'configs'; loadDetailConfigs(); },
}, 'Configurations'),
```

2. Add a `loadDetailConfigs()` function (parallel to `loadDetailModels()` / `loadDetailUsers()`).
3. Add the `else if (detailTab === 'configs')` branch in the rendering logic (before the final `else` block for users).

---

## 5. Data Flow Diagrams

### 5.1 Assign User with Preset

```
[Owner clicks "+ Add User"]
        │
        ▼
[showAddUserToEndpointForm(ep, onAssigned)]
        │
        ├──► apiGet('/api/admin/users')          → allUsers[]
        ├──► apiGet('/api/ai/admin/endpoints/{id}/users') → assignedIds (for filtering)
        └──► apiGet('/api/ai/admin/endpoints/{id}/configs') → configs[]
                │
                ▼
        [Render form]
        ├── User checkboxes (unchanged)
        ├── "Apply Configuration" dropdown ◄── NEW
        │     ├── "-- Custom (manual) --" (default)
        │     ├── "High-volume writer"  (pre-fills limits)
        │     └── "Basic reader"        (pre-fills limits)
        ├── Limit fields (pre-filled if preset selected)
        ├── Reset schedule/time fields
        └── [Assign] button → apiPost('/api/ai/admin/endpoints/{id}/users', body)
                │                                                  ▲
                │                                                  │
                └── body contains current field values ────────────┘
                    (no reference to preset ID — values were copied)
```

### 5.2 Config CRUD Flow

```
[Configurations Tab]
        │
        ├── Load: apiGet('/api/ai/admin/endpoints/{id}/configs')
        │
        ├── [Create Configuration]
        │       └── showCreateConfigForm()
        │               └── apiPost('/api/ai/admin/endpoints/{id}/configs', body)
        │
        ├── [Edit] (on a config row)
        │       └── showEditConfigForm()
        │               └── apiPut('/api/ai/admin/endpoints/{id}/configs/{config_id}', body)
        │
        ├── [Rename] (on a config row)
        │       └── showRenameConfigForm()
        │               └── apiPut('/api/ai/admin/endpoints/{id}/configs/{config_id}', {name})
        │
        └── [Delete] (on a config row)
                └── showConfirmModal() → apiDelete()
```

---

## 6. Implementation Order

### Phase 1: Backend Foundation

| Step | File | Description |
|------|------|-------------|
| 1.1 | [`app/database.py`](app/database.py) | Add `ai_endpoint_configs` table DDL + index as a migration block (try/except pattern) |
| 1.2 | [`app/models.py`](app/models.py) | Add `AIConfigCreate` and `AIConfigUpdate` Pydantic models (after line 205) |
| 1.3 | [`app/routers/ai_router.py`](app/routers/ai_router.py) | Add import of new models |
| 1.4 | [`app/routers/ai_router.py`](app/routers/ai_router.py) | Add SECTION G with 4 CRUD endpoints (GET list, POST create, PUT update, DELETE) |

### Phase 2: Frontend — Configurations Tab

| Step | File | Description |
|------|------|-------------|
| 2.1 | [`static/index.html`](static/index.html) | In `showAdminEndpointDetail()`, add `detailConfigs` state array, `loadDetailConfigs()` function |
| 2.2 | [`static/index.html`](static/index.html) | Add "Configurations" tab button to the tab bar |
| 2.3 | [`static/index.html`](static/index.html) | Add `if (detailTab === 'configs')` rendering block with config list and action buttons |
| 2.4 | [`static/index.html`](static/index.html) | Implement `showCreateConfigForm(ep, onCreated)` modal |
| 2.5 | [`static/index.html`](static/index.html) | Implement `showEditConfigForm(ep, config, onUpdated)` modal |
| 2.6 | [`static/index.html`](static/index.html) | Implement `showRenameConfigForm(ep, config, onUpdated)` modal |
| 2.7 | [`static/index.html`](static/index.html) | Wire Delete button with `showConfirmModal()` confirmation |

### Phase 3: Frontend — Integration with Assign/Edit Flows

| Step | File | Description |
|------|------|-------------|
| 3.1 | [`static/index.html`](static/index.html) | In `showAddUserToEndpointForm()`, fetch configs and add "Apply Configuration" dropdown |
| 3.2 | [`static/index.html`](static/index.html) | Implement on-change handler that pre-fills all limit fields from selected preset |
| 3.3 | [`static/index.html`](static/index.html) | In `showEditUserLimitsForm()`, fetch configs and add "Load from Preset" selector |
| 3.4 | [`static/index.html`](static/index.html) | Implement on-change handler for edit form preset loading |

### Phase 4: Verification

| Step | Description |
|------|-------------|
| 4.1 | Verify existing Models tab still works (fetch, toggle, multipliers) |
| 4.2 | Verify existing Users tab still works (list, add, edit, remove) |
| 4.3 | Verify existing usage tracking and chat functionality unchanged |
| 4.4 | Test config CRUD (create, list, edit, rename, delete) |
| 4.5 | Test preset pre-fills in Add User form |
| 4.6 | Test preset pre-fills in Edit User Limits form |
| 4.7 | Verify cascading delete: removing an endpoint removes its configs |

---

## 7. Design Patterns & Conventions

### Backend
- All admin endpoints use `get_current_admin_or_owner` dependency
- Error handling: `HTTPException` with standard status codes (404, 409, etc.)
- Response format: direct dicts/lists (same as existing endpoints)
- SQL: parameterized queries, `dict(row)` pattern for row conversion

### Frontend
- DOM creation via `el(tag, attrs, ...children)` — same as existing code
- Modals via `showModalOverlay(content)` — reusable overlay pattern
- Form re-rendering via inline `renderForm()` — same pattern as assign/edit forms
- CSS: use existing variables (`var(--bg-elevated)`, `var(--accent-primary)`, `var(--text-muted)`, `var(--border-light)`, `var(--bg-input)`, `var(--border-medium)`, `var(--text-primary)`, `var(--error)`)
- Class names: reuse `btn`, `btn-primary`, `btn-secondary`, `btn-sm`, `form-group`, `modal`, `modal-actions`, `modal-error`, and create minimal new classes if needed
- No external UI libraries — all custom CSS in the existing `<style>` block

### No-Break Guarantees
- No changes to `AIEndpointUserAssign` or `AIEndpointUserUpdate` models
- No changes to existing API endpoint signatures or behavior
- No changes to existing frontend function signatures
- Presets are value-templates only — no FK relationship from user assignments to configs
- All existing tables (`ai_endpoints`, `ai_endpoint_models`, `ai_endpoint_users`, `ai_usage`, `ai_chat_sessions`, `ai_chat_messages`) remain untouched
