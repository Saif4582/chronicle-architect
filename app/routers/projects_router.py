from fastapi import APIRouter, Depends, HTTPException, status
from app.database import get_db
from app.auth import get_current_user
from app.models import ProjectCreate, ProjectUpdate, ProjectResponse
from app.tokenizer import get_token_count

router = APIRouter(prefix="")


@router.get("/api/projects")
async def list_projects(user: dict = Depends(get_current_user), db=Depends(get_db)):
    cursor = await db.execute(
        """SELECT p.id, p.user_id, p.title, p.description, p.created_at, p.updated_at, p.last_accessed,
                  COALESCE(SUM(CASE WHEN TRIM(ch.content) = '' THEN 0
                                    ELSE LENGTH(ch.content) - LENGTH(REPLACE(ch.content, ' ', '')) + 1
                               END), 0) AS total_words,
                  COUNT(DISTINCT ch.id) as chapter_count,
                  COUNT(DISTINCT v.id) as volume_count
           FROM projects p
           LEFT JOIN volumes v ON v.project_id = p.id
           LEFT JOIN chapters ch ON ch.project_id = p.id
           WHERE p.user_id = ?
           GROUP BY p.id
           ORDER BY p.updated_at DESC""",
        (user["id"],),
    )
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        cursor2 = await db.execute("SELECT content FROM chapters WHERE project_id = ?", (d["id"],))
        chapters = await cursor2.fetchall()
        total_tokens = 0
        for ch in chapters:
            total_tokens += get_token_count(ch[0] or "")
        d["total_tokens"] = total_tokens
        result.append(d)
    return result


@router.post("/api/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(body: ProjectCreate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    # Check for duplicate title (case-insensitive) for this user
    cursor_check = await db.execute(
        "SELECT id FROM projects WHERE user_id = ? AND LOWER(title) = LOWER(?)",
        (user["id"], body.title),
    )
    existing = await cursor_check.fetchone()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A project with this title already exists.",
        )

    cursor = await db.execute(
        "INSERT INTO projects (user_id, title, description) VALUES (?, ?, ?)",
        (user["id"], body.title, body.description),
    )
    await db.commit()
    project_id = cursor.lastrowid

    # Create exactly one default "Chapter 1"
    await db.execute(
        "INSERT INTO chapters (project_id, title, content, position) VALUES (?, ?, ?, ?)",
        (project_id, "Chapter 1", "", 0),
    )
    await db.commit()

    cursor2 = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor2.fetchone()

    return dict(project)


@router.get("/api/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()

    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    return project_dict


@router.put("/api/projects/{project_id}", response_model=ProjectResponse)
async def update_project(project_id: int, body: ProjectUpdate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()

    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    new_title = body.title if body.title is not None else project_dict["title"]
    new_description = body.description if body.description is not None else project_dict["description"]
    new_content = body.content if body.content is not None else project_dict["content"]

    await db.execute(
        "UPDATE projects SET title = ?, description = ?, content = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_title, new_description, new_content, project_id),
    )
    await db.commit()

    cursor2 = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    updated = await cursor2.fetchone()

    return dict(updated)


@router.delete("/api/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(project_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()

    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    await db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    await db.commit()
    return None


@router.put("/api/projects/{project_id}/touch")
async def touch_project(project_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    """Update the last_accessed timestamp for a project."""
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    await db.execute(
        "UPDATE projects SET last_accessed = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (project_id,),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    updated = await cursor.fetchone()
    return dict(updated)
