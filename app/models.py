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
    last_accessed: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class SetupResponse(BaseModel):
    setup_required: bool


class ChapterCreate(BaseModel):
    title: str = "Untitled"
    volume_id: Optional[int] = None


class ChapterUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    position: Optional[int] = None
    volume_id: Optional[int] = None


class ChapterResponse(BaseModel):
    id: int
    project_id: int
    title: str
    content: str
    position: int
    volume_id: Optional[int] = None
    created_at: str
    updated_at: str
    model_config = ConfigDict(from_attributes=True)


class ChapterReorder(BaseModel):
    new_position: int


class VolumeCreate(BaseModel):
    title: str = "Untitled Volume"


class VolumeUpdate(BaseModel):
    title: Optional[str] = None
    position: Optional[int] = None


class VolumeResponse(BaseModel):
    id: int
    project_id: int
    title: str
    position: int
    created_at: str
    updated_at: str
    model_config = ConfigDict(from_attributes=True)


class VolumeReorder(BaseModel):
    new_position: int


class ChapterAssignVolume(BaseModel):
    volume_id: Optional[int] = None


class WikiEntryCreate(BaseModel):
    name: str
    category: str
    parent_id: Optional[int] = None
    content: str = ""
    metadata_json: str = "{}"
    parents: Optional[list[int]] = None


class WikiEntryUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    parent_id: Optional[int] = None
    content: Optional[str] = None
    metadata_json: Optional[str] = None
    parents: Optional[list[int]] = None


class WikiEntryResponse(BaseModel):
    id: int
    project_id: int
    name: str
    category: str
    parent_id: Optional[int] = None
    content: str
    metadata_json: str = "{}"
    created_at: str
    updated_at: str
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(BaseModel):
    status: str


class TokenizeRequest(BaseModel):
    text: str


class TokenizeResponse(BaseModel):
    tokens: int


class AdminSettingsUpdate(BaseModel):
    lockout_threshold: Optional[int] = None
    lockout_duration_minutes: Optional[int] = None


class UserProfileUpdate(BaseModel):
    display_name: Optional[str] = None
    new_password: Optional[str] = None
    new_username: Optional[str] = None


class AdminUserUpdate(BaseModel):
    username: Optional[str] = None
    display_name: Optional[str] = None
    new_password: Optional[str] = None


class ReorderRequest(BaseModel):
    order: list[int]
