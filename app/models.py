from pydantic import BaseModel, ConfigDict
from typing import Optional, List


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


class WikiReorderRequest(BaseModel):
    order: list[int]
    category: Optional[str] = None


class WikiMoveRequest(BaseModel):
    entry_id: int
    category: Optional[str] = None
    parent_ids: Optional[List[int]] = None
    position: Optional[int] = None


# --- AI Endpoint Models ---

class AIEndpointCreate(BaseModel):
    name: str
    base_url: str
    api_key: str

class AIEndpointUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None  # Only set if changing; None = don't change

class AIEndpointModelUpdate(BaseModel):
    enabled: Optional[bool] = None
    multiplier_requests: Optional[float] = None
    multiplier_tokens: Optional[float] = None
    max_context_tokens: Optional[int] = None

class AIEndpointUserAssign(BaseModel):
    user_id: int
    limit_type: str = "requests"  # "requests", "tokens", "both"
    limit_value_requests: Optional[int] = None
    limit_value_tokens: Optional[int] = None
    reset_schedule: str = "daily"  # "daily", "weekly", "monthly"
    reset_time: Optional[str] = None  # hour (0-23) for daily, day name for weekly, date (1-28) for monthly
    is_shared_pool: bool = False
    shared_pool_id: Optional[int] = None
    config_id: Optional[int] = None  # Named config preset to link user to

class AIEndpointUserUpdate(BaseModel):
    limit_type: Optional[str] = None
    limit_value_requests: Optional[int] = None
    limit_value_tokens: Optional[int] = None
    reset_schedule: Optional[str] = None
    reset_time: Optional[str] = None
    is_shared_pool: Optional[bool] = None

# --- AI Endpoint Configuration (Presets) Models ---

class AIConfigCreate(BaseModel):
    name: str
    limit_type: str = "requests"
    limit_value_requests: Optional[int] = None
    limit_value_tokens: Optional[int] = None
    reset_schedule: str = "daily"
    reset_time: Optional[str] = None
    is_shared_pool: bool = False

class AIConfigUpdate(BaseModel):
    name: Optional[str] = None
    limit_type: Optional[str] = None
    limit_value_requests: Optional[int] = None
    limit_value_tokens: Optional[int] = None
    reset_schedule: Optional[str] = None
    reset_time: Optional[str] = None
    is_shared_pool: Optional[bool] = None

# --- AI Chat Models ---

class AIChatRequest(BaseModel):
    session_id: Optional[int] = None
    endpoint_id: Optional[int] = None
    model_name: Optional[str] = None
    config_id: Optional[int] = None  # Named config preset to use
    message: str
    context_selection: Optional[dict] = None  # { chapter_ids: [], volume_ids: [], wiki_ids: [] }
    reasoning: Optional[bool] = False

class AIChatSessionCreate(BaseModel):
    endpoint_id: int
    model_name: str
    title: str = "New Chat"
    context_selection: Optional[dict] = None
    config_id: Optional[int] = None  # Named config preset

class AIChatSessionUpdate(BaseModel):
    title: Optional[str] = None
    context_selection: Optional[dict] = None

# --- Chat (Communication) Models ---

class DMMessageSend(BaseModel):
    recipient_id: int
    content: str

class GroupCreate(BaseModel):
    name: str
    member_ids: List[int]

class GroupMessageSend(BaseModel):
    content: str

class GroupInvite(BaseModel):
    user_ids: List[int]

class GlobalMessageSend(BaseModel):
    content: str
