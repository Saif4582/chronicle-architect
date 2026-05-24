from pydantic import BaseModel, ConfigDict
from typing import Optional


class UserCreate(BaseModel):
    username: str
    password: str


class UserLogin(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    token: str


class ProjectCreate(BaseModel):
    title: str
    description: str = ""


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None


class ProjectResponse(BaseModel):
    id: int
    user_id: int
    title: str
    description: str
    content: str
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class SetupResponse(BaseModel):
    setup_required: bool


class ChapterCreate(BaseModel):
    title: str = "Untitled"


class ChapterUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    position: Optional[int] = None


class ChapterResponse(BaseModel):
    id: int
    project_id: int
    title: str
    content: str
    position: int
    created_at: str
    updated_at: str
    model_config = ConfigDict(from_attributes=True)


class ChapterReorder(BaseModel):
    new_position: int


class WikiEntryCreate(BaseModel):
    name: str
    category: str
    parent_id: Optional[int] = None
    content: str = ""


class WikiEntryUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    parent_id: Optional[int] = None
    content: Optional[str] = None


class WikiEntryResponse(BaseModel):
    id: int
    project_id: int
    name: str
    category: str
    parent_id: Optional[int] = None
    content: str
    created_at: str
    updated_at: str
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    status: str


class TokenizeRequest(BaseModel):
    text: str


class TokenizeResponse(BaseModel):
    tokens: int
