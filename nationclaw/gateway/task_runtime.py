"""Real task execution bridge between Gateway clients and AutoAgent."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from uuid import uuid4

from nationclaw.agent import AutoAgent

from .manager import ConnectionManager
from .schemas import (
    EventEnvelope,
    EventType,
    TaskRecordResponse,
    TaskStatus,
    TaskSubmitRequest,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


@dataclass
class TaskRecord:
    task_id: str
    request_id: str
    task: str
    status: TaskStatus
    device: Optional[str] = None
    mode: str = "normal"
    max_steps: Optional[int] = None
    created_at: str = field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    cancel_requested: bool = False
    result: Optional[List[str]] = None
    error: Optional[str] = None
    metadata: Dict = field(default_factory=dict)
    cancellation_event: threading.Event = field(default_factory=threading.Event)
    future: Optional[Future] = None

    def to_response(self) -> TaskRecordResponse:
        return TaskRecordResponse(
            task_id=self.task_id,
            request_id=self.request_id,
            status=self.status,
            task=self.task,
            device=self.device,
            mode=self.mode,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            cancel_requested=self.cancel_requested,
            result=self.result,
            error=self.error,
            metadata=self.metadata,
        )


class GatewayTaskManager:
    """Serializes AutoAgent task execution and streams real task events."""

    def __init__(
        self,
        agent: AutoAgent,
        connection_manager: ConnectionManager,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.agent = agent
        self.connection_manager = connection_manager
        self.loop = loop
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="nationclaw-gateway-task")
        self._tasks: Dict[str, TaskRecord] = {}
        self._lock = threading.RLock()
        self._active_record: Optional[TaskRecord] = None
        self._active_session_id: Optional[str] = None
        self._shutdown = False
        self.agent.add_log_listener(self._handle_agent_log)

    def submit(self, request: TaskSubmitRequest, *, request_id: Optional[str] = None, session_id: Optional[str] = None) -> TaskRecord:
        if self._shutdown:
            raise RuntimeError("GatewayTaskManager is shut down")

        task_id = f"task_{uuid4().hex}"
        record = TaskRecord(
            task_id=task_id,
            request_id=request_id or task_id,
            task=request.task,
            status=TaskStatus.QUEUED,
            device=request.device,
            mode=request.mode,
            max_steps=request.max_steps,
            metadata=request.metadata,
        )
        with self._lock:
            self._tasks[task_id] = record
            record.future = self._executor.submit(self._run_task, record, session_id)

        self._publish(
            EventEnvelope(
                type=EventType.TASK_STATUS.value,
                requestId=record.request_id,
                sessionId=session_id,
                payload={
                    "task_id": task_id,
                    "status": TaskStatus.QUEUED.value,
                    "message": "Task queued",
                },
            )
        )
        return record

    def cancel(self, task_id: str, *, request_id: Optional[str] = None, session_id: Optional[str] = None) -> TaskRecord:
        record = self.get(task_id)
        record.cancel_requested = True
        record.cancellation_event.set()

        if record.status == TaskStatus.QUEUED and record.future and record.future.cancel():
            record.status = TaskStatus.CANCELLED
            record.finished_at = utc_now_iso()

        self._publish(
            EventEnvelope(
                type=EventType.TASK_STATUS.value,
                requestId=request_id or record.request_id,
                sessionId=session_id,
                payload={
                    "task_id": task_id,
                    "status": record.status.value,
                    "cancel_requested": True,
                    "message": "Cancellation requested",
                },
            )
        )
        return record

    def get(self, task_id: str) -> TaskRecord:
        with self._lock:
            record = self._tasks.get(task_id)
        if record is None:
            raise KeyError(task_id)
        return record

    def list(self) -> List[TaskRecord]:
        with self._lock:
            return list(self._tasks.values())

    def shutdown(self) -> None:
        self._shutdown = True
        with self._lock:
            records = list(self._tasks.values())
        for record in records:
            record.cancellation_event.set()
        try:
            self.agent.remove_log_listener(self._handle_agent_log)
        except Exception:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _run_task(self, record: TaskRecord, session_id: Optional[str]) -> None:
        with self._lock:
            self._active_record = record
            self._active_session_id = session_id

        record.status = TaskStatus.RUNNING
        record.started_at = utc_now_iso()
        self._publish_status(record, session_id, "Task started")

        try:
            if record.cancellation_event.is_set():
                record.status = TaskStatus.CANCELLED
                record.finished_at = utc_now_iso()
                self._publish_result(record, session_id, "Task cancelled before execution")
                return

            max_steps = record.max_steps
            if max_steps is None:
                config_max_steps = getattr(self.agent.config, "max_steps", -1)
                max_steps = config_max_steps if isinstance(config_max_steps, int) and config_max_steps > 0 else 30

            task_text = record.task
            if record.device:
                task_text = f"Use device `{record.device}` when device interaction is needed.\n\n{record.task}"

            result = self.agent.execute_task(
                task_text,
                max_steps=max_steps,
                mode=record.mode,
                cancellation_event=record.cancellation_event,
            )
            record.result = [str(item) for item in result if isinstance(item, str)]

            if record.cancellation_event.is_set() or record.cancel_requested:
                record.status = TaskStatus.CANCELLED
                message = "Task cancelled"
            else:
                record.status = TaskStatus.FINISHED
                message = "Task finished"

            record.finished_at = utc_now_iso()
            self._publish_result(record, session_id, message)
        except Exception as exc:
            logger.exception("Gateway task failed: %s", exc)
            record.status = TaskStatus.FAILED
            record.error = str(exc)
            record.finished_at = utc_now_iso()
            self._publish(
                EventEnvelope(
                    type=EventType.ERROR.value,
                    requestId=record.request_id,
                    sessionId=session_id,
                    payload={
                        "task_id": record.task_id,
                        "status": TaskStatus.FAILED.value,
                        "message": str(exc),
                    },
                )
            )
            self._publish_result(record, session_id, "Task failed")
        finally:
            with self._lock:
                if self._active_record is record:
                    self._active_record = None
                    self._active_session_id = None

    def _handle_agent_log(self, content: str) -> None:
        with self._lock:
            record = self._active_record
            session_id = self._active_session_id
        if record is None:
            return
        self._publish(
            EventEnvelope(
                type=EventType.AGENT_LOG.value,
                requestId=record.request_id,
                sessionId=session_id,
                payload={
                    "task_id": record.task_id,
                    "message": content,
                },
            )
        )

    def _publish_status(self, record: TaskRecord, session_id: Optional[str], message: str) -> None:
        self._publish(
            EventEnvelope(
                type=EventType.TASK_STATUS.value,
                requestId=record.request_id,
                sessionId=session_id,
                payload={
                    "task_id": record.task_id,
                    "status": record.status.value,
                    "message": message,
                    "started_at": record.started_at,
                    "finished_at": record.finished_at,
                    "cancel_requested": record.cancel_requested,
                },
            )
        )

    def _publish_result(self, record: TaskRecord, session_id: Optional[str], message: str) -> None:
        self._publish_status(record, session_id, message)
        self._publish(
            EventEnvelope(
                type=EventType.TASK_RESULT.value,
                requestId=record.request_id,
                sessionId=session_id,
                payload=record.to_response().model_dump() if hasattr(record.to_response(), "model_dump") else record.to_response().dict(),
            )
        )

    def _publish(self, event: EventEnvelope) -> None:
        future = asyncio.run_coroutine_threadsafe(self.connection_manager.broadcast(event), self.loop)
        future.add_done_callback(self._log_publish_failure)

    @staticmethod
    def _log_publish_failure(future) -> None:
        try:
            future.result()
        except Exception as exc:
            logger.warning("Failed to publish Gateway event: %s", exc)
