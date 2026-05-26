from fastapi import APIRouter, Depends, HTTPException, status, Query, Request
import json
from app.database import get_db
from app.auth import get_current_user
from app.models import WikiEntryCreate, WikiEntryUpdate, WikiEntryResponse

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

    if category is not None:
        cursor = await db.execute(
            "SELECT * FROM wiki_entries WHERE project_id = ? AND category = ? ORDER BY name ASC",
            (project_id, category),
        )
    else:
        cursor = await db.execute(
            "SELECT * FROM wiki_entries WHERE project_id = ? ORDER BY name ASC",
            (project_id,),
        )
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        entry = dict(r)
        parent_ids = []
        if entry.get("parents"):
            try:
                parent_ids = json.loads(entry["parents"])
            except:
                parent_ids = []
        if entry.get("parent_id") and entry["parent_id"] not in parent_ids:
            parent_ids.append(entry["parent_id"])
        entry["parent_ids"] = parent_ids
        parent_names = []
        if parent_ids:
            for pid in parent_ids:
                cursor2 = await db.execute("SELECT name FROM wiki_entries WHERE id = ?", (pid,))
                p_row = await cursor2.fetchone()
                if p_row:
                    parent_names.append({"id": pid, "name": p_row[0]})
        entry["parent_names"] = parent_names
        result.append(entry)
    return result


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

    parents_json = json.dumps(body.parents) if body.parents else None
    cursor = await db.execute(
        "INSERT INTO wiki_entries (project_id, name, category, parent_id, content, metadata_json, parents) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, body.name, body.category, body.parent_id, body.content, body.metadata_json, parents_json),
    )
    await db.commit()
    entry_id = cursor.lastrowid

    cursor = await db.execute("SELECT * FROM wiki_entries WHERE id = ?", (entry_id,))
    entry = await cursor.fetchone()
    return dict(entry)


@router.get("/api/wiki/{id}", response_model=WikiEntryResponse)
async def get_wiki_entry(id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    entry_dict, _ = await _verify_wiki_ownership(id, user, db)
    parent_ids = []
    if entry_dict.get("parents"):
        try:
            parent_ids = json.loads(entry_dict["parents"])
        except:
            parent_ids = []
    if entry_dict.get("parent_id") and entry_dict["parent_id"] not in parent_ids:
        parent_ids.append(entry_dict["parent_id"])
    entry_dict["parent_ids"] = parent_ids
    parent_names = []
    if parent_ids:
        for pid in parent_ids:
            cursor = await db.execute("SELECT name FROM wiki_entries WHERE id = ?", (pid,))
            p_row = await cursor.fetchone()
            if p_row:
                parent_names.append({"id": pid, "name": p_row[0]})
    entry_dict["parent_names"] = parent_names
    return entry_dict


@router.put("/api/wiki/{id}", response_model=WikiEntryResponse)
async def update_wiki_entry(id: int, body: WikiEntryUpdate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    entry_dict, _ = await _verify_wiki_ownership(id, user, db)

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

    new_parents = body.parents if body.parents is not None else entry_dict.get("parents")
    parents_json = json.dumps(new_parents) if new_parents else None
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
    await _verify_wiki_ownership(id, user, db)
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
