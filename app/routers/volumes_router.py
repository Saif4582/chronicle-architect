from fastapi import APIRouter, Depends, HTTPException, status
import html
import re
import unicodedata
from app.database import get_db
from app.auth import get_current_user
from app.models import VolumeCreate, VolumeUpdate, VolumeResponse, VolumeReorder, ChapterAssignVolume, ChapterResponse
from app.tokenizer import get_token_count


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


router = APIRouter(prefix="")


# --- Helper: verify volume ownership through project ---


async def _verify_volume_ownership(volume_id: int, user: dict, db):
    """Fetch volume, verify its project belongs to the user. Returns (volume_dict, project_dict)."""
    cursor = await db.execute("SELECT * FROM volumes WHERE id = ?", (volume_id,))
    volume = await cursor.fetchone()
    if volume is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Volume not found")

    volume_dict = dict(volume)

    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (volume_dict["project_id"],))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    return volume_dict, project_dict


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


# --- Volume CRUD ---


@router.get("/api/projects/{project_id}/volumes")
async def list_volumes(project_id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    # Verify project ownership
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    cursor = await db.execute(
        "SELECT * FROM volumes WHERE project_id = ?",
        (project_id,),
    )
    rows = await cursor.fetchall()

    def _natural_sort_key(volume):
        title = volume.get("title", "") or ""
        import re
        match = re.match(r"^(\d+)", title)
        if match:
            return (0, int(match.group(1)), title.lower())
        return (1, 0, title.lower())

    volumes = [dict(r) for r in rows]
    volumes.sort(key=_natural_sort_key)
    return volumes


@router.post("/api/projects/{project_id}/volumes", response_model=VolumeResponse, status_code=status.HTTP_201_CREATED)
async def create_volume(project_id: int, body: VolumeCreate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    # Verify project ownership
    cursor = await db.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    project = await cursor.fetchone()
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")

    project_dict = dict(project)
    if project_dict["user_id"] != user["id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your project")

    # Duplicate volume title check (case-insensitive)
    cursor = await db.execute(
        "SELECT id FROM volumes WHERE project_id = ? AND LOWER(title) = LOWER(?)",
        (project_id, body.title),
    )
    existing = await cursor.fetchone()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A volume with this name already exists.",
        )

    # Auto-assign position
    cursor = await db.execute("SELECT MAX(position) as max_pos FROM volumes WHERE project_id = ?", (project_id,))
    row = await cursor.fetchone()
    max_pos = row["max_pos"] if row["max_pos"] is not None else -1
    new_position = max_pos + 1

    cursor = await db.execute(
        "INSERT INTO volumes (project_id, title, position) VALUES (?, ?, ?)",
        (project_id, body.title, new_position),
    )
    await db.commit()
    volume_id = cursor.lastrowid

    cursor = await db.execute("SELECT * FROM volumes WHERE id = ?", (volume_id,))
    volume = await cursor.fetchone()
    return dict(volume)


@router.get("/api/volumes/{id}", response_model=VolumeResponse)
async def get_volume(id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    volume_dict, _ = await _verify_volume_ownership(id, user, db)
    return volume_dict


@router.put("/api/volumes/{id}", response_model=VolumeResponse)
async def update_volume(id: int, body: VolumeUpdate, user: dict = Depends(get_current_user), db=Depends(get_db)):
    volume_dict, _ = await _verify_volume_ownership(id, user, db)

    # Duplicate volume title check (case-insensitive)
    if body.title is not None:
        cursor = await db.execute(
            "SELECT id FROM volumes WHERE project_id = ? AND LOWER(title) = LOWER(?) AND id != ?",
            (volume_dict["project_id"], body.title, id),
        )
        existing = await cursor.fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A volume with this name already exists.",
            )

    new_title = body.title if body.title is not None else volume_dict["title"]
    new_position = body.position if body.position is not None else volume_dict["position"]

    await db.execute(
        "UPDATE volumes SET title = ?, position = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_title, new_position, id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM volumes WHERE id = ?", (id,))
    updated = await cursor.fetchone()
    return dict(updated)


@router.delete("/api/volumes/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_volume(id: int, user: dict = Depends(get_current_user), db=Depends(get_db)):
    volume_dict, _ = await _verify_volume_ownership(id, user, db)
    project_id = volume_dict["project_id"]
    old_position = volume_dict["position"]

    # Set volume_id = NULL for all chapters that reference this volume
    await db.execute("UPDATE chapters SET volume_id = NULL WHERE volume_id = ?", (id,))

    # Delete the volume
    await db.execute("DELETE FROM volumes WHERE id = ?", (id,))

    # Reorder remaining volumes: shift positions down
    await db.execute(
        "UPDATE volumes SET position = position - 1 WHERE project_id = ? AND position > ?",
        (project_id, old_position),
    )
    await db.commit()
    return None


@router.put("/api/volumes/{id}/reorder", response_model=VolumeResponse)
async def reorder_volume(id: int, body: VolumeReorder, user: dict = Depends(get_current_user), db=Depends(get_db)):
    volume_dict, _ = await _verify_volume_ownership(id, user, db)
    project_id = volume_dict["project_id"]
    old_position = volume_dict["position"]
    new_position = body.new_position

    # Get total number of volumes for this project
    cursor = await db.execute("SELECT COUNT(*) as cnt FROM volumes WHERE project_id = ?", (project_id,))
    row = await cursor.fetchone()
    total = row["cnt"]

    # Clamp new_position
    if new_position < 0:
        new_position = 0
    if new_position >= total:
        new_position = total - 1

    if old_position == new_position:
        # No change needed, return as-is
        return volume_dict

    if new_position < old_position:
        # Moving up: shift others down (positions between new and old go +1)
        await db.execute(
            "UPDATE volumes SET position = position + 1 WHERE project_id = ? AND position >= ? AND position < ?",
            (project_id, new_position, old_position),
        )
    else:
        # Moving down: shift others up (positions between old and new go -1)
        await db.execute(
            "UPDATE volumes SET position = position - 1 WHERE project_id = ? AND position > ? AND position <= ?",
            (project_id, old_position, new_position),
        )

    await db.execute(
        "UPDATE volumes SET position = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_position, id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM volumes WHERE id = ?", (id,))
    updated = await cursor.fetchone()
    return dict(updated)


# --- Chapter Volume Assignment ---


@router.put("/api/chapters/{chapter_id}/assign-volume", response_model=ChapterResponse)
async def assign_chapter_volume(chapter_id: int, body: ChapterAssignVolume, user: dict = Depends(get_current_user), db=Depends(get_db)):
    chapter_dict, _ = await _verify_chapter_ownership(chapter_id, user, db)

    if body.volume_id is not None:
        # Verify the volume belongs to the same project as the chapter
        cursor = await db.execute("SELECT * FROM volumes WHERE id = ?", (body.volume_id,))
        volume = await cursor.fetchone()
        if volume is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Volume not found")

        volume_dict = dict(volume)
        if volume_dict["project_id"] != chapter_dict["project_id"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Volume does not belong to the same project as the chapter",
            )

    await db.execute(
        "UPDATE chapters SET volume_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (body.volume_id, chapter_id),
    )
    await db.commit()

    cursor = await db.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,))
    updated = await cursor.fetchone()
    result = dict(updated)
    plain_text = _strip_html(result.get("content", ""))
    result["token_count"] = get_token_count(plain_text)
    return result
