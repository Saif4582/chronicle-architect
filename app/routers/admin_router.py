import html
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, status
from app.database import get_db
from app.auth import get_current_admin_or_owner, hash_password, log_action
from app.models import UserCreate, AdminUserUpdate, ReorderRequest
from app.tokenizer import get_token_count, count_words


def _strip_html(text: str) -> str:
    """Extract plain text from HTML-equivalent content.

    Pipelines the input through three stages so that the resulting
    character and word counts closely mirror TipTap's ``editor.getText()``
    (the client-side live-counter source of truth):

    1.  Strip all HTML tags (regex – unavoidable approximation; self-closing
        and void elements are handled correctly because the regex removes
        everything inside angle brackets).
    2.  Decode HTML entities (``&nbsp;``, ``&``, ``&mdash;``, …) into
        literal characters via ``html.unescape``.
    3.  Normalise any remaining Unicode whitespace characters (e.g. no-break
        space U+00A0) to ordinary ASCII spaces and collapse runs of
        whitespace so that Python's ``str.split()`` sees the same word
        boundaries as JavaScript's ``/\\s+/``.

    .. note::
        A negligible ±1-2 word / token variation may persist because TipTap
        joins block-level text nodes with ``\\n`` whereas this function
        replaces every tag with a single space.  The difference is only
        noticeable for content whose word count straddles a block boundary
        (``</p><p>`` yields an extra space in the server count that is
        absent from the client count, or vice versa).  Both counts are
        considered accurate.
    """
    text = text or ''
    # 1) Remove tags
    text = re.sub(r'<[^>]*>', ' ', text)
    # 2) Decode HTML entities
    text = html.unescape(text)
    # 3) Normalise whitespace: convert all Unicode whitespace → ASCII space
    #    and collapse consecutive spaces.
    text = ''.join(' ' if unicodedata.category(ch).startswith('Z') or ch in ('\t', '\n', '\r', '\f', '\v') else ch for ch in text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

router = APIRouter(prefix="/api/admin")

# Default settings
DEFAULT_CONFIG = {
    "lockout_threshold": 5,
    "lockout_duration_minutes": 30,
    "jwt_expiry_days": 30,
    "session_only": False,
    "admins_can_create_users": True,
    "admins_can_delete_users": True,
    "admins_can_see_user_ids": False,
    "admins_can_see_device_info": False,
    "admins_can_see_stats": False,
    "admins_can_edit_users": False,
    "admins_can_see_logs": False,
    "admins_can_see_tokens": False,
}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(BASE_DIR, "data", "admin_config.json")


def _load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return dict(DEFAULT_CONFIG)


def _save_config(config: dict):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def _check_hierarchy(admin: dict, target_user: dict):
    """Enforce that admin can only manage users below them in position order. Owner is exempt."""
    if admin["role"] == "owner":
        return  # Owner can manage anyone
    admin_pos = admin.get("position", 99999)
    target_pos = target_user.get("position", 99999)
    if admin_pos > target_pos:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only manage users below you in the hierarchy."
        )


@router.get("/users")
async def list_users(
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    cursor = await db.execute("""
        SELECT id, username, display_name, role, failed_login_attempts, locked_until,
               last_ip, last_user_agent, last_active_at, last_logout_at, position
        FROM users ORDER BY position ASC, id ASC
    """)
    rows = await cursor.fetchall()

    result = []
    for r in rows:
        user_data = dict(r)

        # Get all chapter content for this user
        cursor2 = await db.execute("""
            SELECT ch.content FROM chapters ch
            JOIN projects p ON ch.project_id = p.id
            WHERE p.user_id = ?
        """, (user_data["id"],))
        chapters = await cursor2.fetchall()

        # Get all wiki content for this user (including metadata)
        cursor3 = await db.execute("""
            SELECT w.content, w.metadata_json FROM wiki_entries w
            JOIN projects p ON w.project_id = p.id
            WHERE p.user_id = ?
        """, (user_data["id"],))
        wikis = await cursor3.fetchall()

        total_words = 0
        total_tokens = 0
        total_chars = 0
        for ch in chapters:
            text = _strip_html(ch[0] or "")
            total_words += count_words(text)
            total_tokens += get_token_count(text)
            total_chars += len(text)
        for w in wikis:
            text = _strip_html(w[0] or "")
            total_words += count_words(text)
            total_tokens += get_token_count(text)
            total_chars += len(text)
            # Also count wiki metadata: subcategories, snippet, notepad, attributes
            try:
                meta = json.loads(w[1] or '{}')
                subs = meta.get('subcategories', {})
                for sub_val in subs.values():
                    if sub_val:
                        # Custom subcategories are stored as JSON {"name":..., "content":...}
                        content = sub_val
                        if isinstance(sub_val, str) and sub_val.startswith('{'):
                            try:
                                parsed = json.loads(sub_val)
                                content = parsed.get('content', sub_val)
                            except:
                                pass
                        plain = _strip_html(content)
                        total_words += count_words(plain)
                        total_tokens += get_token_count(plain)
                        total_chars += len(plain)
                snippet = meta.get('ai_context_snippet', '')
                if snippet:
                    total_words += count_words(snippet)
                    total_tokens += get_token_count(snippet)
                    total_chars += len(snippet)
                notepad = meta.get('private_notepad', '')
                if notepad:
                    plain = _strip_html(notepad)
                    total_words += count_words(plain)
                    total_tokens += get_token_count(plain)
                    total_chars += len(plain)
                attrs = meta.get('attributes', {})
                for attr_val in attrs.values():
                    if attr_val:
                        total_words += count_words(str(attr_val))
                        total_tokens += get_token_count(str(attr_val))
                        total_chars += len(str(attr_val))
            except:
                pass

        user_data["total_words"] = total_words
        user_data["total_tokens"] = total_tokens
        user_data["total_chars"] = total_chars
        result.append(user_data)

    return result


@router.put("/users/reorder")
async def reorder_users(
    body: ReorderRequest,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    if admin["role"] != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owner can reorder users.")

    for index, user_id in enumerate(body.order):
        await db.execute(
            "UPDATE users SET position = ? WHERE id = ?",
            (index, user_id),
        )
    await db.commit()
    await log_action(db, admin["id"], admin["username"], "reorder_users", "User order updated")
    return {"ok": True}


@router.patch("/users/{user_id}")
async def admin_update_user(
    user_id: int,
    body: AdminUserUpdate,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    if admin["role"] != "owner":
        config = _load_config()
        if not config.get("admins_can_edit_users", False):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can edit users.")

    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = await cursor.fetchone()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    _check_hierarchy(admin, dict(user))

    updates = []
    params = []

    if body.username is not None:
        cursor2 = await db.execute("SELECT id FROM users WHERE username = ? AND id != ?", (body.username, user_id))
        if await cursor2.fetchone():
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already taken.")
        updates.append("username = ?")
        params.append(body.username)

    if body.display_name is not None:
        updates.append("display_name = ?")
        params.append(body.display_name)

    if body.new_password is not None and body.new_password.strip():
        password_hash = hash_password(body.new_password)
        updates.append("password_hash = ?")
        params.append(password_hash)

    if not updates:
        return {"ok": True, "changed": False}

    params.append(user_id)
    await db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
    await db.commit()

    await log_action(db, admin["id"], admin["username"], "edit_user", f"Edited user ID {user_id}")

    return {"ok": True, "changed": True}


@router.get("/logs")
async def list_logs(
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
    page: int = 1,
    per_page: int = 50,
    action: str = None,
    user_id: int = None,
):
    if admin["role"] != "owner":
        config = _load_config()
        if not config.get("admins_can_see_logs", False):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can view logs.")

    where_clauses = []
    params = []
    if action:
        where_clauses.append("action = ?")
        params.append(action)
    if user_id:
        where_clauses.append("user_id = ?")
        params.append(user_id)

    where_sql = " WHERE " + " AND ".join(where_clauses) if where_clauses else ""

    offset_val = (page - 1) * per_page
    cursor = await db.execute(
        f"SELECT * FROM logs{where_sql} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        params + [per_page, offset_val],
    )
    rows = await cursor.fetchall()

    cursor2 = await db.execute(f"SELECT COUNT(*) as cnt FROM logs{where_sql}", params)
    total = (await cursor2.fetchone())[0]

    cursor3 = await db.execute("SELECT DISTINCT action FROM logs ORDER BY action")
    actions = [r[0] for r in await cursor3.fetchall() if r[0]]

    return {
        "logs": [dict(r) for r in rows],
        "total": total,
        "page": page,
        "per_page": per_page,
        "actions": actions,
    }


@router.post("/users")
async def admin_create_user(
    body: UserCreate,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    # Check if admin is allowed to create users
    if admin["role"] != "owner":
        config = _load_config()
        if not config.get("admins_can_create_users", True):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can create users.")

    # Check if username already exists
    cursor = await db.execute("SELECT id FROM users WHERE username = ?", (body.username,))
    existing = await cursor.fetchone()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Username already exists.")
    
    # Get max position to place new user at bottom
    cursor_pos = await db.execute("SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM users")
    pos_row = await cursor_pos.fetchone()
    next_position = pos_row[0] if pos_row else 0
    
    # Hash password and insert
    password_hash = hash_password(body.password)
    cursor = await db.execute(
        "INSERT INTO users (username, password_hash, role, position) VALUES (?, ?, 'user', ?)",
        (body.username, password_hash, next_position),
    )
    await db.commit()
    await log_action(db, admin["id"], admin["username"], "create_user", f"Created user '{body.username}'")
    return {"ok": True, "id": cursor.lastrowid}


@router.patch("/users/{user_id}/lock")
async def toggle_lock(
    user_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    # Cannot lock owner
    # Cannot perform action on yourself
    if user_id == admin["id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot perform this action on yourself.")

    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = await cursor.fetchone()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user_dict = dict(user)
    if user_dict["role"] == "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot lock the owner.")

    _check_hierarchy(admin, user_dict)

    # Toggle lock: if currently locked, unlock; otherwise lock for duration
    if user_dict.get("locked_until"):
        await db.execute(
            "UPDATE users SET locked_until = NULL, failed_login_attempts = 0 WHERE id = ?",
            (user_id,),
        )
    else:
        config = _load_config()
        duration = config.get("lockout_duration_minutes", 30)
        locked_until = (datetime.utcnow() + timedelta(minutes=duration)).isoformat()
        await db.execute(
            "UPDATE users SET locked_until = ? WHERE id = ?",
            (locked_until, user_id),
        )
    await db.commit()
    # Determine new lock state after toggle
    cursor2 = await db.execute("SELECT locked_until FROM users WHERE id = ?", (user_id,))
    updated_user = await cursor2.fetchone()
    now_locked = updated_user and updated_user[0] is not None
    await log_action(db, admin["id"], admin["username"], "toggle_lock", f"User ID {user_id} {'locked' if now_locked else 'unlocked'}")
    return {"ok": True}


@router.patch("/users/{user_id}/role")
async def change_role(
    user_id: int,
    body: dict,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    # Only owner can change roles
    if admin["role"] != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only owner can change user roles.")

    # Cannot perform action on yourself
    if user_id == admin["id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot perform this action on yourself.")

    new_role = body.get("role")
    if new_role not in ("user", "admin"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid role. Must be 'user' or 'admin'.")

    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = await cursor.fetchone()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user_dict = dict(user)
    if user_dict["role"] == "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cannot change role of the owner.")

    _check_hierarchy(admin, user_dict)

    await db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    await db.commit()
    await log_action(db, admin["id"], admin["username"], "change_role", f"User ID {user_id} changed to {new_role}")
    return {"ok": True}


@router.get("/settings")
async def get_settings(admin: dict = Depends(get_current_admin_or_owner)):
    return _load_config()


@router.patch("/settings")
async def update_settings(
    body: dict,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    if admin["role"] != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can modify settings.")
    config = _load_config()
    if "lockout_threshold" in body:
        val = int(body["lockout_threshold"])
        if val < 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Threshold must be >= 1")
        config["lockout_threshold"] = val
    if "lockout_duration_minutes" in body:
        val = int(body["lockout_duration_minutes"])
        if val < 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Duration must be >= 1")
        config["lockout_duration_minutes"] = val
    if "jwt_expiry_days" in body:
        val = int(body["jwt_expiry_days"])
        if val < 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="JWT expiry must be >= 1")
        config["jwt_expiry_days"] = val
    if "session_only" in body:
        config["session_only"] = bool(body["session_only"])
    if "admins_can_create_users" in body:
        config["admins_can_create_users"] = bool(body["admins_can_create_users"])
    if "admins_can_delete_users" in body:
        config["admins_can_delete_users"] = bool(body["admins_can_delete_users"])
    if "admins_can_see_user_ids" in body:
        config["admins_can_see_user_ids"] = bool(body["admins_can_see_user_ids"])
    if "admins_can_see_device_info" in body:
        config["admins_can_see_device_info"] = bool(body["admins_can_see_device_info"])
    if "admins_can_see_stats" in body:
        config["admins_can_see_stats"] = bool(body["admins_can_see_stats"])
    if "admins_can_edit_users" in body:
        config["admins_can_edit_users"] = bool(body["admins_can_edit_users"])
    if "admins_can_see_logs" in body:
        config["admins_can_see_logs"] = bool(body["admins_can_see_logs"])
    if "admins_can_see_tokens" in body:
        config["admins_can_see_tokens"] = bool(body["admins_can_see_tokens"])
    _save_config(config)
    await log_action(db, admin["id"], admin["username"], "update_settings", "Settings changed")
    return config


@router.delete("/users/{user_id}")
async def delete_user(
    user_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    # Only owner can delete users (or admin if setting allows)
    if admin["role"] != "owner":
        config = _load_config()
        if not config.get("admins_can_delete_users", True):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can delete users.")

    # Cannot delete self
    if user_id == admin["id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete yourself.")

    cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    user = await cursor.fetchone()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    _check_hierarchy(admin, dict(user))

    # Delete all user data (cascading by project deletions handles chapters, volumes, wiki)
    # Delete wiki entries for user's projects
    await db.execute("DELETE FROM wiki_entries WHERE project_id IN (SELECT id FROM projects WHERE user_id = ?)", (user_id,))
    # Delete chapters for user's projects
    await db.execute("DELETE FROM chapters WHERE project_id IN (SELECT id FROM projects WHERE user_id = ?)", (user_id,))
    # Delete volumes for user's projects
    await db.execute("DELETE FROM volumes WHERE project_id IN (SELECT id FROM projects WHERE user_id = ?)", (user_id,))
    # Delete projects
    await db.execute("DELETE FROM projects WHERE user_id = ?", (user_id,))
    # Delete the user
    await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    await db.commit()
    await log_action(db, admin["id"], admin["username"], "delete_user", f"Deleted user ID {user_id} ({dict(user)['username']})")
    return {"ok": True}


@router.get("/tokens")
async def list_tokens(
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    if admin["role"] != "owner":
        config = _load_config()
        if not config.get("admins_can_see_tokens", False):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can view tokens.")

    cursor = await db.execute(
        "SELECT id, username, role, token_version, position FROM users ORDER BY position ASC, id ASC"
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


@router.post("/tokens/revoke/{user_id}")
async def revoke_user_token(
    user_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    if admin["role"] != "owner":
        config = _load_config()
        if not config.get("admins_can_see_tokens", False):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can revoke tokens.")

    cursor = await db.execute("SELECT id, username, role, position FROM users WHERE id = ?", (user_id,))
    user = await cursor.fetchone()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    _check_hierarchy(admin, dict(user))

    await db.execute("UPDATE users SET token_version = token_version + 1 WHERE id = ?", (user_id,))
    await db.commit()
    await log_action(db, admin["id"], admin["username"], "revoke_token", f"Token revoked for user ID {user_id}")
    return {"ok": True}


@router.post("/tokens/revoke-all")
async def revoke_all_tokens(
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    if admin["role"] != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can revoke tokens.")

    # Increment token_version for all users — invalidates all existing JWTs
    await db.execute("UPDATE users SET token_version = token_version + 1")
    await db.commit()
    await log_action(db, admin["id"], admin["username"], "revoke_all_tokens", "All tokens revoked")
    return {"ok": True}
