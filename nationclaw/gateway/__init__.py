"""Gateway API for NationClaw.

The gateway exposes REST and WebSocket interfaces for submitting tasks,
streaming task progress, cancelling running work, and inspecting runtime state.
"""

from .app import create_app
from .task_runtime import GatewayTaskManager

__all__ = ["create_app", "GatewayTaskManager"]
