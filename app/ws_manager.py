"""
WebSocket Connection Manager for Chronicle Architect.

Provides a singleton ConnectionManager class that tracks connected clients
and their channel subscriptions. Two module-level instances are created:

- ``chat_manager``  — for ``/ws/chat``  (DM, group, global chat)
- ``admin_manager`` — for ``/ws/admin`` (users, logs, tokens)
"""

import json
from fastapi import WebSocket


class ConnectionManager:
    """Tracks active WebSocket connections and channel subscriptions."""

    def __init__(self):
        # user_id → WebSocket
        self.active_connections: dict[int, WebSocket] = {}
        # channel (str) → set of user_ids
        self.subscriptions: dict[str, set[int]] = {}

    async def connect(self, websocket: WebSocket, user_id: int) -> None:
        """Register a newly authenticated WebSocket connection."""
        # Close any previous connection for this user (single-connection policy)
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].close()
            except Exception:
                pass
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id: int, websocket: WebSocket | None = None) -> None:
        """Remove a user from active connections and all channel subscriptions.

        If *websocket* is provided, the user is only removed when the stored
        WebSocket matches *websocket* (prevents a stale disconnect from
        wiping the active connection after a reconnect).
        """
        if websocket is not None:
            current = self.active_connections.get(user_id)
            if current is not websocket:
                return  # Stale disconnect — a newer connection has replaced this one
        self.active_connections.pop(user_id, None)
        for channel in list(self.subscriptions.keys()):
            subs = self.subscriptions[channel]
            subs.discard(user_id)
            if not subs:
                del self.subscriptions[channel]

    def subscribe(self, user_id: int, channel: str) -> None:
        """Subscribe a user to a channel."""
        if channel not in self.subscriptions:
            self.subscriptions[channel] = set()
        self.subscriptions[channel].add(user_id)

    def unsubscribe(self, user_id: int, channel: str) -> None:
        """Unsubscribe a user from a channel."""
        if channel in self.subscriptions:
            self.subscriptions[channel].discard(user_id)
            if not self.subscriptions[channel]:
                del self.subscriptions[channel]

    def get_subscriptions(self, user_id: int) -> list[str]:
        """Return all channels a user is currently subscribed to."""
        return [ch for ch, subs in self.subscriptions.items() if user_id in subs]

    async def send_to_user(self, user_id: int, data: dict) -> bool:
        """Send a JSON message to a specific user. Returns True if sent."""
        ws = self.active_connections.get(user_id)
        if ws is None:
            return False
        try:
            await ws.send_json(data)
            return True
        except Exception:
            self.disconnect(user_id)
            return False

    async def broadcast(self, channel: str, data: dict) -> int:
        """Send a JSON message to all users subscribed to *channel*.

        Returns the number of recipients the message was sent to.
        """
        user_ids = self.subscriptions.get(channel, set())
        count = 0
        dead: list[int] = []
        for uid in list(user_ids):
            ws = self.active_connections.get(uid)
            if ws is None:
                dead.append(uid)
                continue
            try:
                await ws.send_json(data)
                count += 1
            except Exception:
                dead.append(uid)
        # Clean up dead subscribers
        for uid in dead:
            self.disconnect(uid)
        return count


# ---------------------------------------------------------------------------
# Singleton instances
# ---------------------------------------------------------------------------

chat_manager = ConnectionManager()
"""Manager for ``/ws/chat`` — DM, group, and global chat real-time messages."""

admin_manager = ConnectionManager()
"""Manager for ``/ws/admin`` — admin panel live updates (users, logs, tokens)."""
