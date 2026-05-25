from fastapi import APIRouter, Depends, HTTPException, status
from app.database import get_db
from app.auth import get_current_user
from app.models import ProjectCreate, ProjectUpdate, ProjectResponse
from app.tokenizer import get_token_count, count_words
import html
import json
import re
import unicodedata

router = APIRouter(prefix="")


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


@router.get("/api/projects")
async def list_projects(user: dict = Depends(get_current_user), db=Depends(get_db)):
    cursor = await db.execute(
        """SELECT p.id, p.user_id, p.title, p.description, p.created_at, p.updated_at, p.last_accessed,
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

        # Fetch all chapter content
        cursor_ch = await db.execute("SELECT content FROM chapters WHERE project_id = ?", (d["id"],))
        chapters = await cursor_ch.fetchall()

        # Fetch all wiki content (including metadata for subcategories, notepad, etc.)
        cursor_wiki = await db.execute("SELECT content, metadata_json FROM wiki_entries WHERE project_id = ?", (d["id"],))
        wikis = await cursor_wiki.fetchall()

        total_words = 0
        total_tokens = 0
        for ch in chapters:
            text = _strip_html(ch[0] or "")
            total_words += count_words(text)
            total_tokens += get_token_count(text)
        for w in wikis:
            text = _strip_html(w[0] or "")
            total_words += count_words(text)
            total_tokens += get_token_count(text)
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
                snippet = meta.get('ai_context_snippet', '')
                if snippet:
                    total_words += count_words(snippet)
                    total_tokens += get_token_count(snippet)
                notepad = meta.get('private_notepad', '')
                if notepad:
                    plain = _strip_html(notepad)
                    total_words += count_words(plain)
                    total_tokens += get_token_count(plain)
                attrs = meta.get('attributes', {})
                for attr_val in attrs.values():
                    if attr_val:
                        total_words += count_words(str(attr_val))
                        total_tokens += get_token_count(str(attr_val))
            except:
                pass

        d["total_words"] = total_words
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
