import json
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from app.database import init_db
from app.routers.auth_router import router as auth_router
from app.routers.projects_router import router as projects_router
from app.routers.chapters_router import router as chapters_router
from app.routers.volumes_router import router as volumes_router
from app.routers.wiki_router import router as wiki_router
from app.routers.admin_router import router as admin_router
from app.rate_limit import rate_limit_middleware
from app.tokenizer import get_token_count
from app.auth import get_current_user
from app.models import TokenizeRequest, TokenizeResponse

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    await init_db()
    yield


app = FastAPI(title="Chronicle Architect", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(rate_limit_middleware)


app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(chapters_router)
app.include_router(volumes_router)
app.include_router(wiki_router)
app.include_router(admin_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.post("/api/tokenize")
async def tokenize(body: TokenizeRequest, user: dict = Depends(get_current_user)):
    tokens = get_token_count(body.text)
    return TokenizeResponse(tokens=tokens)


@app.get("/version.json")
async def get_version():
    with open(os.path.join(BASE_DIR, "version.json")) as f:
        return json.load(f)


app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True), name="static")
