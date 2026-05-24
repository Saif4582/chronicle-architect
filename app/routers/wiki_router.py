from fastapi import APIRouter, Depends, HTTPException, status, Query
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
    return [dict(r) for r in rows]


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

    cursor = await db.execute(
        "INSERT INTO wiki_entries (project_id, name, category, parent_id, content) VALUES (?, ?, ?, ?, ?)",
        (project_id, body.name, body.category, body.parent_id, body.content),
    )
    await db.commit()
    entry_id = cursor.lastrowid

    cursor = await db.execute("SELECT * FROM wiki_entries WHERE id = ?", (entry_id,))
    entry = await cursor.fetchone()
    return dict(entry)


@router.get("/api/wiki/{id}", response_model=WikiEntryResponse)
async def get_wiki_entry(id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    entry_dict, _ = await _verify_wiki_ownership(id, user, db)
    return entry_dict


@router.put("/api/wiki/{id}", response_model=WikiEntryResponse)
async def update_wiki_entry(id: int, body: WikiEntryUpdate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    entry_dict, _ = await _verify_wiki_ownership(id, user, db)

    new_name = body.name if body.name is not None else entry_dict["name"]
    new_category = body.category if body.category is not None else entry_dict["category"]
    new_parent_id = body.parent_id if body.parent_id is not None else entry_dict["parent_id"]
    new_content = body.content if body.content is not None else entry_dict["content"]

    await db.execute(
        "UPDATE wiki_entries SET name = ?, category = ?, parent_id = ?, content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_name, new_category, new_parent_id, new_content, id),
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

    cursor = await db.execute(
        "SELECT DISTINCT category FROM wiki_entries WHERE project_id = ? ORDER BY category ASC",
        (project_id,),
    )
    rows = await cursor.fetchall()
    return [r[0] for r in rows]
