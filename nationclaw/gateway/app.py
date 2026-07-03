"""FastAPI application for the NationClaw Gateway."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from nationclaw.agent import AutoAgent
from nationclaw.config import AgentConfig, CustomArgParser
from nationclaw.main import configure_logging

from .manager import ConnectionManager
from .schemas import (
    DeviceInfo,
    EventEnvelope,
    EventType,
    HealthResponse,
    TaskCancelResponse,
    TaskRecordResponse,
    TaskStatus,
    TaskSubmitRequest,
    TaskSubmitResponse,
)
from .task_runtime import GatewayTaskManager

logger = logging.getLogger(__name__)


def load_agent_config(config_path: str) -> AgentConfig:
    parser = CustomArgParser((AgentConfig,))
    abs_path = os.path.abspath(config_path)
    if abs_path.endswith(".json"):
        config = parser.parse_json_file(json_file=abs_path)[0]
    elif abs_path.endswith((".yaml", ".yml")):
        config = parser.parse_yaml_file(yaml_file=abs_path)[0]
    else:
        raise ValueError("Gateway config path must end with .json, .yaml, or .yml")
    return config


def create_app(config_path: Optional[str] = None, agent: Optional[AutoAgent] = None) -> FastAPI:
    """Create a Gateway FastAPI app.

    Either `config_path` or an already constructed `agent` must be provided.
    """
    if agent is None and not config_path:
        raise ValueError("create_app requires config_path or agent")

    app = FastAPI(title="NationClaw Gateway", version="0.1.0")
    connection_manager = ConnectionManager()

    @app.on_event("startup")
    async def startup() -> None:
        nonlocal agent
        if agent is None:
            config = load_agent_config(config_path)  # type: ignore[arg-type]
            configure_logging(config.log_level)
            agent = AutoAgent(config)
        app.state.agent = agent
        app.state.connection_manager = connection_manager
        app.state.task_manager = GatewayTaskManager(agent, connection_manager, asyncio.get_running_loop())
        logger.info("NationClaw Gateway started")

    @app.on_event("shutdown")
    async def shutdown() -> None:
        task_manager: GatewayTaskManager = app.state.task_manager
        task_manager.shutdown()
        runtime_agent: AutoAgent = app.state.agent
        runtime_agent.stop()
        logger.info("NationClaw Gateway stopped")

    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(status="ok")

    @app.get("/devices", response_model=list[DeviceInfo])
    async def devices() -> list[DeviceInfo]:
        runtime_agent: AutoAgent = app.state.agent
        return [
            DeviceInfo(name=name, description=description)
            for name, description in runtime_agent.device_manager.get_available_devices()
        ]

    @app.post("/tasks", response_model=TaskSubmitResponse)
    async def submit_task(request: TaskSubmitRequest) -> TaskSubmitResponse:
        task_manager: GatewayTaskManager = app.state.task_manager
        record = task_manager.submit(request)
        return TaskSubmitResponse(task_id=record.task_id, status=record.status)

    @app.get("/tasks", response_model=list[TaskRecordResponse])
    async def list_tasks() -> list[TaskRecordResponse]:
        task_manager: GatewayTaskManager = app.state.task_manager
        return [record.to_response() for record in task_manager.list()]

    @app.get("/tasks/{task_id}", response_model=TaskRecordResponse)
    async def get_task(task_id: str) -> TaskRecordResponse:
        task_manager: GatewayTaskManager = app.state.task_manager
        try:
            return task_manager.get(task_id).to_response()
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    @app.post("/tasks/{task_id}/cancel", response_model=TaskCancelResponse)
    async def cancel_task(task_id: str) -> TaskCancelResponse:
        task_manager: GatewayTaskManager = app.state.task_manager
        try:
            record = task_manager.cancel(task_id)
            return TaskCancelResponse(
                task_id=record.task_id,
                status=record.status,
                cancel_requested=record.cancel_requested,
            )
        except KeyError:
            raise HTTPException(status_code=404, detail=f"Task not found: {task_id}")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await connection_manager.connect(websocket)
        session_id = websocket.query_params.get("session_id")
        try:
            while True:
                raw_event = await websocket.receive_json()
                event = EventEnvelope(**raw_event)
                task_manager: GatewayTaskManager = app.state.task_manager

                if event.type == EventType.TASK_SUBMIT.value:
                    request = TaskSubmitRequest(**event.payload)
                    record = task_manager.submit(
                        request,
                        request_id=event.requestId,
                        session_id=event.sessionId or session_id,
                    )
                    await connection_manager.broadcast(
                        EventEnvelope(
                            type=EventType.TASK_STATUS.value,
                            requestId=event.requestId,
                            sessionId=event.sessionId or session_id,
                            payload={
                                "task_id": record.task_id,
                                "status": record.status.value,
                                "message": "Task accepted",
                            },
                        )
                    )
                elif event.type == EventType.TASK_CANCEL.value:
                    task_id = event.payload.get("task_id")
                    if not task_id:
                        await connection_manager.send_error(
                            "task.cancel requires payload.task_id",
                            request_id=event.requestId,
                            session_id=event.sessionId or session_id,
                        )
                        continue
                    try:
                        record = task_manager.cancel(
                            task_id,
                            request_id=event.requestId,
                            session_id=event.sessionId or session_id,
                        )
                        await connection_manager.broadcast(
                            EventEnvelope(
                                type=EventType.TASK_STATUS.value,
                                requestId=event.requestId,
                                sessionId=event.sessionId or session_id,
                                payload={
                                    "task_id": record.task_id,
                                    "status": record.status.value,
                                    "cancel_requested": record.cancel_requested,
                                    "message": "Cancellation requested",
                                },
                            )
                        )
                    except KeyError:
                        await connection_manager.send_error(
                            f"Task not found: {task_id}",
                            request_id=event.requestId,
                            session_id=event.sessionId or session_id,
                        )
                else:
                    await connection_manager.send_error(
                        f"Unsupported event type: {event.type}",
                        request_id=event.requestId,
                        session_id=event.sessionId or session_id,
                    )
        except WebSocketDisconnect:
            await connection_manager.disconnect(websocket)
        except Exception as exc:
            logger.exception("Gateway WebSocket error: %s", exc)
            await connection_manager.disconnect(websocket)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the NationClaw Gateway")
    parser.add_argument("config", help="Path to NationClaw YAML/JSON config file")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to localhost for safety.")
    parser.add_argument("--port", type=int, default=11825, help="Gateway port")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload")
    args = parser.parse_args()

    app = create_app(config_path=args.config)
    uvicorn.run(app, host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
