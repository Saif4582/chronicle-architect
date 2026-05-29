from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
import json
import aiosqlite
import logging
from app.database import get_db, DB_PATH
from app.auth import get_current_user
from app.models import WikiEntryCreate, WikiEntryUpdate, WikiEntryResponse, WikiReorderRequest, WikiMoveRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="")

# --- Helper: verify wiki entry ownership through project ---


async def _verify_wiki_ownership(entry_id: int, user: dict, db):
    """Fetch wiki entry, verify its project belongs to the user. Returns (entry_dict, project_dict)."""
    cursor = await db.execute("SELECT * FROM wiki_entries WHERE id = ?", (entry_id,))
    entry = await cursor.fetchone()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Wiki entry not found")

    entry_dict = dict(entry)

    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (entry_dict["project_id"],))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    return entry_dict, project_dict


async def fetch_wiki_entries_for_project(db, project_id: int) -> list:
    """Fetch all wiki entries for a project with resolved parent info."""
    cursor = await db.execute(
        "SELECT * FROM wiki_entries WHERE project_id = ? ORDER BY position ASC, name ASC",
        (project_id,)
    )
    rows = await cursor.fetchall()
    entries = []
    for row in rows:
        entry = dict(row)
        # Parse parents field
        parents_raw = entry.get("parents", "")
        if isinstance(parents_raw, str):
            parents_raw = parents_raw.strip()
            if parents_raw.startswith("["):
                try:
                    parent_ids = json.loads(parents_raw)
                except Exception:
                    parent_ids = []
            elif parents_raw:
                try:
                    parent_ids = [int(x.strip()) for x in parents_raw.split(",") if x.strip()]
                except (ValueError, TypeError):
                    parent_ids = []
            else:
                parent_ids = []
        elif isinstance(parents_raw, (int, float)):
            parent_ids = [int(parents_raw)]
        else:
            parent_ids = []

        # Ensure parent_id FK is included
        if entry.get("parent_id") and entry["parent_id"] not in parent_ids:
            parent_ids.insert(0, entry["parent_id"])

        entry["parent_ids"] = parent_ids

        # Resolve parent names
        if parent_ids:
            placeholders = ",".join("?" * len(parent_ids))
            name_cursor = await db.execute(
                f"SELECT id, name FROM wiki_entries WHERE id IN ({placeholders})",
                parent_ids
            )
            name_rows = await name_cursor.fetchall()
            name_map = {r["id"]: r["name"] for r in name_rows}
            entry["parent_names"] = [
                {"id": pid, "name": name_map.get(pid, "Unknown")}
                for pid in parent_ids
                if pid in name_map
            ]
        else:
            entry["parent_names"] = []

        entries.append(entry)

    return entries


@router.get("/api/projects/{project_id}/wiki")
async def list_wiki_entries(
    project_id: int,
    category: str = Query(None),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    # Verify project ownership
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    entries = await fetch_wiki_entries_for_project(db, project_id)

    # Filter by category if specified
    if category:
        entries = [e for e in entries if e.get("category") == category]

    return entries


@router.post("/api/projects/{project_id}/wiki", response_model=WikiEntryResponse, status_code=status.HTTP_201_CREATED)
async def create_wiki_entry(project_id: int, body: WikiEntryCreate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    # Verify project ownership
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    # Duplicate entry name check (case-insensitive)
    cursor = await db.execute(
        "SELECT id FROM wiki_entries WHERE project_id = ? AND LOWER(name) = LOWER(?) AND LOWER(category) = LOWER(?)",
        (project_id, body.name, body.category),
    )
    existing = await cursor.fetchone()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="An entry with this name already exists in this category.")

    # Determine next position for new entry
    cursor_pos = await db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 as next_pos FROM wiki_entries WHERE project_id = ?",
        (project_id,),
    )
    pos_row = await cursor_pos.fetchone()
    next_pos = pos_row[0] if pos_row else 0

    parents_json = json.dumps(body.parents) if body.parents else None
    cursor = await db.execute(
        "INSERT INTO wiki_entries (project_id, name, category, parent_id, content, metadata_json, parents, position) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, body.name, body.category, body.parent_id, body.content, body.metadata_json, parents_json, next_pos),
    )
    await db.commit()
    entry_id = cursor.lastrowid

    cursor = await db.execute("SELECT * FROM wiki_entries WHERE id = ?", (entry_id,))
    entry = await cursor.fetchone()

    return dict(entry)


@router.put("/api/projects/{project_id}/wiki/reorder")
async def reorder_wiki_entries(
    project_id: int,
    body: WikiReorderRequest,
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    # Verify project ownership (any project member can reorder)
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    category = body.category
    logger.info(f"Reorder request: order length = {len(body.order)}, category = {category}, project_id = {project_id}")

    try:
        # Wrap entire reorder in a single transaction
        await db.execute("BEGIN IMMEDIATE")

        # Normalize NULL positions (legacy rows) scoped to category if provided
        if category:
            await db.execute(
                "UPDATE wiki_entries SET position = 0 WHERE position IS NULL AND category = ? AND project_id = ?",
                (category, project_id),
            )
        else:
            await db.execute(
                "UPDATE wiki_entries SET position = 0 WHERE position IS NULL AND project_id = ?",
                (project_id,),
            )

        # Apply the new ordering
        for i, entry_id in enumerate(body.order):
            await db.execute(
                "UPDATE wiki_entries SET position = ? WHERE id = ? AND project_id = ?",
                (i, entry_id, project_id),
            )

        await db.commit()
        logger.info(f"Reorder completed successfully for project {project_id}")

        return {"ok": True}
    except Exception as e:
        logger.error(f"Reorder failed for project {project_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/wiki/{id}", response_model=WikiEntryResponse)
async def get_wiki_entry(id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    entry_dict, _ = await _verify_wiki_ownership(id, user, db)
    parent_ids = []
    if entry_dict.get("parents"):
        try:
            parsed = json.loads(entry_dict["parents"])
            if isinstance(parsed, list):
                parent_ids = [int(x) for x in parsed]
            elif isinstance(parsed, str):
                parent_ids = [int(x.strip()) for x in parsed.split(",") if x.strip()]
            elif isinstance(parsed, (int, float)):
                parent_ids = [int(parsed)]
            else:
                parent_ids = []
        except:
            try:
                parent_ids = [int(x.strip()) for x in entry_dict["parents"].split(",") if x.strip()]
            except:
                parent_ids = []
    if entry_dict.get("parent_id") and entry_dict["parent_id"] not in parent_ids:
        parent_ids.append(entry_dict["parent_id"])
    entry_dict["parent_ids"] = parent_ids
    # Batch parent name resolution (fix N+1)
    parent_names = []
    if parent_ids:
        placeholders = ','.join('?' * len(parent_ids))
        cursor = await db.execute(
            f"SELECT id, name FROM wiki_entries WHERE id IN ({placeholders})",
            parent_ids
        )
        rows = await cursor.fetchall()
        parent_map = {row['id']: row['name'] for row in rows}
        parent_names = [
            {"id": pid, "name": parent_map.get(pid)}
            for pid in parent_ids
            if pid in parent_map
        ]
    entry_dict["parent_names"] = parent_names
    return entry_dict


@router.put("/api/wiki/{id}", response_model=WikiEntryResponse)
async def update_wiki_entry(id: int, body: WikiEntryUpdate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    entry_dict, project_dict = await _verify_wiki_ownership(id, user, db)

    new_name = body.name if body.name is not None else entry_dict["name"]
    new_category = body.category if body.category is not None else entry_dict["category"]
    new_parent_id = body.parent_id if body.parent_id is not None else entry_dict["parent_id"]
    new_content = body.content if body.content is not None else entry_dict["content"]
    new_metadata_json = body.metadata_json if body.metadata_json is not None else entry_dict.get("metadata_json", "{}")

    # Duplicate check: if name or category changed, ensure no conflict
    if (body.name is not None and body.name.lower() != entry_dict["name"].lower()) or \
       (body.category is not None and body.category.lower() != (entry_dict.get("category") or "").lower()):
        cursor = await db.execute(
            "SELECT id FROM wiki_entries WHERE project_id = ? AND id != ? AND LOWER(name) = LOWER(?) AND LOWER(category) = LOWER(?)",
            (entry_dict["project_id"], id, new_name, new_category),
        )
        existing = await cursor.fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An entry with this name already exists in that category.",
            )

    if body.parents is not None:
        new_parents = body.parents
        parents_json = json.dumps(new_parents) if len(new_parents) > 0 else None
        # Keep parent_id in sync with parents array
        new_parent_id = new_parents[0] if len(new_parents) > 0 else None
    else:
        # Preserve existing parents — entry_dict["parents"] is raw DB value (already JSON string or None)
        parents_raw = entry_dict.get("parents")
        if parents_raw is not None:
            # It's already a JSON-encoded string in the DB, use as-is to avoid double-encoding
            parents_json = parents_raw
        else:
            parents_json = None
    await db.execute(
        "UPDATE wiki_entries SET name = ?, category = ?, parent_id = ?, content = ?, metadata_json = ?, parents = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_name, new_category, new_parent_id, new_content, new_metadata_json, parents_json, id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM wiki_entries WHERE id = ?", (id,))
    updated = await cursor.fetchone()

    return dict(updated)


@router.delete("/api/wiki/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_wiki_entry(id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    entry_dict, project_dict = await _verify_wiki_ownership(id, user, db)
    await db.execute("DELETE FROM wiki_entries WHERE id = ?", (id,))
    await db.commit()

    return None


@router.get("/api/projects/{project_id}/wiki/categories")
async def list_wiki_categories(project_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    # Verify project ownership
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    PRESET_CATEGORIES = ['Characters', 'Locations', 'Items / Artefacts', 'Factions / Groups', 'Lore / History', 'Religions', 'Power System']

    cursor = await db.execute(
        "SELECT DISTINCT category FROM wiki_entries WHERE project_id = ? AND category IS NOT NULL AND category != '' ORDER BY category ASC",
        (project_id,),
    )
    rows = await cursor.fetchall()
    custom_categories = [r[0] for r in rows if r[0] not in PRESET_CATEGORIES]

    # Presets first (preserving order), then custom categories alphabetically
    return PRESET_CATEGORIES + custom_categories


@router.put("/api/projects/{project_id}/wiki/categories/{old_name}")
async def rename_wiki_category(project_id: int, old_name: str, request: Request, user: dict = Depends(get_current_user), db=Depends(get_db)):
    """Rename a category across all wiki entries in a project. The new name is passed in the request body."""
    # Verify project ownership
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    # Read the new name from the request body
    body_bytes = await request.body()
    body = json.loads(body_bytes) if body_bytes else {}
    new_name = body.get("name", "").strip()

    if not new_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="New category name is required")

    PRESET_CATEGORIES = ['Characters', 'Locations', 'Items / Artefacts', 'Factions / Groups', 'Lore / History', 'Religions', 'Power System']
    if old_name in PRESET_CATEGORIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot rename preset categories")

    if new_name in PRESET_CATEGORIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot rename to a preset category name")

    # Check if new_name already exists as a distinct category
    cursor = await db.execute(
        "SELECT DISTINCT category FROM wiki_entries WHERE project_id = ? AND category = ?",
        (project_id, new_name),
    )
    existing = await cursor.fetchone()
    if existing and old_name.lower() != new_name.lower():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Category '{new_name}' already exists")

    # Update all entries with the old category name
    await db.execute(
        "UPDATE wiki_entries SET category = ?, updated_at = CURRENT_TIMESTAMP WHERE project_id = ? AND category = ?",
        (new_name, project_id, old_name),
    )
    await db.commit()

    return {"old_name": old_name, "new_name": new_name, "message": "Category renamed successfully"}


@router.delete("/api/projects/{project_id}/wiki/categories/{category_name}")
async def delete_wiki_category(
    project_id: int,
    category_name: str,
    action: str = Query("delete"),
    target: str = Query(None),
    user: dict = Depends(get_current_user),
    db=Depends(get_db),
):
    """Delete or move a wiki category and all its entries."""
    # Verify project ownership
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    PRESET_CATEGORIES = ['Characters', 'Locations', 'Items / Artefacts', 'Factions / Groups', 'Lore / History', 'Religions', 'Power System']
    if category_name in PRESET_CATEGORIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot delete preset categories.")

    if action == "delete":
        cursor = await db.execute(
            "DELETE FROM wiki_entries WHERE project_id = ? AND category = ?",
            (project_id, category_name),
        )
        await db.commit()
        deleted_count = cursor.rowcount

        return {"message": "Category deleted along with all entries", "deleted_count": deleted_count}

    elif action == "move":
        new_category = target.strip() if target and target.strip() else "Uncategorized"
        cursor = await db.execute(
            "UPDATE wiki_entries SET category = ?, updated_at = CURRENT_TIMESTAMP WHERE project_id = ? AND category = ?",
            (new_category, project_id, category_name),
        )
        await db.commit()
        moved_count = cursor.rowcount

        return {"message": "Entries moved successfully", "moved_count": moved_count, "target_category": new_category}

    else:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid action. Must be 'delete' or 'move'.")


@router.put("/api/projects/{project_id}/wiki/move")
async def move_wiki_entry(project_id: int, body: WikiMoveRequest, user: dict = Depends(get_current_user), db: aiosqlite.Connection = Depends(get_db)):
    logger.info(f"Move request: entry_id={body.entry_id}, category={body.category}, parent_ids={body.parent_ids}, position={body.position}, project_id={project_id}")

    # 1. Verify project ownership (same pattern as other endpoints)
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=403, detail="Not authorized")

    # 2. Verify entry exists in this project
    cursor = await db.execute("SELECT * FROM wiki_entries WHERE id = ? AND project_id = ?", (body.entry_id, project_id))
    entry = await cursor.fetchone()
    if not entry:
        raise HTTPException(status_code=404, detail="Wiki entry not found")
    entry_dict = dict(entry)

    try:
        # Wrap all data-modifying operations in a single transaction
        await db.execute("BEGIN IMMEDIATE")

        # 3. If category provided and different, update it
        if body.category is not None and body.category != entry_dict["category"]:
            await db.execute("UPDATE wiki_entries SET category = ? WHERE id = ?", (body.category, body.entry_id))

        # 4. If parent_ids provided, validate and update
        if body.parent_ids is not None:
            # Validate circular references
            for pid in body.parent_ids:
                if pid == body.entry_id:
                    raise HTTPException(status_code=400, detail="Circular reference: entry cannot be its own parent")
                # Follow parent chain to check for circular reference
                current = pid
                visited = set()
                while current is not None:
                    if current == body.entry_id:
                        raise HTTPException(status_code=400, detail="Circular reference detected")
                    if current in visited:
                        break
                    visited.add(current)
                    c = await db.execute("SELECT parent_id FROM wiki_entries WHERE id = ? AND project_id = ?", (current, project_id))
                    row = await c.fetchone()
                    current = row["parent_id"] if row else None

            # Validate all parent_ids exist in this project
            for pid in body.parent_ids:
                c = await db.execute("SELECT id FROM wiki_entries WHERE id = ? AND project_id = ?", (pid, project_id))
                if not await c.fetchone():
                    raise HTTPException(status_code=400, detail=f"Parent entry {pid} not found in project")

            # Update parents JSON and parent_id FK
            if body.parent_ids:
                parents_json = json.dumps(body.parent_ids)
                parent_id_fk = body.parent_ids[0]
            else:
                parents_json = None
                parent_id_fk = None
            await db.execute(
                "UPDATE wiki_entries SET parents = ?, parent_id = ? WHERE id = ?",
                (parents_json, parent_id_fk, body.entry_id)
            )

        # 5. If position provided, shift existing entries and update
        if body.position is not None:
            # Get current category (might have been updated above)
            if body.category is not None:
                effective_category = body.category
            else:
                effective_category = entry_dict["category"]

            # Remove entry from current ordering (set to -1 temporarily)
            await db.execute("UPDATE wiki_entries SET position = -1 WHERE id = ?", (body.entry_id,))

            # Shift entries down to make room
            await db.execute(
                "UPDATE wiki_entries SET position = position + 1 WHERE category = ? AND project_id = ? AND position >= ? AND id != ?",
                (effective_category, project_id, body.position, body.entry_id)
            )

            # Set new position (ensure not negative)
            new_position = max(0, body.position)
            await db.execute(
                "UPDATE wiki_entries SET position = ? WHERE id = ?",
                (new_position, body.entry_id)
            )

        await db.commit()
        logger.info(f"Move completed successfully for entry {body.entry_id} in project {project_id}")

        # 6. Return full updated wiki entries list (same format as list endpoint)
        cursor = await db.execute(
            "SELECT * FROM wiki_entries WHERE project_id = ? ORDER BY category, position",
            (project_id,)
        )
        rows = await cursor.fetchall()
        result = []
        all_parent_ids = set()
        for r in rows:
            entry = dict(r)
            parent_ids = []
            if entry.get("parents"):
                try:
                    parsed = json.loads(entry["parents"])
                    if isinstance(parsed, list):
                        parent_ids = [int(x) for x in parsed]
                    elif isinstance(parsed, str):
                        parent_ids = [int(x.strip()) for x in parsed.split(",") if x.strip()]
                    elif isinstance(parsed, (int, float)):
                        parent_ids = [int(parsed)]
                    else:
                        parent_ids = []
                except:
                    try:
                        parent_ids = [int(x.strip()) for x in entry["parents"].split(",") if x.strip()]
                    except:
                        parent_ids = []
            if entry.get("parent_id") and entry["parent_id"] not in parent_ids:
                parent_ids.append(entry["parent_id"])
            entry["parent_ids"] = parent_ids
            for pid in parent_ids:
                all_parent_ids.add(pid)
            result.append(entry)

        # Batch resolve parent names
        parent_map = {}
        if all_parent_ids:
            placeholders = ','.join('?' * len(all_parent_ids))
            c = await db.execute(
                f"SELECT id, name FROM wiki_entries WHERE id IN ({placeholders})",
                list(all_parent_ids)
            )
            for row in await c.fetchall():
                parent_map[row["id"]] = row["name"]

        for entry in result:
            entry["parent_names"] = [
                {"id": pid, "name": parent_map.get(pid)}
                for pid in entry.get("parent_ids", [])
                if pid in parent_map
            ]

        return result

    except HTTPException:
        # Roll back the transaction so it doesn't linger on the connection
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        raise
    except Exception as e:
        logger.exception(f"Move failed for entry {body.entry_id} in project {project_id}")
        # Roll back the failed transaction
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))
