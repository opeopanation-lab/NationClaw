"""WebSocket connection manager for Gateway event streaming."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional, Set

from fastapi import WebSocket

from .schemas import EventEnvelope

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks active WebSocket clients and broadcasts event envelopes."""

    def __init__(self) -> None:
        self._connections: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        async with self._lock:
            self._connections.add(websocket)
        logger.info("Gateway WebSocket connected; clients=%d", len(self._connections))

    async def disconnect(self, websocket: WebSocket) -> None:
        async with self._lock:
            self._connections.discard(websocket)
        logger.info("Gateway WebSocket disconnected; clients=%d", len(self._connections))

    async def broadcast(self, event: EventEnvelope) -> None:
        payload = event.model_dump() if hasattr(event, "model_dump") else event.dict()
        async with self._lock:
            connections = list(self._connections)

        stale: Set[WebSocket] = set()
        for websocket in connections:
            try:
                await websocket.send_json(payload)
            except Exception as exc:
                logger.warning("Dropping stale Gateway WebSocket client: %s", exc)
                stale.add(websocket)

        if stale:
            async with self._lock:
                for websocket in stale:
                    self._connections.discard(websocket)

    async def send_error(
        self,
        message: str,
        *,
        request_id: str,
        session_id: Optional[str] = None,
    ) -> None:
        await self.broadcast(
            EventEnvelope(
                type="error",
                requestId=request_id,
                sessionId=session_id,
                payload={"message": message},
            )
        )
