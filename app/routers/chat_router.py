import json
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth import get_current_admin_or_owner, get_current_user
from app.database import get_db
from app.models import DMMessageSend, GlobalMessageSend, GroupCreate, GroupInvite, GroupMessageSend
from app.ws_manager import chat_manager

# ---------------------------------------------------------------------------
# Admin config loader (replicated from admin_router / ai_router to avoid
# circular imports)
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
    "admins_can_delete_global_chat": False,
}


def _load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    return dict(DEFAULT_CONFIG)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_user_display(user_dict: dict) -> str:
    """Return display_name if set, otherwise fall back to username."""
    return user_dict.get("display_name") or user_dict.get("username", "Unknown")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/chat")


# ===========================================================================
# SECTION A — Direct Messages
# ===========================================================================


@router.get("/dm/contacts")
async def dm_contacts(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Return list of users the current user has exchanged DMs with,
    including last-message preview, ordered by most recent message first."""
    user_id = user["id"]

    # 1. Find all distinct contact IDs
    cursor = await db.execute(
        """
        SELECT DISTINCT
            CASE WHEN sender_id = ? THEN recipient_id ELSE sender_id END AS contact_id
        FROM dm_messages
        WHERE (sender_id = ? OR recipient_id = ?) AND is_deleted = 0
        """,
        (user_id, user_id, user_id),
    )
    contact_rows = await cursor.fetchall()
    contact_ids = [row["contact_id"] for row in contact_rows]

    if not contact_ids:
        return []

    # 2. For each contact, fetch user info + last message
    contacts = []
    for cid in contact_ids:
        cursor = await db.execute(
            "SELECT id, username, display_name FROM users WHERE id = ?", (cid,)
        )
        contact_user = await cursor.fetchone()
        if contact_user is None:
            continue
        contact_user = dict(contact_user)

        cursor = await db.execute(
            """
            SELECT content, created_at FROM dm_messages
            WHERE ((sender_id = ? AND recipient_id = ?)
                OR (sender_id = ? AND recipient_id = ?))
              AND is_deleted = 0
            ORDER BY created_at DESC LIMIT 1
            """,
            (user_id, cid, cid, user_id),
        )
        last_msg = await cursor.fetchone()

        contacts.append(
            {
                "id": contact_user["id"],
                "user_id": contact_user["id"],
                "username": contact_user["username"],
                "display_name": contact_user["display_name"],
                "last_message": last_msg["content"][:100] if last_msg else None,
                "last_time": last_msg["created_at"] if last_msg else None,
            }
        )

    # Sort by last_time descending (most recent first); None sorts last
    contacts.sort(key=lambda c: c["last_time"] or "", reverse=True)
    return contacts


@router.get("/dm/{user_id}")
async def dm_history(
    user_id: int,
    since: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get DM history between current user and *user_id*, oldest first."""
    current_id = user["id"]

    # Verify the other user exists
    cursor = await db.execute("SELECT id FROM users WHERE id = ?", (user_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    where = (
        "((sender_id = ? AND recipient_id = ?) OR (sender_id = ? AND recipient_id = ?))"
        " AND is_deleted = 0"
    )
    params: list = [current_id, user_id, user_id, current_id]

    if since is not None:
        where += " AND id > ?"
        params.append(since)

    params.append(limit)

    cursor = await db.execute(
        f"SELECT * FROM dm_messages WHERE {where} ORDER BY created_at ASC LIMIT ?",
        params,
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.post("/dm", status_code=status.HTTP_201_CREATED)
async def dm_send(
    body: DMMessageSend,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Send a direct message to another user."""
    if body.recipient_id == user["id"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot send a message to yourself.",
        )

    # Verify recipient exists
    cursor = await db.execute("SELECT id FROM users WHERE id = ?", (body.recipient_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Recipient not found")

    now = _iso_now()
    cursor = await db.execute(
        "INSERT INTO dm_messages (sender_id, recipient_id, content, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], body.recipient_id, body.content, now),
    )
    await db.commit()
    msg_id = cursor.lastrowid

    cursor = await db.execute("SELECT * FROM dm_messages WHERE id = ?", (msg_id,))
    msg_dict = dict(await cursor.fetchone())

    # Broadcast via WebSocket to both sender and recipient
    sender_data = {**msg_dict, "recipient_id": body.recipient_id}
    recipient_data = {**msg_dict, "recipient_id": user["id"]}
    await chat_manager.broadcast(
        f"dm:{user['id']}",
        {"type": "new_message", "channel": "dm", "other_user_id": body.recipient_id, "msg": sender_data},
    )
    await chat_manager.broadcast(
        f"dm:{body.recipient_id}",
        {"type": "new_message", "channel": "dm", "other_user_id": user["id"], "msg": recipient_data},
    )

    return msg_dict


@router.delete("/dm/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
async def dm_delete(
    message_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Soft-delete a DM message. Can only delete own messages."""
    cursor = await db.execute(
        "SELECT * FROM dm_messages WHERE id = ? AND is_deleted = 0", (message_id,)
    )
    msg = await cursor.fetchone()
    if msg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    msg = dict(msg)

    if msg["sender_id"] != user["id"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You can only delete your own messages.",
        )

    await db.execute("UPDATE dm_messages SET is_deleted = 1 WHERE id = ?", (message_id,))
    await db.commit()
    return None


# ===========================================================================
# SECTION B — Group Chat
# ===========================================================================


@router.post("/groups", status_code=status.HTTP_201_CREATED)
async def group_create(
    body: GroupCreate,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Create a group chat. Creator is automatically an accepted member;
    invited members are set to pending."""
    now = _iso_now()

    # Create the group
    cursor = await db.execute(
        "INSERT INTO chat_groups (name, creator_id, created_at) VALUES (?, ?, ?)",
        (body.name, user["id"], now),
    )
    await db.commit()
    group_id = cursor.lastrowid

    # Add creator as accepted
    await db.execute(
        "INSERT INTO chat_group_members (group_id, user_id, status, joined_at) VALUES (?, ?, 'accepted', ?)",
        (group_id, user["id"], now),
    )

    # Add invited members as pending (skip duplicates and self)
    seen = {user["id"]}
    for mid in body.member_ids:
        if mid in seen:
            continue
        seen.add(mid)
        # Verify user exists
        cursor = await db.execute("SELECT id FROM users WHERE id = ?", (mid,))
        if await cursor.fetchone() is None:
            continue  # Skip nonexistent users
        await db.execute(
            "INSERT OR IGNORE INTO chat_group_members (group_id, user_id, status, joined_at) VALUES (?, ?, 'pending', ?)",
            (group_id, mid, now),
        )

    await db.commit()

    # Return the created group with members
    cursor = await db.execute("SELECT * FROM chat_groups WHERE id = ?", (group_id,))
    group = dict(await cursor.fetchone())

    cursor = await db.execute(
        "SELECT cgm.*, u.username, u.display_name FROM chat_group_members cgm "
        "JOIN users u ON cgm.user_id = u.id WHERE cgm.group_id = ?",
        (group_id,),
    )
    group["members"] = [dict(r) for r in await cursor.fetchall()]
    return group


@router.get("/groups")
async def group_list(
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """List all groups the current user is a member of (any status),
    ordered by most recent activity."""
    user_id = user["id"]

    # Get all groups the user is a member of
    cursor = await db.execute(
        """
        SELECT cg.* FROM chat_groups cg
        JOIN chat_group_members cgm ON cg.id = cgm.group_id
        WHERE cgm.user_id = ?
        GROUP BY cg.id
        """,
        (user_id,),
    )
    groups = [dict(r) for r in await cursor.fetchall()]

    result = []
    for g in groups:
        gid = g["id"]

        # Members
        cursor = await db.execute(
            "SELECT cgm.*, u.username, u.display_name FROM chat_group_members cgm "
            "JOIN users u ON cgm.user_id = u.id WHERE cgm.group_id = ?",
            (gid,),
        )
        g["members"] = [dict(r) for r in await cursor.fetchall()]

        # Last message
        cursor = await db.execute(
            "SELECT content, created_at FROM chat_group_messages "
            "WHERE group_id = ? AND is_deleted = 0 ORDER BY created_at DESC LIMIT 1",
            (gid,),
        )
        last_msg = await cursor.fetchone()
        g["last_message"] = last_msg["content"][:100] if last_msg else None
        g["last_time"] = last_msg["created_at"] if last_msg else g.get("created_at")

        result.append(g)

    # Sort by last_time descending
    result.sort(key=lambda g: g["last_time"] or "", reverse=True)
    return result


@router.get("/groups/{group_id}")
async def group_detail(
    group_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get group details with all members. Must be a member."""
    user_id = user["id"]

    cursor = await db.execute("SELECT * FROM chat_groups WHERE id = ?", (group_id,))
    group = await cursor.fetchone()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    group = dict(group)

    # Verify membership
    cursor = await db.execute(
        "SELECT * FROM chat_group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not a member of this group")

    # Get members with user info
    cursor = await db.execute(
        "SELECT cgm.*, u.username, u.display_name FROM chat_group_members cgm "
        "JOIN users u ON cgm.user_id = u.id WHERE cgm.group_id = ?",
        (group_id,),
    )
    group["members"] = [dict(r) for r in await cursor.fetchall()]

    # Last message
    cursor = await db.execute(
        "SELECT content, created_at FROM chat_group_messages "
        "WHERE group_id = ? AND is_deleted = 0 ORDER BY created_at DESC LIMIT 1",
        (group_id,),
    )
    last_msg = await cursor.fetchone()
    group["last_message"] = last_msg["content"][:100] if last_msg else None
    group["last_time"] = last_msg["created_at"] if last_msg else None

    return group


@router.get("/groups/{group_id}/messages")
async def group_messages(
    group_id: int,
    since: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get group messages (oldest first). Must be a member."""
    user_id = user["id"]

    # Verify group exists
    cursor = await db.execute("SELECT * FROM chat_groups WHERE id = ?", (group_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    # Verify membership
    cursor = await db.execute(
        "SELECT * FROM chat_group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not a member of this group")

    where = "group_id = ? AND is_deleted = 0"
    params: list = [group_id]

    if since is not None:
        where += " AND id > ?"
        params.append(since)

    params.append(limit)

    cursor = await db.execute(
        f"SELECT cgm.*, u.username as sender_username, u.display_name as sender_display_name FROM chat_group_messages cgm "
        f"JOIN users u ON cgm.sender_id = u.id WHERE {where} ORDER BY cgm.created_at ASC LIMIT ?",
        params,
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.post("/groups/{group_id}/messages", status_code=status.HTTP_201_CREATED)
async def group_send_message(
    group_id: int,
    body: GroupMessageSend,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Send a message to a group. Must be an accepted member."""
    user_id = user["id"]

    # Verify group exists
    cursor = await db.execute("SELECT * FROM chat_groups WHERE id = ?", (group_id,))
    if await cursor.fetchone() is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")

    # Verify accepted membership
    cursor = await db.execute(
        "SELECT * FROM chat_group_members WHERE group_id = ? AND user_id = ? AND status = 'accepted'",
        (group_id, user_id),
    )
    if await cursor.fetchone() is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must be an accepted member to send messages.",
        )

    now = _iso_now()
    cursor = await db.execute(
        "INSERT INTO chat_group_messages (group_id, sender_id, content, created_at) VALUES (?, ?, ?, ?)",
        (group_id, user_id, body.content, now),
    )
    await db.commit()
    msg_id = cursor.lastrowid

    cursor = await db.execute("SELECT * FROM chat_group_messages WHERE id = ?", (msg_id,))
    msg_dict = dict(await cursor.fetchone())

    # Broadcast via WebSocket to all group subscribers
    await chat_manager.broadcast(
        f"group:{group_id}",
        {"type": "new_message", "channel": "group", "group_id": group_id, "msg": msg_dict},
    )

    return msg_dict


@router.delete("/groups/{group_id}/messages/{msg_id}", status_code=status.HTTP_204_NO_CONTENT)
async def group_delete_message(
    group_id: int,
    msg_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Soft-delete a group message. Allowed for message sender or group creator."""
    user_id = user["id"]

    # Verify group exists
    cursor = await db.execute("SELECT * FROM chat_groups WHERE id = ?", (group_id,))
    group = await cursor.fetchone()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    group = dict(group)

    # Verify message exists and belongs to this group
    cursor = await db.execute(
        "SELECT * FROM chat_group_messages WHERE id = ? AND group_id = ? AND is_deleted = 0",
        (msg_id, group_id),
    )
    msg = await cursor.fetchone()
    if msg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    msg = dict(msg)

    # Permission: sender or group creator
    if msg["sender_id"] != user_id and group["creator_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the message sender or group creator can delete this message.",
        )

    await db.execute(
        "UPDATE chat_group_messages SET is_deleted = 1 WHERE id = ?", (msg_id,)
    )
    await db.commit()
    return None


@router.put("/groups/{group_id}/accept")
async def group_accept(
    group_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Accept a pending group invitation."""
    user_id = user["id"]

    cursor = await db.execute(
        "SELECT * FROM chat_group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    member = await cursor.fetchone()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="You are not invited to this group")
    member = dict(member)

    if member["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot accept — current status is '{member['status']}'.",
        )

    await db.execute(
        "UPDATE chat_group_members SET status = 'accepted' WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    await db.commit()
    return {"ok": True, "status": "accepted"}


@router.put("/groups/{group_id}/decline")
async def group_decline(
    group_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Decline a pending group invitation."""
    user_id = user["id"]

    cursor = await db.execute(
        "SELECT * FROM chat_group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    member = await cursor.fetchone()
    if member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="You are not invited to this group")
    member = dict(member)

    if member["status"] != "pending":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot decline — current status is '{member['status']}'.",
        )

    await db.execute(
        "UPDATE chat_group_members SET status = 'declined' WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    await db.commit()
    return {"ok": True, "status": "declined"}


@router.post("/groups/{group_id}/invite", status_code=status.HTTP_201_CREATED)
async def group_invite(
    group_id: int,
    body: GroupInvite,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Invite more users to the group. Must be an accepted member or creator."""
    user_id = user["id"]

    # Verify group exists
    cursor = await db.execute("SELECT * FROM chat_groups WHERE id = ?", (group_id,))
    group = await cursor.fetchone()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    group = dict(group)

    # Verify inviter is an accepted member or the creator
    cursor = await db.execute(
        "SELECT * FROM chat_group_members WHERE group_id = ? AND user_id = ?",
        (group_id, user_id),
    )
    member = await cursor.fetchone()
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are not a member of this group")
    member = dict(member)
    if member["status"] != "accepted":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only accepted members can invite others.",
        )

    # Get existing member IDs (any status) to skip
    cursor = await db.execute(
        "SELECT user_id FROM chat_group_members WHERE group_id = ?", (group_id,)
    )
    existing_ids = {row["user_id"] for row in await cursor.fetchall()}

    now = _iso_now()
    invited = []
    for uid in body.user_ids:
        if uid in existing_ids:
            continue  # Skip users already in the group
        # Verify user exists
        cursor = await db.execute("SELECT id FROM users WHERE id = ?", (uid,))
        if await cursor.fetchone() is None:
            continue
        await db.execute(
            "INSERT OR IGNORE INTO chat_group_members (group_id, user_id, status, joined_at) VALUES (?, ?, 'pending', ?)",
            (group_id, uid, now),
        )
        invited.append(uid)

    await db.commit()
    return {"invited": invited}


@router.delete("/groups/{group_id}/members/{target_user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def group_remove_member(
    group_id: int,
    target_user_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Remove a member or leave the group.
    - If target == self: leave (set status='removed').
    - If target != self: must be group creator to remove someone."""
    user_id = user["id"]

    # Verify group exists
    cursor = await db.execute("SELECT * FROM chat_groups WHERE id = ?", (group_id,))
    group = await cursor.fetchone()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    group = dict(group)

    # Verify target is a member
    cursor = await db.execute(
        "SELECT * FROM chat_group_members WHERE group_id = ? AND user_id = ?",
        (group_id, target_user_id),
    )
    target_member = await cursor.fetchone()
    if target_member is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User is not a member of this group")
    target_member = dict(target_member)

    if target_user_id == user_id:
        # Self: leave the group
        await db.execute(
            "UPDATE chat_group_members SET status = 'removed' WHERE group_id = ? AND user_id = ?",
            (group_id, user_id),
        )
    else:
        # Removing someone else: must be creator
        if group["creator_id"] != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the group creator can remove other members.",
            )
        await db.execute(
            "UPDATE chat_group_members SET status = 'removed' WHERE group_id = ? AND user_id = ?",
            (group_id, target_user_id),
        )

    await db.commit()
    return None


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def group_disband(
    group_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Delete/disband the group. Only the creator can do this.
    Cascades delete messages and memberships via FK."""
    user_id = user["id"]

    cursor = await db.execute("SELECT * FROM chat_groups WHERE id = ?", (group_id,))
    group = await cursor.fetchone()
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Group not found")
    group = dict(group)

    if group["creator_id"] != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the group creator can disband the group.",
        )

    # Delete group (FK cascades delete memberships and messages)
    await db.execute("DELETE FROM chat_groups WHERE id = ?", (group_id,))
    await db.commit()
    return None


# ===========================================================================
# SECTION C — Global Chat
# ===========================================================================


@router.get("/global/messages")
async def global_messages(
    since: int | None = Query(None),
    before: int | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Get recent global chat messages, newest first.

    - ``since``  – get messages newer than this ID (polling)
    - ``before`` – get messages older than this ID (pagination)
    - ``limit``  – max messages to return (default 50)
    """
    where = "is_deleted = 0"
    params: list = []

    if since is not None:
        where += " AND id > ?"
        params.append(since)

    if before is not None:
        where += " AND id < ?"
        params.append(before)

    params.append(limit)

    cursor = await db.execute(
        f"SELECT gcm.*, u.username as sender_username, u.display_name as sender_display_name FROM global_chat_messages gcm "
        f"JOIN users u ON gcm.sender_id = u.id WHERE {where} ORDER BY gcm.created_at DESC LIMIT ?",
        params,
    )
    return [dict(r) for r in await cursor.fetchall()]


@router.post("/global/messages", status_code=status.HTTP_201_CREATED)
async def global_send_message(
    body: GlobalMessageSend,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Send a message to the global chat."""
    now = _iso_now()
    cursor = await db.execute(
        "INSERT INTO global_chat_messages (sender_id, content, created_at) VALUES (?, ?, ?)",
        (user["id"], body.content, now),
    )
    await db.commit()
    msg_id = cursor.lastrowid

    cursor = await db.execute(
        "SELECT gcm.*, u.username as sender_username, u.display_name as sender_display_name "
        "FROM global_chat_messages gcm JOIN users u ON gcm.sender_id = u.id WHERE gcm.id = ?",
        (msg_id,),
    )
    msg_dict = dict(await cursor.fetchone())

    # Broadcast via WebSocket to all global subscribers
    await chat_manager.broadcast(
        "global",
        {"type": "new_message", "channel": "global", "msg": msg_dict},
    )

    return msg_dict


@router.delete("/global/messages/{msg_id}", status_code=status.HTTP_204_NO_CONTENT)
async def global_delete_message(
    msg_id: int,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Soft-delete a global chat message.

    Allowed for:
    - The message sender
    - Admin/owner with ``admins_can_delete_global_chat`` permission enabled
    """
    cursor = await db.execute(
        "SELECT * FROM global_chat_messages WHERE id = ? AND is_deleted = 0", (msg_id,)
    )
    msg = await cursor.fetchone()
    if msg is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    msg = dict(msg)

    # Sender can always delete their own
    if msg["sender_id"] == user["id"]:
        await db.execute(
            "UPDATE global_chat_messages SET is_deleted = 1 WHERE id = ?", (msg_id,)
        )
        await db.commit()
        return None

    # Admin/owner with permission check
    if user["role"] in ("admin", "owner"):
        config = _load_config()
        if user["role"] == "admin" and not config.get("admins_can_delete_global_chat", False):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin permission to delete global chat messages is not enabled. Contact the owner.",
            )
        # Owner can always delete (if permission is False for admin, owner still passes)
        await db.execute(
            "UPDATE global_chat_messages SET is_deleted = 1 WHERE id = ?", (msg_id,)
        )
        await db.commit()
        return None

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="You can only delete your own messages.",
    )
