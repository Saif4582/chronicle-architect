import json
import logging
import os
import asyncio
import traceback
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from app.database import init_db, DB_PATH
from app.routers.auth_router import router as auth_router
from app.routers.projects_router import router as projects_router
from app.routers.chapters_router import router as chapters_router
from app.routers.volumes_router import router as volumes_router
from app.routers.wiki_router import router as wiki_router
from app.routers.admin_router import router as admin_router
from app.routers.ai_router import router as ai_router
from app.routers.chat_router import router as chat_router
from app.rate_limit import rate_limit_middleware
from app.tokenizer import get_token_count
from app.auth import get_current_user, decode_jwt
from app.config import get_settings
from app.ws_manager import chat_manager, admin_manager
from app.models import TokenizeRequest, TokenizeResponse

logger = logging.getLogger(__name__)

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
app.include_router(ai_router)
app.include_router(chat_router)


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


# ---------------------------------------------------------------------------
# WebSocket: Chat (DM, Group, Global)
# ---------------------------------------------------------------------------


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket):
    await websocket.accept()
    user_id: int | None = None

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "Invalid JSON"})
                continue

            msg_type = msg.get("type", "")
            print(f"[WS:chat] User {user_id or '?'} → received: {msg_type}")

            # --- AUTH ---
            if msg_type == "auth":
                token = msg.get("token", "")
                settings = get_settings()
                payload = decode_jwt(token, settings["SECRET_KEY"])
                if payload is None:
                    await websocket.send_json({"type": "auth_error", "detail": "Invalid or expired token"})
                    await websocket.close()
                    return

                uid = payload.get("sub")
                if uid is None:
                    await websocket.send_json({"type": "auth_error", "detail": "Invalid token payload"})
                    await websocket.close()
                    return

                user_id = int(uid)

                # Verify user exists and token version matches
                import aiosqlite
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute("SELECT id, token_version FROM users WHERE id = ?", (user_id,))
                    row = await cursor.fetchone()
                    if row is None:
                        await websocket.send_json({"type": "auth_error", "detail": "User not found"})
                        await websocket.close()
                        return
                    row = dict(row)
                    payload_version = payload.get("token_version", 0)
                    if payload_version != row.get("token_version", 0):
                        await websocket.send_json({"type": "auth_error", "detail": "Token has been revoked"})
                        await websocket.close()
                        return

                await chat_manager.connect(websocket, user_id)
                print(f"[WS:chat] User {user_id} connected successfully")
                await websocket.send_json({"type": "auth_ok", "user_id": user_id})

            # --- Must be authenticated for everything below ---
            elif user_id is None:
                await websocket.send_json({"type": "error", "detail": "Authenticate first: { type: 'auth', token: '...' }"})

            # --- SUBSCRIBE ---
            elif msg_type == "subscribe":
                channel = msg.get("channel", "")
                if not channel:
                    await websocket.send_json({"type": "error", "detail": "Missing 'channel' field"})
                    continue
                chat_manager.subscribe(user_id, channel)
                await websocket.send_json({"type": "subscribed", "channel": channel})

            # --- UNSUBSCRIBE ---
            elif msg_type == "unsubscribe":
                channel = msg.get("channel", "")
                if not channel:
                    await websocket.send_json({"type": "error", "detail": "Missing 'channel' field"})
                    continue
                chat_manager.unsubscribe(user_id, channel)
                await websocket.send_json({"type": "unsubscribed", "channel": channel})

            # --- PING (keep-alive) ---
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            else:
                await websocket.send_json({"type": "error", "detail": f"Unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        print(f"[WS:chat] User {user_id}: WebSocket disconnected by client")
    except Exception as e:
        print(f"[WS:chat] User {user_id}: Unexpected error — {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if user_id is not None:
            print(f"[WS:chat] User {user_id} disconnected (finally)")
            chat_manager.disconnect(user_id, websocket)


# ---------------------------------------------------------------------------
# WebSocket: Admin Panel (users, logs, tokens)
# ---------------------------------------------------------------------------


@app.websocket("/ws/admin")
async def ws_admin(websocket: WebSocket):
    await websocket.accept()
    user_id: int | None = None
    is_admin: bool = False

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json({"type": "error", "detail": "Invalid JSON"})
                continue

            msg_type = msg.get("type", "")
            print(f"[WS:admin] Admin user {user_id or '?'} → received: {msg_type}")

            # --- AUTH ---
            if msg_type == "auth":
                token = msg.get("token", "")
                settings = get_settings()
                payload = decode_jwt(token, settings["SECRET_KEY"])
                if payload is None:
                    await websocket.send_json({"type": "auth_error", "detail": "Invalid or expired token"})
                    await websocket.close()
                    return

                uid = payload.get("sub")
                if uid is None:
                    await websocket.send_json({"type": "auth_error", "detail": "Invalid token payload"})
                    await websocket.close()
                    return

                user_id = int(uid)

                # Verify user exists and is admin/owner
                import aiosqlite
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    cursor = await db.execute(
                        "SELECT id, role, token_version FROM users WHERE id = ?", (user_id,)
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        await websocket.send_json({"type": "auth_error", "detail": "User not found"})
                        await websocket.close()
                        return
                    row = dict(row)
                    if row["role"] not in ("admin", "owner"):
                        await websocket.send_json({"type": "auth_error", "detail": "Admin access required"})
                        await websocket.close()
                        return
                    payload_version = payload.get("token_version", 0)
                    if payload_version != row.get("token_version", 0):
                        await websocket.send_json({"type": "auth_error", "detail": "Token has been revoked"})
                        await websocket.close()
                        return
                    is_admin = True

                await admin_manager.connect(websocket, user_id)
                print(f"[WS:admin] Admin user {user_id} ({row['role']}) connected successfully")
                await websocket.send_json({"type": "auth_ok", "user_id": user_id, "role": row["role"]})

            # --- Must be authenticated ---
            elif user_id is None or not is_admin:
                await websocket.send_json({"type": "error", "detail": "Authenticate first: { type: 'auth', token: '...' }"})

            # --- SUBSCRIBE ---
            elif msg_type == "subscribe":
                channel = msg.get("channel", "")
                if channel not in ("users", "logs", "tokens"):
                    await websocket.send_json({"type": "error", "detail": f"Unknown admin channel: {channel}. Use 'users', 'logs', or 'tokens'."})
                    continue
                admin_manager.subscribe(user_id, channel)
                await websocket.send_json({"type": "subscribed", "channel": channel})

            # --- UNSUBSCRIBE ---
            elif msg_type == "unsubscribe":
                channel = msg.get("channel", "")
                if not channel:
                    await websocket.send_json({"type": "error", "detail": "Missing 'channel' field"})
                    continue
                admin_manager.unsubscribe(user_id, channel)
                await websocket.send_json({"type": "unsubscribed", "channel": channel})

            # --- PING ---
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})

            else:
                await websocket.send_json({"type": "error", "detail": f"Unknown message type: {msg_type}"})

    except WebSocketDisconnect:
        print(f"[WS:admin] Admin user {user_id}: WebSocket disconnected by client")
    except Exception as e:
        print(f"[WS:admin] Admin user {user_id}: Unexpected error — {type(e).__name__}: {e}")
        traceback.print_exc()
    finally:
        if user_id is not None:
            print(f"[WS:admin] Admin user {user_id} disconnected (finally)")
            admin_manager.disconnect(user_id, websocket)




# ---------------------------------------------------------------------------
# Static files (must be last)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=os.path.join(BASE_DIR, "static"), html=True), name="static")
