"""Pydantic schemas and event helpers for the NationClaw Gateway."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    PAUSED = "paused"
    FINISHED = "finished"
    FAILED = "failed"
    INFEASIBLE = "infeasible"
    CANCELLED = "cancelled"


class EventType(str, Enum):
    TASK_SUBMIT = "task.submit"
    TASK_CANCEL = "task.cancel"
    TASK_STATUS = "task.status"
    TASK_RESULT = "task.result"
    AGENT_LOG = "agent.log"
    DEVICE_SCREENSHOT = "device.screenshot"
    APPROVAL_REQUEST = "approval.request"
    APPROVAL_RESPONSE = "approval.response"
    ERROR = "error"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_request_id(prefix: str = "req") -> str:
    return f"{prefix}_{uuid4().hex}"


class EventEnvelope(BaseModel):
    type: str
    requestId: str = Field(default_factory=new_request_id)
    sessionId: Optional[str] = None
    timestamp: str = Field(default_factory=utc_now_iso)
    payload: Dict[str, Any] = Field(default_factory=dict)


class TaskSubmitRequest(BaseModel):
    task: str = Field(..., min_length=1)
    device: Optional[str] = None
    mode: str = "normal"
    max_steps: Optional[int] = Field(default=None, ge=1)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TaskSubmitResponse(BaseModel):
    task_id: str
    status: TaskStatus


class TaskCancelResponse(BaseModel):
    task_id: str
    status: TaskStatus
    cancel_requested: bool


class TaskRecordResponse(BaseModel):
    task_id: str
    request_id: str
    status: TaskStatus
    task: str
    device: Optional[str] = None
    mode: str
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancel_requested: bool = False
    result: Optional[List[str]] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    status: str
    service: str = "nationclaw-gateway"
    timestamp: str = Field(default_factory=utc_now_iso)


class DeviceInfo(BaseModel):
    name: str
    description: str


class ErrorResponse(BaseModel):
    error: str
