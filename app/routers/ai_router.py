import html
import json
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.auth import get_current_admin_or_owner, get_current_user
from app.config import get_settings
from app.database import DB_PATH, get_db
from app.models import (
    AIChatRequest,
    AIChatSessionCreate,
    AIChatSessionUpdate,
    AIConfigCreate,
    AIConfigUpdate,
    AIEndpointCreate,
    AIEndpointModelUpdate,
    AIEndpointUpdate,
    AIEndpointUserAssign,
    AIEndpointUserUpdate,
)
from app.tokenizer import get_token_count

# ---------------------------------------------------------------------------
# HTML stripping helper (copied from admin_router / chapters_router)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Reasoning/Thinking parameter map
# ---------------------------------------------------------------------------
# Keys are regex patterns matched against model_name (case-insensitive).
# First match wins. `enable`/`disable` objects are merged into the upstream
# POST JSON body. When `extra_body_wrap` is True, params are nested under
# an `extra_body` key (OpenAI convention used by MiMo, Kimi, etc.).
REASONING_MAP = [
    {
        "pattern": r"^(deepseek|glm)",
        "enable": {"thinking": {"type": "enabled"}},
        "disable": {"thinking": {"type": "disabled"}},
    },
    {
        "pattern": r"^(mimo|kimi)",
        "extra_body_wrap": True,
        "enable": {"reasoning": True},
        "disable": {"reasoning": False},
    },
    {
        "pattern": r"^o1|^o3|^o4",
        "enable": {"reasoning_effort": "medium"},
        "disable": {},
    },
]


# ---------------------------------------------------------------------------
# Encryption helpers
# ---------------------------------------------------------------------------


def _get_fernet() -> Fernet:
    return Fernet(get_settings()["ENCRYPTION_KEY"])


def _encrypt_api_key(key: str) -> str:
    return _get_fernet().encrypt(key.encode()).decode()


def _decrypt_api_key(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()


def _mask_api_key(encrypted: str) -> str:
    """Return a masked version for frontend display."""
    try:
        raw = _decrypt_api_key(encrypted)
        if len(raw) <= 8:
            return "••••••••"
        return raw[:4] + "••••••••" + raw[-4:]
    except Exception:
        return "••••••••"


# ---------------------------------------------------------------------------
# Period / schedule helpers
# ---------------------------------------------------------------------------


def get_period_start(now: datetime, schedule: str, reset_time: str | None) -> str:
    """Return ISO date string for the start of the current period."""
    schedule = (schedule or "daily").lower()
    if schedule == "daily":
        hour = int(reset_time) if reset_time else 0
        if now.hour < hour:
            start = now.replace(hour=hour, minute=0, second=0, microsecond=0) - timedelta(days=1)
        else:
            start = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    elif schedule == "weekly":
        target_day = {
            "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
            "friday": 4, "saturday": 5, "sunday": 6,
        }
        day_name = (reset_time or "monday").lower()
        day_num = target_day.get(day_name, 0)
        days_since = (now.weekday() - day_num) % 7
        start = (now - timedelta(days=days_since)).replace(hour=0, minute=0, second=0, microsecond=0)
    elif schedule == "monthly":
        day = int(reset_time) if reset_time else 1
        if now.day >= day:
            start = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
        else:
            if now.month == 1:
                start = now.replace(year=now.year - 1, month=12, day=day, hour=0, minute=0, second=0, microsecond=0)
            else:
                start = now.replace(month=now.month - 1, day=day, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.isoformat()


# ---------------------------------------------------------------------------
# Admin config loader (replicated from admin_router to avoid circular imports)
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(BASE_DIR, "data", "admin_config.json")

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
    "admins_can_delete_ai_chats": False,
}


def _load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return dict(DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Limit checking logic
# ---------------------------------------------------------------------------


async def check_usage_limits(db, endpoint_id: int, user_id: int, model_name: str) -> dict | None:
    """
    Returns None if within limits, or an error dict if over limit.
    Checks both per-user and shared-pool limits.
    """
    # 1. Get the user's assignment for this endpoint
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?",
        (endpoint_id, user_id),
    )
    assignment = await cursor.fetchone()

    if assignment is None:
        # User might be the endpoint owner or admin — check if they set self-limits
        return None  # No limits set = unlimited

    assignment = dict(assignment)

    # 2. Get model multipliers
    cursor = await db.execute(
        "SELECT multiplier_requests, multiplier_tokens FROM ai_endpoint_models WHERE endpoint_id = ? AND model_name = ?",
        (endpoint_id, model_name),
    )
    model = await cursor.fetchone()
    if model is None:
        return {"error": "Model not available for this endpoint"}

    model = dict(model)
    req_mult = float(model.get("multiplier_requests", 1.0))
    tok_mult = float(model.get("multiplier_tokens", 1.0))

    # 3. Determine current period start
    now = datetime.now(timezone.utc)
    if assignment.get("is_shared_pool") and assignment.get("shared_pool_id"):
        # Get all users in this shared pool
        cursor = await db.execute(
            "SELECT user_id FROM ai_endpoint_users WHERE shared_pool_id = ? AND endpoint_id = ?",
            (assignment["shared_pool_id"], endpoint_id),
        )
        pool_rows = await cursor.fetchall()
        pool_user_ids = [assignment["user_id"]] + [r["user_id"] for r in pool_rows]
    else:
        pool_user_ids = [user_id]

    period_start = get_period_start(now, assignment.get("reset_schedule", "daily"), assignment.get("reset_time"))

    # 4. Query usage for this period
    placeholders = ",".join("?" for _ in pool_user_ids)
    cursor = await db.execute(
        f"SELECT COALESCE(SUM(request_count), 0) as total_requests, COALESCE(SUM(token_count), 0) as total_tokens "
        f"FROM ai_usage WHERE endpoint_id = ? AND model_name = ? AND period_start = ? AND user_id IN ({placeholders})",
        [endpoint_id, model_name, period_start] + pool_user_ids,
    )
    usage = await cursor.fetchone()
    usage = dict(usage) if usage else {"total_requests": 0, "total_tokens": 0}

    # 5. Check request limit
    limit_type = assignment.get("limit_type", "requests")
    if limit_type in ("requests", "both"):
        limit_val = assignment.get("limit_value_requests")
        if limit_val is not None:
            if limit_val == 0:
                pass  # 0 = unlimited
            else:
                effective_used = usage["total_requests"] * req_mult
                if effective_used >= limit_val:
                    return {
                        "error": f"Request limit reached. {effective_used}/{limit_val} effective requests used.",
                        "limit_type": "requests",
                        "used": effective_used,
                        "limit": limit_val,
                        "remaining": 0,
                    }

    # 6. Check token limit
    if limit_type in ("tokens", "both"):
        limit_val = assignment.get("limit_value_tokens")
        if limit_val is not None:
            if limit_val == 0:
                pass  # 0 = unlimited
            else:
                effective_used = usage["total_tokens"] * tok_mult
                if effective_used >= limit_val:
                    return {
                        "error": f"Token limit reached. {effective_used}/{limit_val} effective tokens used.",
                        "limit_type": "tokens",
                        "used": effective_used,
                        "limit": limit_val,
                        "remaining": 0,
                    }

    return None  # Within limits


# ---------------------------------------------------------------------------
# Background task: save completion after streaming
# ---------------------------------------------------------------------------


async def save_completion(
    db_path: str,
    session_id: int,
    content: str,
    token_count: int,
    endpoint_id: int,
    user_id: int,
    model_name: str,
    period_start: str,
    reasoning_content: str = "",
):
    """Save assistant message and record usage after streaming completes."""
    import aiosqlite

    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA journal_mode=WAL")
        db.row_factory = aiosqlite.Row

        # Save assistant message
        await db.execute(
            "INSERT INTO ai_chat_messages (session_id, role, content, reasoning, token_count) VALUES (?, 'assistant', ?, ?, ?)",
            (session_id, content, reasoning_content, token_count),
        )

        # Record usage — upsert pattern: update existing or insert new
        cursor = await db.execute(
            "SELECT id, request_count, token_count FROM ai_usage "
            "WHERE endpoint_id = ? AND user_id = ? AND model_name = ? AND period_start = ?",
            (endpoint_id, user_id, model_name, period_start),
        )
        row = await cursor.fetchone()
        if row:
            await db.execute(
                "UPDATE ai_usage SET request_count = request_count + 1, "
                "token_count = token_count + ?, recorded_at = CURRENT_TIMESTAMP WHERE id = ?",
                (token_count, row["id"]),
            )
        else:
            await db.execute(
                "INSERT INTO ai_usage (endpoint_id, user_id, model_name, request_count, token_count, period_start) "
                "VALUES (?, ?, ?, 1, ?, ?)",
                (endpoint_id, user_id, model_name, token_count, period_start),
            )
        await db.commit()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/ai")


# ===========================================================================
# SECTION A — ADMIN/OWNER: Endpoint CRUD
# ===========================================================================


@router.get("/admin/endpoints")
async def admin_list_endpoints(
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Return all AI endpoints with models and assigned users (masked keys)."""
    cursor = await db.execute(
        "SELECT e.*, u.username as owner_username FROM ai_endpoints e "
        "JOIN users u ON e.owner_user_id = u.id ORDER BY e.created_at DESC"
    )
    endpoints = [dict(row) for row in await cursor.fetchall()]

    for ep in endpoints:
        ep["api_key_encrypted"] = _mask_api_key(ep["api_key_encrypted"])
        ep["api_key_masked"] = True

        # Load models for this endpoint
        cursor = await db.execute(
            "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ?", (ep["id"],)
        )
        ep["models"] = [dict(m) for m in await cursor.fetchall()]

        # Load assigned users
        cursor = await db.execute(
            "SELECT aeu.*, u.username, u.display_name, u.role "
            "FROM ai_endpoint_users aeu JOIN users u ON aeu.user_id = u.id "
            "WHERE aeu.endpoint_id = ?",
            (ep["id"],),
        )
        ep["assigned_users"] = [dict(u) for u in await cursor.fetchall()]

    return endpoints


@router.post("/admin/endpoints", status_code=status.HTTP_201_CREATED)
async def admin_create_endpoint(
    body: AIEndpointCreate,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Create a new AI endpoint (owned by the admin/owner who creates it)."""
    encrypted_key = _encrypt_api_key(body.api_key)
    cursor = await db.execute(
        "INSERT INTO ai_endpoints (owner_user_id, name, base_url, api_key_encrypted, is_admin_endpoint) VALUES (?, ?, ?, ?, 1)",
        (admin["id"], body.name, body.base_url, encrypted_key),
    )
    await db.commit()
    ep_id = cursor.lastrowid

    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (ep_id,))
    ep = dict(await cursor.fetchone())
    ep["api_key_encrypted"] = _mask_api_key(ep["api_key_encrypted"])
    ep["api_key_masked"] = True
    return ep


@router.put("/admin/endpoints/{endpoint_id}")
async def admin_update_endpoint(
    endpoint_id: int,
    body: AIEndpointUpdate,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Update an endpoint. Only updates api_key if a new non-empty value is provided."""
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    ep = await cursor.fetchone()
    if ep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    ep = dict(ep)

    new_name = body.name if body.name is not None else ep["name"]
    new_url = body.base_url if body.base_url is not None else ep["base_url"]
    new_key = ep["api_key_encrypted"]

    if body.api_key is not None and body.api_key != "":
        new_key = _encrypt_api_key(body.api_key)

    await db.execute(
        "UPDATE ai_endpoints SET name = ?, base_url = ?, api_key_encrypted = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_name, new_url, new_key, endpoint_id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    updated = dict(await cursor.fetchone())
    updated["api_key_encrypted"] = _mask_api_key(updated["api_key_encrypted"])
    updated["api_key_masked"] = True
    return updated


@router.delete("/admin/endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_endpoint(
    endpoint_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Delete an endpoint. Cascades to models, user assignments, usage, sessions, messages."""
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    await db.execute("DELETE FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    await db.commit()
    return None


@router.post("/admin/endpoints/{endpoint_id}/fetch-models")
async def admin_fetch_models(
    endpoint_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Fetch available models from the upstream provider and upsert them into the database."""
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    ep = await cursor.fetchone()
    if ep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    ep = dict(ep)

    api_key = _decrypt_api_key(ep["api_key_encrypted"])
    models_url = ep["base_url"].rstrip("/") + "/models"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                models_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Upstream returned {resp.status_code}: {resp.text[:500]}",
                )
            data = resp.json()
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to upstream: {str(e)}",
        )

    model_list = data.get("data", [])
    fetched_names = []
    new_count = 0

    for item in model_list:
        model_name = item.get("id") or item.get("name")
        if not model_name:
            continue
        fetched_names.append(model_name)

        # INSERT OR IGNORE
        cursor = await db.execute(
            "SELECT id FROM ai_endpoint_models WHERE endpoint_id = ? AND model_name = ?",
            (endpoint_id, model_name),
        )
        existing = await cursor.fetchone()
        if existing is None:
            await db.execute(
                "INSERT INTO ai_endpoint_models (endpoint_id, model_name) VALUES (?, ?)",
                (endpoint_id, model_name),
            )
            new_count += 1

    await db.commit()
    return {"fetched_models": fetched_names, "new_count": new_count, "total": len(fetched_names)}


@router.get("/admin/endpoints/{endpoint_id}/models")
async def admin_get_models(
    endpoint_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Return all models for this endpoint from the database."""
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? ORDER BY model_name",
        (endpoint_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.put("/admin/endpoints/{endpoint_id}/models/{model_name}")
async def admin_update_model(
    endpoint_id: int,
    model_name: str,
    body: AIEndpointModelUpdate,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Update model settings (enabled, multipliers, max_context_tokens)."""
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? AND model_name = ?",
        (endpoint_id, model_name),
    )
    model = await cursor.fetchone()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found for this endpoint")
    model = dict(model)

    enabled = 1 if body.enabled is True else (0 if body.enabled is False else model["enabled"])
    req_mult = body.multiplier_requests if body.multiplier_requests is not None else model["multiplier_requests"]
    tok_mult = body.multiplier_tokens if body.multiplier_tokens is not None else model["multiplier_tokens"]
    max_ctx = body.max_context_tokens if body.max_context_tokens is not None else model["max_context_tokens"]

    await db.execute(
        "UPDATE ai_endpoint_models SET enabled = ?, multiplier_requests = ?, multiplier_tokens = ?, max_context_tokens = ? "
        "WHERE endpoint_id = ? AND model_name = ?",
        (enabled, req_mult, tok_mult, max_ctx, endpoint_id, model_name),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? AND model_name = ?",
        (endpoint_id, model_name),
    )
    return dict(await cursor.fetchone())


@router.get("/admin/endpoints/{endpoint_id}/users")
async def admin_get_endpoint_users(
    endpoint_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Return all user assignments for this endpoint with user details."""
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    cursor = await db.execute(
        "SELECT aeu.*, u.username, u.display_name, u.role "
        "FROM ai_endpoint_users aeu JOIN users u ON aeu.user_id = u.id "
        "WHERE aeu.endpoint_id = ?",
        (endpoint_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.post("/admin/endpoints/{endpoint_id}/users", status_code=status.HTTP_201_CREATED)
async def admin_assign_user(
    endpoint_id: int,
    body: AIEndpointUserAssign,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Assign a user to this endpoint with usage limits."""
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    # Verify target user exists
    cursor = await db.execute("SELECT id FROM users WHERE id = ?", (body.user_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Check for existing assignment
    cursor = await db.execute(
        "SELECT id FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?",
        (endpoint_id, body.user_id),
    )
    if await cursor.fetchone() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already assigned to this endpoint")

    await db.execute(
        "INSERT INTO ai_endpoint_users (endpoint_id, user_id, limit_type, limit_value_requests, "
        "limit_value_tokens, reset_schedule, reset_time, is_shared_pool, shared_pool_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            endpoint_id,
            body.user_id,
            body.limit_type,
            body.limit_value_requests,
            body.limit_value_tokens,
            body.reset_schedule,
            body.reset_time,
            1 if body.is_shared_pool else 0,
            body.shared_pool_id,
        ),
    )

    # If a config was specified, record the config-user mapping
    if body.config_id:
        try:
            await db.execute(
                "INSERT INTO ai_endpoint_config_users (config_id, user_id) VALUES (?, ?)",
                (body.config_id, body.user_id),
            )
        except:
            pass  # Ignore duplicate

    await db.commit()

    cursor = await db.execute(
        "SELECT aeu.*, u.username, u.display_name, u.role "
        "FROM ai_endpoint_users aeu JOIN users u ON aeu.user_id = u.id "
        "WHERE aeu.endpoint_id = ? AND aeu.user_id = ?",
        (endpoint_id, body.user_id),
    )
    return dict(await cursor.fetchone())


@router.put("/admin/endpoints/{endpoint_id}/users/{user_id}")
async def admin_update_user_limits(
    endpoint_id: int,
    user_id: int,
    body: AIEndpointUserUpdate,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Update a user's limits for this endpoint."""
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?",
        (endpoint_id, user_id),
    )
    assignment = await cursor.fetchone()
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User assignment not found")
    assignment = dict(assignment)

    new_limit_type = body.limit_type if body.limit_type is not None else assignment["limit_type"]
    new_limit_val_req = body.limit_value_requests if body.limit_value_requests is not None else assignment["limit_value_requests"]
    new_limit_val_tok = body.limit_value_tokens if body.limit_value_tokens is not None else assignment["limit_value_tokens"]
    new_schedule = body.reset_schedule if body.reset_schedule is not None else assignment["reset_schedule"]
    new_reset_time = body.reset_time if body.reset_time is not None else assignment["reset_time"]
    new_shared = 1 if body.is_shared_pool is True else (0 if body.is_shared_pool is False else assignment["is_shared_pool"])

    await db.execute(
        "UPDATE ai_endpoint_users SET limit_type = ?, limit_value_requests = ?, limit_value_tokens = ?, "
        "reset_schedule = ?, reset_time = ?, is_shared_pool = ? WHERE endpoint_id = ? AND user_id = ?",
        (new_limit_type, new_limit_val_req, new_limit_val_tok, new_schedule, new_reset_time, new_shared, endpoint_id, user_id),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT aeu.*, u.username, u.display_name, u.role "
        "FROM ai_endpoint_users aeu JOIN users u ON aeu.user_id = u.id "
        "WHERE aeu.endpoint_id = ? AND aeu.user_id = ?",
        (endpoint_id, user_id),
    )
    return dict(await cursor.fetchone())


@router.delete("/admin/endpoints/{endpoint_id}/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_remove_user(
    endpoint_id: int,
    user_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Remove a user's access to this endpoint."""
    cursor = await db.execute(
        "SELECT id FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?",
        (endpoint_id, user_id),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User assignment not found")

    await db.execute("DELETE FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?", (endpoint_id, user_id))
    await db.commit()
    return None


@router.get("/admin/usage")
async def admin_usage_dashboard(
    period: str = "daily",
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Return a usage dashboard for all users, endpoints, models."""
    now = datetime.now(timezone.utc)
    if period == "daily":
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "weekly":
        days_since = now.weekday()
        period_start = (now - timedelta(days=days_since)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "monthly":
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    else:
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    # Get all usage records for this period
    cursor = await db.execute(
        "SELECT au.*, ae.name as endpoint_name, u.username, u.display_name "
        "FROM ai_usage au "
        "JOIN ai_endpoints ae ON au.endpoint_id = ae.id "
        "JOIN users u ON au.user_id = u.id "
        "WHERE au.period_start = ? ORDER BY au.recorded_at DESC",
        (period_start,),
    )
    usage_records = [dict(r) for r in await cursor.fetchall()]

    # Summaries
    total_requests = sum(r["request_count"] for r in usage_records)
    total_tokens = sum(r["token_count"] for r in usage_records)

    # Per-user breakdown
    per_user = {}
    for r in usage_records:
        uid = r["user_id"]
        if uid not in per_user:
            per_user[uid] = {
                "user_id": uid,
                "username": r["username"],
                "display_name": r["display_name"],
                "request_count": 0,
                "token_count": 0,
            }
        per_user[uid]["request_count"] += r["request_count"]
        per_user[uid]["token_count"] += r["token_count"]

    # Per-endpoint breakdown
    per_endpoint = {}
    for r in usage_records:
        eid = r["endpoint_id"]
        if eid not in per_endpoint:
            per_endpoint[eid] = {
                "endpoint_id": eid,
                "endpoint_name": r["endpoint_name"],
                "request_count": 0,
                "token_count": 0,
            }
        per_endpoint[eid]["request_count"] += r["request_count"]
        per_endpoint[eid]["token_count"] += r["token_count"]

    return {
        "period": period,
        "period_start": period_start,
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "per_user": list(per_user.values()),
        "per_endpoint": list(per_endpoint.values()),
        "records": usage_records,
    }


@router.delete("/admin/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def admin_delete_session(
    session_id: int,
    admin: dict = Depends(get_current_admin_or_owner),
    db=Depends(get_db),
):
    """Delete any user's AI chat session (admin/owner only, with permission check)."""
    cursor = await db.execute("SELECT * FROM ai_chat_sessions WHERE id = ?", (session_id,))
    session = await cursor.fetchone()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session = dict(session)

    config = _load_config()
    if admin["role"] == "admin" and not config.get("admins_can_delete_ai_chats", False):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin permission to delete AI chats is not enabled. Contact the owner.",
        )

    # Also delete the messages (cascade)
    await db.execute("DELETE FROM ai_chat_sessions WHERE id = ?", (session_id,))
    await db.commit()
    return None


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    # Track last_accessed_at timestamp for each config
    await db.execute(
        "UPDATE ai_endpoint_configs SET last_accessed_at = CURRENT_TIMESTAMP WHERE endpoint_id = ?",
        (endpoint_id,),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_configs WHERE endpoint_id = ? ORDER BY name",
        (endpoint_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.post("/admin/endpoints/{endpoint_id}/configs", status_code=status.HTTP_201_CREATED)
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuration not found")
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
               status_code=status.HTTP_204_NO_CONTENT)
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Configuration not found")

    await db.execute("DELETE FROM ai_endpoint_configs WHERE id = ?", (config_id,))
    await db.commit()
    return None


# ===========================================================================
# SECTION B — USER: My Endpoints (Self-Service)
# ===========================================================================


async def _verify_endpoint_ownership(endpoint_id: int, user: dict, db):
    """Verify the current user owns the endpoint. Returns endpoint dict."""
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    ep = await cursor.fetchone()
    if ep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    ep = dict(ep)
    if ep["owner_user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not own this endpoint")
    return ep


@router.get("/endpoints/mine")
async def my_endpoints(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return all endpoints owned by the current user, with models and shared user info."""
    cursor = await db.execute(
        "SELECT * FROM ai_endpoints WHERE owner_user_id = ? AND is_admin_endpoint = 0 ORDER BY created_at DESC",
        (user["id"],),
    )
    endpoints = [dict(row) for row in await cursor.fetchall()]

    for ep in endpoints:
        ep["api_key_encrypted"] = _mask_api_key(ep["api_key_encrypted"])
        ep["api_key_masked"] = True

        # Models
        cursor = await db.execute(
            "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? ORDER BY model_name",
            (ep["id"],),
        )
        ep["models"] = [dict(m) for m in await cursor.fetchall()]

        # Shared users
        cursor = await db.execute(
            "SELECT aeu.*, u.username, u.display_name, u.role "
            "FROM ai_endpoint_users aeu JOIN users u ON aeu.user_id = u.id "
            "WHERE aeu.endpoint_id = ?",
            (ep["id"],),
        )
        ep["shared_users"] = [dict(u) for u in await cursor.fetchall()]

    return endpoints


@router.post("/endpoints/mine", status_code=status.HTTP_201_CREATED)
async def my_create_endpoint(
    body: AIEndpointCreate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a new AI endpoint owned by the current user."""
    encrypted_key = _encrypt_api_key(body.api_key)
    cursor = await db.execute(
        "INSERT INTO ai_endpoints (owner_user_id, name, base_url, api_key_encrypted, is_admin_endpoint) VALUES (?, ?, ?, ?, 0)",
        (user["id"], body.name, body.base_url, encrypted_key),
    )
    await db.commit()
    ep_id = cursor.lastrowid

    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (ep_id,))
    ep = dict(await cursor.fetchone())
    ep["api_key_encrypted"] = _mask_api_key(ep["api_key_encrypted"])
    ep["api_key_masked"] = True
    return ep


@router.put("/endpoints/mine/{endpoint_id}")
async def my_update_endpoint(
    endpoint_id: int,
    body: AIEndpointUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Update own endpoint."""
    ep = await _verify_endpoint_ownership(endpoint_id, user, db)

    new_name = body.name if body.name is not None else ep["name"]
    new_url = body.base_url if body.base_url is not None else ep["base_url"]
    new_key = ep["api_key_encrypted"]

    if body.api_key is not None and body.api_key != "":
        new_key = _encrypt_api_key(body.api_key)

    await db.execute(
        "UPDATE ai_endpoints SET name = ?, base_url = ?, api_key_encrypted = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_name, new_url, new_key, endpoint_id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    updated = dict(await cursor.fetchone())
    updated["api_key_encrypted"] = _mask_api_key(updated["api_key_encrypted"])
    updated["api_key_masked"] = True
    return updated


@router.delete("/endpoints/mine/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def my_delete_endpoint(
    endpoint_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Delete own endpoint."""
    await _verify_endpoint_ownership(endpoint_id, user, db)
    await db.execute("DELETE FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    await db.commit()
    return None


@router.post("/endpoints/mine/{endpoint_id}/fetch-models")
async def my_fetch_models(
    endpoint_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Fetch models from upstream for own endpoint."""
    ep = await _verify_endpoint_ownership(endpoint_id, user, db)

    api_key = _decrypt_api_key(ep["api_key_encrypted"])
    models_url = ep["base_url"].rstrip("/") + "/models"

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                models_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Upstream returned {resp.status_code}: {resp.text[:500]}",
                )
            data = resp.json()
    except httpx.RequestError as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to connect to upstream: {str(e)}",
        )

    model_list = data.get("data", [])
    fetched_names = []
    new_count = 0

    for item in model_list:
        model_name = item.get("id") or item.get("name")
        if not model_name:
            continue
        fetched_names.append(model_name)

        cursor = await db.execute(
            "SELECT id FROM ai_endpoint_models WHERE endpoint_id = ? AND model_name = ?",
            (endpoint_id, model_name),
        )
        existing = await cursor.fetchone()
        if existing is None:
            await db.execute(
                "INSERT INTO ai_endpoint_models (endpoint_id, model_name) VALUES (?, ?)",
                (endpoint_id, model_name),
            )
            new_count += 1

    await db.commit()
    return {"fetched_models": fetched_names, "new_count": new_count, "total": len(fetched_names)}


@router.get("/endpoints/mine/{endpoint_id}/models")
async def my_get_models(
    endpoint_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get models for own endpoint."""
    await _verify_endpoint_ownership(endpoint_id, user, db)
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? ORDER BY model_name",
        (endpoint_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.put("/endpoints/mine/{endpoint_id}/models/{model_name}")
async def my_update_model(
    endpoint_id: int,
    model_name: str,
    body: AIEndpointModelUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Update model settings for own endpoint."""
    await _verify_endpoint_ownership(endpoint_id, user, db)

    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? AND model_name = ?",
        (endpoint_id, model_name),
    )
    model = await cursor.fetchone()
    if model is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not found for this endpoint")
    model = dict(model)

    enabled = 1 if body.enabled is True else (0 if body.enabled is False else model["enabled"])
    req_mult = body.multiplier_requests if body.multiplier_requests is not None else model["multiplier_requests"]
    tok_mult = body.multiplier_tokens if body.multiplier_tokens is not None else model["multiplier_tokens"]
    max_ctx = body.max_context_tokens if body.max_context_tokens is not None else model["max_context_tokens"]

    await db.execute(
        "UPDATE ai_endpoint_models SET enabled = ?, multiplier_requests = ?, multiplier_tokens = ?, max_context_tokens = ? "
        "WHERE endpoint_id = ? AND model_name = ?",
        (enabled, req_mult, tok_mult, max_ctx, endpoint_id, model_name),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? AND model_name = ?",
        (endpoint_id, model_name),
    )
    return dict(await cursor.fetchone())


@router.get("/endpoints/mine/{endpoint_id}/users")
async def my_get_endpoint_users(
    endpoint_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get shared users for own endpoint."""
    await _verify_endpoint_ownership(endpoint_id, user, db)
    cursor = await db.execute(
        "SELECT aeu.*, u.username, u.display_name, u.role "
        "FROM ai_endpoint_users aeu JOIN users u ON aeu.user_id = u.id "
        "WHERE aeu.endpoint_id = ?",
        (endpoint_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.post("/endpoints/mine/{endpoint_id}/users", status_code=status.HTTP_201_CREATED)
async def my_share_endpoint(
    endpoint_id: int,
    body: AIEndpointUserAssign,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Share own endpoint with another user."""
    await _verify_endpoint_ownership(endpoint_id, user, db)

    # Verify target user exists
    cursor = await db.execute("SELECT id FROM users WHERE id = ?", (body.user_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Check for existing assignment
    cursor = await db.execute(
        "SELECT id FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?",
        (endpoint_id, body.user_id),
    )
    if await cursor.fetchone() is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User already assigned to this endpoint")

    await db.execute(
        "INSERT INTO ai_endpoint_users (endpoint_id, user_id, limit_type, limit_value_requests, "
        "limit_value_tokens, reset_schedule, reset_time, is_shared_pool, shared_pool_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            endpoint_id,
            body.user_id,
            body.limit_type,
            body.limit_value_requests,
            body.limit_value_tokens,
            body.reset_schedule,
            body.reset_time,
            1 if body.is_shared_pool else 0,
            body.shared_pool_id,
        ),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT aeu.*, u.username, u.display_name, u.role "
        "FROM ai_endpoint_users aeu JOIN users u ON aeu.user_id = u.id "
        "WHERE aeu.endpoint_id = ? AND aeu.user_id = ?",
        (endpoint_id, body.user_id),
    )
    return dict(await cursor.fetchone())


@router.put("/endpoints/mine/{endpoint_id}/users/{target_user_id}")
async def my_update_shared_user(
    endpoint_id: int,
    target_user_id: int,
    body: AIEndpointUserUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Update shared user's limits for own endpoint."""
    await _verify_endpoint_ownership(endpoint_id, user, db)

    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?",
        (endpoint_id, target_user_id),
    )
    assignment = await cursor.fetchone()
    if assignment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User assignment not found")
    assignment = dict(assignment)

    new_limit_type = body.limit_type if body.limit_type is not None else assignment["limit_type"]
    new_limit_val_req = body.limit_value_requests if body.limit_value_requests is not None else assignment["limit_value_requests"]
    new_limit_val_tok = body.limit_value_tokens if body.limit_value_tokens is not None else assignment["limit_value_tokens"]
    new_schedule = body.reset_schedule if body.reset_schedule is not None else assignment["reset_schedule"]
    new_reset_time = body.reset_time if body.reset_time is not None else assignment["reset_time"]
    new_shared = 1 if body.is_shared_pool is True else (0 if body.is_shared_pool is False else assignment["is_shared_pool"])

    await db.execute(
        "UPDATE ai_endpoint_users SET limit_type = ?, limit_value_requests = ?, limit_value_tokens = ?, "
        "reset_schedule = ?, reset_time = ?, is_shared_pool = ? WHERE endpoint_id = ? AND user_id = ?",
        (new_limit_type, new_limit_val_req, new_limit_val_tok, new_schedule, new_reset_time, new_shared, endpoint_id, target_user_id),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT aeu.*, u.username, u.display_name, u.role "
        "FROM ai_endpoint_users aeu JOIN users u ON aeu.user_id = u.id "
        "WHERE aeu.endpoint_id = ? AND aeu.user_id = ?",
        (endpoint_id, target_user_id),
    )
    return dict(await cursor.fetchone())


@router.delete("/endpoints/mine/{endpoint_id}/users/{target_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def my_unshare_endpoint(
    endpoint_id: int,
    target_user_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Unshare own endpoint from a user."""
    await _verify_endpoint_ownership(endpoint_id, user, db)

    cursor = await db.execute(
        "SELECT id FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?",
        (endpoint_id, target_user_id),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User assignment not found")

    await db.execute("DELETE FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?", (endpoint_id, target_user_id))
    await db.commit()
    return None


# ===========================================================================
# SECTION C — USER: Available Endpoints for Chat
# ===========================================================================


@router.get("/endpoints/available")
async def available_endpoints(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return all endpoints the current user can use, grouped by source."""
    result = {"mine": [], "server": [], "shared_by_username": []}
    seen_ids = set()

    # 1. Endpoints I own ("mine")
    cursor = await db.execute(
        "SELECT * FROM ai_endpoints WHERE owner_user_id = ? ORDER BY name",
        (user["id"],),
    )
    for ep in [dict(r) for r in await cursor.fetchall()]:
        if ep["id"] in seen_ids:
            continue
        seen_ids.add(ep["id"])

        ep["api_key_encrypted"] = _mask_api_key(ep["api_key_encrypted"])
        ep["api_key_masked"] = True

        cursor = await db.execute(
            "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? AND enabled = 1 ORDER BY model_name",
            (ep["id"],),
        )
        ep["models"] = [dict(m) for m in await cursor.fetchall()]

        cursor = await db.execute(
            "SELECT * FROM ai_endpoint_configs WHERE endpoint_id = ? ORDER BY name",
            (ep["id"],),
        )
        ep["configs"] = [dict(c) for c in await cursor.fetchall()]

        result["mine"].append(ep)

    # 2. Endpoints created by admin/owner that I'm assigned to ("server")
    # 2a. Direct user assignments
    cursor = await db.execute(
        "SELECT aeu.endpoint_id FROM ai_endpoint_users aeu WHERE aeu.user_id = ?",
        (user["id"],),
    )
    assigned_ids = set(r["endpoint_id"] for r in await cursor.fetchall())

    # 2b. Config-based assignments (via named config presets)
    cursor = await db.execute(
        "SELECT DISTINCT aec.endpoint_id FROM ai_endpoint_configs aec "
        "JOIN ai_endpoint_config_users aecu ON aec.id = aecu.config_id "
        "WHERE aecu.user_id = ?",
        (user["id"],),
    )
    for r in await cursor.fetchall():
        assigned_ids.add(r["endpoint_id"])

    assigned_ids = list(assigned_ids)

    for eid in assigned_ids:
        if eid in seen_ids:
            continue
        cursor = await db.execute(
            "SELECT e.*, u.username as owner_username FROM ai_endpoints e "
            "JOIN users u ON e.owner_user_id = u.id WHERE e.id = ?",
            (eid,),
        )
        ep = await cursor.fetchone()
        if ep is None:
            continue
        ep = dict(ep)
        seen_ids.add(ep["id"])

        ep["api_key_encrypted"] = _mask_api_key(ep["api_key_encrypted"])
        ep["api_key_masked"] = True

        cursor = await db.execute(
            "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? AND enabled = 1 ORDER BY model_name",
            (ep["id"],),
        )
        ep["models"] = [dict(m) for m in await cursor.fetchall()]

        cursor = await db.execute(
            "SELECT * FROM ai_endpoint_configs WHERE endpoint_id = ? ORDER BY name",
            (ep["id"],),
        )
        ep["configs"] = [dict(c) for c in await cursor.fetchall()]

        owner = ep.get("owner_username", "")
        owner_role_cursor = await db.execute("SELECT role FROM users WHERE username = ?", (owner,))
        owner_row = await owner_role_cursor.fetchone()
        if owner_row and owner_row["role"] in ("admin", "owner"):
            result["server"].append(ep)
        else:
            # Shared by another regular user
            ep["shared_by"] = owner
            result["shared_by_username"].append(ep)

    return result


# ===========================================================================
# SECTION D — CHAT: Sessions & Messages
# ===========================================================================


@router.get("/sessions")
async def list_sessions(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """List all chat sessions for the current user, most recent first."""
    cursor = await db.execute(
        "SELECT s.*, ae.name as endpoint_name FROM ai_chat_sessions s "
        "LEFT JOIN ai_endpoints ae ON s.endpoint_id = ae.id "
        "WHERE s.user_id = ? ORDER BY s.updated_at DESC",
        (user["id"],),
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: AIChatSessionCreate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a new chat session."""
    # Verify endpoint exists
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (body.endpoint_id,))
    ep = await cursor.fetchone()
    if ep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")

    # Verify model exists and is enabled
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? AND model_name = ? AND enabled = 1",
        (body.endpoint_id, body.model_name),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not available or not enabled for this endpoint")

    context_json = json.dumps(body.context_selection) if body.context_selection else "{}"

    cursor = await db.execute(
        "INSERT INTO ai_chat_sessions (user_id, endpoint_id, model_name, title, context_selection, config_id) VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], body.endpoint_id, body.model_name, body.title, context_json, body.config_id),
    )
    await db.commit()
    session_id = cursor.lastrowid

    cursor = await db.execute(
        "SELECT s.*, ae.name as endpoint_name FROM ai_chat_sessions s "
        "LEFT JOIN ai_endpoints ae ON s.endpoint_id = ae.id WHERE s.id = ?",
        (session_id,),
    )
    return dict(await cursor.fetchone())


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get a single session with its messages."""
    cursor = await db.execute(
        "SELECT s.*, ae.name as endpoint_name FROM ai_chat_sessions s "
        "LEFT JOIN ai_endpoints ae ON s.endpoint_id = ae.id WHERE s.id = ?",
        (session_id,),
    )
    session = await cursor.fetchone()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session = dict(session)

    if session["user_id"] != user["id"] and user["role"] not in ("admin", "owner"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your session")

    cursor = await db.execute(
        "SELECT * FROM ai_chat_messages WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    )
    session["messages"] = [dict(r) for r in await cursor.fetchall()]
    return session


@router.put("/sessions/{session_id}")
async def update_session(
    session_id: int,
    body: AIChatSessionUpdate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Update a chat session's title or context_selection."""
    cursor = await db.execute("SELECT * FROM ai_chat_sessions WHERE id = ?", (session_id,))
    session = await cursor.fetchone()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session = dict(session)

    if session["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your session")

    new_title = body.title if body.title is not None else session["title"]
    new_context = json.dumps(body.context_selection) if body.context_selection is not None else session["context_selection"]

    await db.execute(
        "UPDATE ai_chat_sessions SET title = ?, context_selection = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_title, new_context, session_id),
    )
    await db.commit()

    cursor = await db.execute(
        "SELECT s.*, ae.name as endpoint_name FROM ai_chat_sessions s "
        "LEFT JOIN ai_endpoints ae ON s.endpoint_id = ae.id WHERE s.id = ?",
        (session_id,),
    )
    return dict(await cursor.fetchone())


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Delete a chat session. Current user (owner) or admin/owner with permission."""
    cursor = await db.execute("SELECT * FROM ai_chat_sessions WHERE id = ?", (session_id,))
    session = await cursor.fetchone()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session = dict(session)

    # If admin/owner (not the session owner), check permission
    if session["user_id"] != user["id"]:
        if user["role"] in ("admin", "owner"):
            config = _load_config()
            if user["role"] == "admin" and not config.get("admins_can_delete_ai_chats", False):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Admin permission to delete AI chats is not enabled.",
                )
            # Owner can always delete
        else:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your session")

    await db.execute("DELETE FROM ai_chat_sessions WHERE id = ?", (session_id,))
    await db.commit()
    return None


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(
    session_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return all messages for a session ordered by created_at."""
    cursor = await db.execute("SELECT * FROM ai_chat_sessions WHERE id = ?", (session_id,))
    session = await cursor.fetchone()
    if session is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    session = dict(session)

    if session["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your session")

    cursor = await db.execute(
        "SELECT * FROM ai_chat_messages WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    )
    return [dict(r) for r in await cursor.fetchall()]


# ===========================================================================
# SECTION E — CHAT PROXY: Core Streaming Endpoint
# ===========================================================================


@router.post("/chat")
async def ai_chat(
    body: AIChatRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Main AI chat endpoint with SSE streaming proxy."""
    now = datetime.now(timezone.utc)

    # --- Resolve session / endpoint / model ---
    if body.session_id is not None:
        cursor = await db.execute("SELECT * FROM ai_chat_sessions WHERE id = ?", (body.session_id,))
        session = await cursor.fetchone()
        if session is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
        session = dict(session)
        if session["user_id"] != user["id"]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your session")
        endpoint_id = session["endpoint_id"]
        model_name = session["model_name"]
        session_id = session["id"]
        context_selection_raw = session.get("context_selection", "{}")
    else:
        if body.endpoint_id is None or body.model_name is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Must provide either session_id or both endpoint_id and model_name",
            )
        endpoint_id = body.endpoint_id
        model_name = body.model_name
        session_id = None
        context_selection_raw = json.dumps(body.context_selection) if body.context_selection else "{}"

    # --- Verify endpoint exists ---
    cursor = await db.execute("SELECT * FROM ai_endpoints WHERE id = ?", (endpoint_id,))
    ep = await cursor.fetchone()
    if ep is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Endpoint not found")
    ep = dict(ep)

    # --- Verify user has access to this endpoint ---
    # Check: user owns it, or has an assignment, or is admin/owner
    has_access = False
    if ep["owner_user_id"] == user["id"]:
        has_access = True
    elif user["role"] in ("admin", "owner"):
        has_access = True
    else:
        cursor = await db.execute(
            "SELECT id FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?",
            (endpoint_id, user["id"]),
        )
        if await cursor.fetchone() is not None:
            has_access = True

    if not has_access:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have access to this endpoint")

    # --- Verify model is enabled ---
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_models WHERE endpoint_id = ? AND model_name = ? AND enabled = 1",
        (endpoint_id, model_name),
    )
    model_row = await cursor.fetchone()
    if model_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Model not available or not enabled for this endpoint")

    # --- Check usage limits ---
    limit_error = await check_usage_limits(db, endpoint_id, user["id"], model_name)
    if limit_error is not None:
        # Extract the human-readable error message from the dict
        err_msg = limit_error.get("error", "Request limit reached")
        raise HTTPException(status_code=429, detail=err_msg)

    # --- Build system prompt context from context_selection ---
    context_text = ""
    try:
        ctx_sel = json.loads(context_selection_raw)
    except (json.JSONDecodeError, TypeError):
        ctx_sel = {}

    if ctx_sel:
        chapter_ids = ctx_sel.get("chapter_ids", []) or []
        wiki_ids = ctx_sel.get("wiki_ids", []) or []
        volume_ids = ctx_sel.get("volume_ids", []) or []

        context_parts = []

        # Fetch chapter content by IDs
        if chapter_ids:
            for cid in chapter_ids:
                cursor = await db.execute("SELECT title, content FROM chapters WHERE id = ?", (cid,))
                ch = await cursor.fetchone()
                if ch:
                    ch = dict(ch)
                    plain = _strip_html(ch.get("content", ""))
                    context_parts.append(f"[Chapter: {ch.get('title', 'Untitled')}]\n{plain}")

        # Fetch wiki entry content by IDs
        if wiki_ids:
            for wid in wiki_ids:
                cursor = await db.execute("SELECT name, content FROM wiki_entries WHERE id = ?", (wid,))
                w = await cursor.fetchone()
                if w:
                    w = dict(w)
                    plain = _strip_html(w.get("content", ""))
                    context_parts.append(f"[Wiki: {w.get('name', 'Unknown')}]\n{plain}")

        # Fetch volume chapters by volume IDs
        if volume_ids:
            for vid in volume_ids:
                cursor = await db.execute(
                    "SELECT title, content FROM chapters WHERE volume_id = ? ORDER BY position ASC",
                    (vid,),
                )
                vol_chapters = await cursor.fetchall()
                if vol_chapters:
                    cursor_vol = await db.execute("SELECT title FROM volumes WHERE id = ?", (vid,))
                    vol = await cursor_vol.fetchone()
                    vol_title = dict(vol)["title"] if vol else f"Volume {vid}"
                    context_parts.append(f"[Volume: {vol_title}]")
                    for vc in vol_chapters:
                        vc = dict(vc)
                        plain = _strip_html(vc.get("content", ""))
                        context_parts.append(f"Chapter: {vc.get('title', 'Untitled')}\n{plain}")

        if context_parts:
            context_text = "\n\n".join(context_parts)

    system_content = (
        "You are an AI writing assistant helping with a novel."
        + (f" Here is the relevant context:\n\n{context_text}\n\nPlease respond helpfully based on this context." if context_text else " Please respond helpfully.")
    )

    # --- Save user message ---
    user_msg_token_count = get_token_count(body.message)

    # Create session if needed
    if session_id is None:
        auto_title = body.message[:50].strip() + ("..." if len(body.message) > 50 else "") or "New Chat"
        cursor = await db.execute(
            "INSERT INTO ai_chat_sessions (user_id, endpoint_id, model_name, title, context_selection, config_id) VALUES (?, ?, ?, ?, ?, ?)",
            (user["id"], endpoint_id, model_name, auto_title, context_selection_raw, body.config_id),
        )
        await db.commit()
        session_id = cursor.lastrowid

    await db.execute(
        "INSERT INTO ai_chat_messages (session_id, role, content, token_count) VALUES (?, 'user', ?, ?)",
        (session_id, body.message, user_msg_token_count),
    )
    # Update session timestamp
    await db.execute("UPDATE ai_chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
    await db.commit()

    # --- Build message array for upstream ---
    # Load existing messages for this session
    cursor = await db.execute(
        "SELECT role, content FROM ai_chat_messages WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    )
    existing_messages = [dict(r) for r in await cursor.fetchall()]

    upstream_messages = [{"role": "system", "content": system_content}]
    for msg in existing_messages:
        upstream_messages.append({"role": msg["role"], "content": msg["content"]})

    # --- Decrypt API key ---
    api_key = _decrypt_api_key(ep["api_key_encrypted"])
    base_url = ep["base_url"].rstrip("/")
    chat_url = f"{base_url}/chat/completions"

    # --- Compute period start for usage tracking ---
    # Get assignment for period info
    cursor = await db.execute(
        "SELECT * FROM ai_endpoint_users WHERE endpoint_id = ? AND user_id = ?",
        (endpoint_id, user["id"]),
    )
    assignment = await cursor.fetchone()
    if assignment:
        assignment = dict(assignment)
        period_start_str = get_period_start(now, assignment.get("reset_schedule", "daily"), assignment.get("reset_time"))
    else:
        period_start_str = get_period_start(now, "daily", None)

    # --- SSE Streaming generator ---
    async def sse_generator():
        full_content = ""
        reasoning_content = ""
        usage_info = None

        # Build reasoning/thinking parameters for upstream API based on model name
        reasoning_params = {}
        extra_body_params = {}
        for entry in REASONING_MAP:
            if re.search(entry["pattern"], model_name, re.IGNORECASE):
                params = entry["enable"] if body.reasoning else entry["disable"]
                if entry.get("extra_body_wrap"):
                    extra_body_params = params
                else:
                    reasoning_params = params
                break

        # Build the JSON payload for the upstream request
        request_json = {
            "model": model_name,
            "messages": upstream_messages,
            "stream": True,
        }
        if reasoning_params:
            request_json.update(reasoning_params)
        if extra_body_params:
            request_json["extra_body"] = extra_body_params

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    chat_url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_json,
                ) as response:
                    if response.status_code != 200:
                        try:
                            error_body = await response.aread()
                            error_text = error_body.decode()[:500]
                        except Exception:
                            error_text = f"Status {response.status_code}"
                        yield f"data: {{\"error\": \"Upstream error: {response.status_code}\", \"detail\": {json.dumps(error_text)}}}\n\n"
                        return

                    reasoning_enabled = bool(body.reasoning)
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]
                            if data_str == "[DONE]":
                                yield "data: [DONE]\n\n"
                                break
                            try:
                                data = json.loads(data_str)
                                choices = data.get("choices", [])
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    content = delta.get("content", "")
                                    reasoning = delta.get("reasoning_content", "")
                                    if reasoning:
                                        reasoning_content += reasoning
                                    if content:
                                        full_content += content
                                    # When reasoning is disabled, strip reasoning_content from the chunk
                                    # before forwarding to the frontend. Some upstream APIs always return
                                    # reasoning_content regardless of request params, causing slowdowns.
                                    if not reasoning_enabled and "reasoning_content" in delta:
                                        modified_delta = {k: v for k, v in delta.items() if k != "reasoning_content"}
                                        choices[0]["delta"] = modified_delta
                                        data["choices"] = choices
                                        modified_line = "data: " + json.dumps(data, separators=(",", ":"))
                                        yield f"{modified_line}\n\n"
                                    else:
                                        yield f"{line}\n\n"
                                else:
                                    yield f"{line}\n\n"
                                if "usage" in data:
                                    usage_info = data["usage"]
                            except json.JSONDecodeError:
                                yield f"{line}\n\n"
                        else:
                            yield f"{line}\n"
        except httpx.RequestError as e:
            yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"
        except Exception as e:
            yield f"data: {{\"error\": \"{str(e)}\"}}\n\n"
        finally:
            # Schedule background save
            if full_content or usage_info:
                token_count = 0
                if usage_info:
                    token_count = usage_info.get("total_tokens", 0) or usage_info.get("completion_tokens", 0)
                if token_count == 0:
                    token_count = get_token_count(full_content)

                background_tasks.add_task(
                    save_completion,
                    DB_PATH,
                    session_id,
                    full_content,
                    token_count,
                    endpoint_id,
                    user["id"],
                    model_name,
                    period_start_str,
                    reasoning_content,
                )

    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ===========================================================================
# SECTION F — USAGE: Current User
# ===========================================================================


@router.get("/usage")
async def my_usage(
    period: str = "daily",
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return the current user's usage breakdown."""
    now = datetime.now(timezone.utc)
    if period == "daily":
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "weekly":
        days_since = now.weekday()
        period_start = (now - timedelta(days=days_since)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    elif period == "monthly":
        period_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    else:
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

    cursor = await db.execute(
        "SELECT au.*, ae.name as endpoint_name, ae.owner_user_id "
        "FROM ai_usage au "
        "JOIN ai_endpoints ae ON au.endpoint_id = ae.id "
        "WHERE au.user_id = ? AND au.period_start = ? ORDER BY au.recorded_at DESC",
        (user["id"], period_start),
    )
    records = [dict(r) for r in await cursor.fetchall()]

    total_requests = sum(r["request_count"] for r in records)
    total_tokens = sum(r["token_count"] for r in records)

    # Per-endpoint breakdown
    per_endpoint = {}
    for r in records:
        eid = r["endpoint_id"]
        if eid not in per_endpoint:
            per_endpoint[eid] = {
                "endpoint_id": eid,
                "endpoint_name": r["endpoint_name"],
                "request_count": 0,
                "token_count": 0,
            }
        per_endpoint[eid]["request_count"] += r["request_count"]
        per_endpoint[eid]["token_count"] += r["token_count"]

    # Per-model breakdown
    per_model = {}
    for r in records:
        mn = r["model_name"]
        if mn not in per_model:
            per_model[mn] = {"model_name": mn, "request_count": 0, "token_count": 0}
        per_model[mn]["request_count"] += r["request_count"]
        per_model[mn]["token_count"] += r["token_count"]

    # Remaining limits for each endpoint the user has access to
    limits_remaining = []
    cursor = await db.execute(
        "SELECT aeu.*, ae.name as endpoint_name FROM ai_endpoint_users aeu "
        "JOIN ai_endpoints ae ON aeu.endpoint_id = ae.id WHERE aeu.user_id = ?",
        (user["id"],),
    )
    assignments = [dict(r) for r in await cursor.fetchall()]

    for a in assignments:
        a_eid = a["endpoint_id"]
        # Get all models for this endpoint with multipliers
        cursor = await db.execute(
            "SELECT model_name, multiplier_requests, multiplier_tokens FROM ai_endpoint_models WHERE endpoint_id = ? AND enabled = 1",
            (a_eid,),
        )
        models = [dict(m) for m in await cursor.fetchall()]

        for m in models:
            # Calculate used for this user/endpoint/model/period
            limit_type = a.get("limit_type", "requests")
            limit_req = a.get("limit_value_requests")
            limit_tok = a.get("limit_value_tokens")
            req_mult = float(m.get("multiplier_requests", 1.0))
            tok_mult = float(m.get("multiplier_tokens", 1.0))

            # Get used
            cursor = await db.execute(
                "SELECT COALESCE(SUM(request_count),0) as req, COALESCE(SUM(token_count),0) as tok "
                "FROM ai_usage WHERE endpoint_id = ? AND user_id = ? AND model_name = ? AND period_start = ?",
                (a_eid, user["id"], m["model_name"], period_start),
            )
            used_row = await cursor.fetchone()
            used = dict(used_row) if used_row else {"req": 0, "tok": 0}

            limits_remaining.append({
                "endpoint_id": a_eid,
                "endpoint_name": a.get("endpoint_name", ""),
                "model_name": m["model_name"],
                "limit_type": limit_type,
                "requests_used": used["req"],
                "requests_limit": limit_req,
                "requests_remaining": max(0, (limit_req - int(used["req"] * req_mult))) if limit_req is not None else None,
                "tokens_used": used["tok"],
                "tokens_limit": limit_tok,
                "tokens_remaining": max(0, (limit_tok - int(used["tok"] * tok_mult))) if limit_tok is not None else None,
                "reset_schedule": a.get("reset_schedule", "daily"),
            })

    return {
        "period": period,
        "period_start": period_start,
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "per_endpoint": list(per_endpoint.values()),
        "per_model": list(per_model.values()),
        "limits_remaining": limits_remaining,
        "records": records,
    }
