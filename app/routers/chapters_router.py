from fastapi import APIRouter, Depends, HTTPException, status
from app.database import get_db
from app.auth import get_current_user
from app.models import ChapterCreate, ChapterUpdate, ChapterResponse, ChapterReorder
from app.tokenizer import get_token_count, count_words

router = APIRouter(prefix="")

# --- Helper: verify chapter ownership through project ---


async def _verify_chapter_ownership(chapter_id: int, user: dict, db):
    """Fetch chapter, verify its project belongs to the user. Returns (chapter_dict, project_dict)."""
    cursor = await db.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,))
    chapter = await cursor.fetchone()
    if chapter is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Chapter not found")

    chapter_dict = dict(chapter)

    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (chapter_dict["project_id"],))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    return chapter_dict, project_dict


@router.get("/api/projects/{project_id}/chapters")
async def list_chapters(project_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    # Verify project ownership
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    cursor = await db.execute(
        "SELECT * FROM chapters WHERE project_id = ? ORDER BY position ASC",
        (project_id,),
    )
    rows = await cursor.fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["token_count"] = get_token_count(d.get("content", ""))
        d["word_count"] = count_words(d.get("content", ""))
        result.append(d)
    return result


@router.post("/api/projects/{project_id}/chapters", response_model=ChapterResponse, status_code=status.HTTP_201_CREATED)
async def create_chapter(project_id: int, body: ChapterCreate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    # Verify project ownership
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    # Validate volume_id if provided
    if body.volume_id is not None:
        cursor_vol = await db.execute("SELECT * FROM volumes WHERE id = ?", (body.volume_id,))
        volume = await cursor_vol.fetchone()
        if volume is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Volume not found")
        volume_dict = dict(volume)
        if volume_dict["project_id"] != project_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Volume does not belong to this project")

    # Auto-assign position
    cursor = await db.execute("SELECT MAX(position) as max_pos FROM chapters WHERE project_id = ?", (project_id,))
    row = await cursor.fetchone()
    max_pos = row["max_pos"] if row["max_pos"] is not None else -1
    new_position = max_pos + 1

    # Check for duplicate chapter title within the same volume (case-insensitive)
    # If volume_id is set, check only chapters in that volume; if null, check only chapters with no volume
    if body.volume_id is not None:
        cursor_check = await db.execute(
            "SELECT id FROM chapters WHERE project_id = ? AND LOWER(title) = LOWER(?) AND volume_id = ?",
            (project_id, body.title, body.volume_id),
        )
    else:
        cursor_check = await db.execute(
            "SELECT id FROM chapters WHERE project_id = ? AND LOWER(title) = LOWER(?) AND volume_id IS NULL",
            (project_id, body.title),
        )
    existing = await cursor_check.fetchone()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A chapter with this title already exists in this project.",
        )

    cursor = await db.execute(
        "INSERT INTO chapters (project_id, title, content, position, volume_id) VALUES (?, ?, ?, ?, ?)",
        (project_id, body.title, "", new_position, body.volume_id),
    )
    await db.commit()
    chapter_id = cursor.lastrowid

    cursor = await db.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,))
    chapter = await cursor.fetchone()
    return dict(chapter)


@router.get("/api/chapters/{id}", response_model=ChapterResponse)
async def get_chapter(id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    chapter_dict, _ = await _verify_chapter_ownership(id, user, db)
    chapter_dict["token_count"] = get_token_count(chapter_dict.get("content", ""))
    return chapter_dict


@router.put("/api/chapters/{id}", response_model=ChapterResponse)
async def update_chapter(id: int, body: ChapterUpdate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    chapter_dict, _ = await _verify_chapter_ownership(id, user, db)

    # Check for duplicate chapter title within the same volume (case-insensitive, exclude self)
    if body.title is not None and body.title.lower() != chapter_dict["title"].lower():
        target_volume = body.volume_id if body.volume_id is not None else chapter_dict.get("volume_id")
        if target_volume is not None:
            cursor_check = await db.execute(
                "SELECT id FROM chapters WHERE project_id = ? AND LOWER(title) = LOWER(?) AND volume_id = ? AND id != ?",
                (chapter_dict["project_id"], body.title, target_volume, id),
            )
        else:
            cursor_check = await db.execute(
                "SELECT id FROM chapters WHERE project_id = ? AND LOWER(title) = LOWER(?) AND volume_id IS NULL AND id != ?",
                (chapter_dict["project_id"], body.title, id),
            )
        existing = await cursor_check.fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A chapter with this title already exists in this project.",
            )

    new_title = body.title if body.title is not None else chapter_dict["title"]
    new_content = body.content if body.content is not None else chapter_dict["content"]
    new_position = body.position if body.position is not None else chapter_dict["position"]
    new_volume_id = body.volume_id if body.volume_id is not None else chapter_dict["volume_id"]

    # Validate volume_id if caller provides a non-None value
    if body.volume_id is not None:
        cursor_vol = await db.execute("SELECT * FROM volumes WHERE id = ?", (body.volume_id,))
        volume = await cursor_vol.fetchone()
        if volume is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Volume not found")
        volume_dict = dict(volume)
        if volume_dict["project_id"] != chapter_dict["project_id"]:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Volume does not belong to this project")

    await db.execute(
        "UPDATE chapters SET title = ?, content = ?, position = ?, volume_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_title, new_content, new_position, new_volume_id, id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM chapters WHERE id = ?", (id,))
    updated = await cursor.fetchone()
    return dict(updated)


@router.delete("/api/chapters/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chapter(id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    chapter_dict, _ = await _verify_chapter_ownership(id, user, db)
    project_id = chapter_dict["project_id"]
    old_position = chapter_dict["position"]

    await db.execute("DELETE FROM chapters WHERE id = ?", (id,))

    # Reorder remaining chapters: shift positions down
    await db.execute(
        "UPDATE chapters SET position = position - 1 WHERE project_id = ? AND position > ?",
        (project_id, old_position),
    )
    await db.commit()
    return None


@router.put("/api/chapters/{id}/reorder", response_model=ChapterResponse)
async def reorder_chapter(id: int, body: ChapterReorder, user: dict = Depends(get_current_user), db=Depends(get_db)):
    chapter_dict, _ = await _verify_chapter_ownership(id, user, db)
    project_id = chapter_dict["project_id"]
    old_position = chapter_dict["position"]
    new_position = body.new_position

    # Get total number of chapters for this project
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM chapters WHERE project_id = ?", (project_id,))
    row = await cursor.fetchone()
    total = row["cnt"]

    # Clamp new_position
    if new_position < 0:
        new_position = 0
    if new_position >= total:
        new_position = total - 1

    if old_position == new_position:
        # No change needed, return as-is
        return chapter_dict

    if new_position < old_position:
        # Moving up: shift others down (positions between new and old go +1)
        await db.execute(
            "UPDATE chapters SET position = position + 1 WHERE project_id = ? AND position >= ? AND position < ?",
            (project_id, new_position, old_position),
        )
    else:
        # Moving down: shift others up (positions between old and new go -1)
        await db.execute(
            "UPDATE chapters SET position = position - 1 WHERE project_id = ? AND position > ? AND position <= ?",
            (project_id, old_position, new_position),
        )

    await db.execute(
        "UPDATE chapters SET position = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_position, id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM chapters WHERE id = ?", (id,))
    updated = await cursor.fetchone()
    return dict(updated)
