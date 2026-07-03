"""
NationClaw - Tool Registry.

Provides a small registry for declaring, discovering, validating, and executing
callable tools used by the intelligence engine.
"""

import asyncio
import inspect
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ToolCategory(Enum):
    DEVICE_CONTROL = "device_control"
    APP_MANAGEMENT = "app_management"
    WEB_AUTOMATION = "web_automation"
    FILE_OPERATIONS = "file_operations"
    COMMUNICATION = "communication"
    SYSTEM = "system"
    CUSTOM = "custom"


@dataclass
class ToolParameter:
    name: str
    type: str
    description: str
    required: bool = True
    default: Any = None
    enum: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "required": self.required,
            "default": self.default,
            "enum": self.enum,
        }


@dataclass
class ToolMetadata:
    name: str
    display_name: str
    description: str
    category: ToolCategory
    version: str = "1.0.0"
    author: str = "NationClaw"
    tags: List[str] = field(default_factory=list)
    requires_device: bool = False
    dependencies: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "category": self.category.value,
            "version": self.version,
            "author": self.author,
            "tags": self.tags,
            "requires_device": self.requires_device,
            "dependencies": self.dependencies,
        }


@dataclass
class Tool:
    metadata: ToolMetadata
    parameters: List[ToolParameter]
    function: Optional[Callable[..., Any]] = None
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metadata": self.metadata.to_dict(),
            "parameters": [parameter.to_dict() for parameter in self.parameters],
            "enabled": self.enabled,
        }


class ToolRegistry:
    """Registry for dynamic tool declaration, discovery, and execution."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self._categories: Dict[ToolCategory, List[str]] = {}
        self._dependencies: Dict[str, List[str]] = {}
        logger.info("ToolRegistry initialized")

    def register_tool(self, tool: Tool) -> bool:
        """Register or replace a tool."""
        name = tool.metadata.name
        if name in self._tools:
            logger.warning("Tool '%s' already registered; replacing it", name)

        self._tools[name] = tool

        category = tool.metadata.category
        self._categories.setdefault(category, [])
        if name not in self._categories[category]:
            self._categories[category].append(name)

        if tool.metadata.dependencies:
            self._dependencies[name] = tool.metadata.dependencies

        logger.info("Registered tool: %s", name)
        return True

    def register_function(
        self,
        func: Callable[..., Any],
        metadata: ToolMetadata,
        parameters: List[ToolParameter],
    ) -> bool:
        """Register a Python callable as a tool."""
        return self.register_tool(Tool(metadata=metadata, parameters=parameters, function=func))

    def get_tool(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list_tools(self) -> List[str]:
        return list(self._tools.keys())

    def list_by_category(self, category: ToolCategory) -> List[Tool]:
        return [self._tools[name] for name in self._categories.get(category, [])]

    def search_tools(self, query: str) -> List[Tool]:
        query_lower = query.lower()
        results: List[Tool] = []
        for tool in self._tools.values():
            metadata = tool.metadata
            if (
                query_lower in metadata.name.lower()
                or query_lower in metadata.display_name.lower()
                or query_lower in metadata.description.lower()
                or any(query_lower in tag.lower() for tag in metadata.tags)
            ):
                results.append(tool)
        return results

    def _validate_parameters(self, tool: Tool, parameters: Dict[str, Any]) -> Dict[str, Any]:
        validated: Dict[str, Any] = {}
        for parameter in tool.parameters:
            if parameter.name in parameters:
                value = parameters[parameter.name]
            elif parameter.required:
                raise ValueError(f"Required parameter '{parameter.name}' not provided")
            else:
                value = parameter.default

            if parameter.enum is not None and value not in parameter.enum:
                raise ValueError(
                    f"Parameter '{parameter.name}' must be one of {parameter.enum}; got {value!r}"
                )

            validated[parameter.name] = value
        return validated

    def execute_tool(self, name: str, parameters: Dict[str, Any]) -> Any:
        """Execute a registered synchronous tool."""
        tool = self.get_tool(name)
        if tool is None:
            raise ValueError(f"Tool '{name}' not found")
        if not tool.enabled:
            raise ValueError(f"Tool '{name}' is disabled")
        if tool.function is None:
            raise ValueError(f"Tool '{name}' has no function")

        validated = self._validate_parameters(tool, parameters)
        logger.info("Executing tool: %s", name)
        return tool.function(**validated)

    async def execute_tool_async(self, name: str, parameters: Dict[str, Any]) -> Any:
        """Execute a registered tool, supporting both async and sync callables."""
        tool = self.get_tool(name)
        if tool is None:
            raise ValueError(f"Tool '{name}' not found")
        if tool.function is None:
            raise ValueError(f"Tool '{name}' has no function")

        if inspect.iscoroutinefunction(tool.function):
            validated = self._validate_parameters(tool, parameters)
            logger.info("Executing async tool: %s", name)
            return await tool.function(**validated)

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.execute_tool, name, parameters)


def _annotation_to_type_name(annotation: Any) -> str:
    if annotation == inspect.Parameter.empty:
        return "string"
    if hasattr(annotation, "__name__"):
        return annotation.__name__
    return str(annotation)


def tool(
    name: str,
    display_name: str,
    description: str,
    category: ToolCategory,
    **kwargs: Any,
):
    """Decorator for registering a function in the default registry."""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(func)
        parameters: List[ToolParameter] = []
        for parameter_name, parameter in signature.parameters.items():
            parameters.append(
                ToolParameter(
                    name=parameter_name,
                    type=_annotation_to_type_name(parameter.annotation),
                    description=f"Parameter: {parameter_name}",
                    required=parameter.default == inspect.Parameter.empty,
                    default=None if parameter.default == inspect.Parameter.empty else parameter.default,
                )
            )

        metadata = ToolMetadata(
            name=name,
            display_name=display_name,
            description=description,
            category=category,
            **kwargs,
        )
        get_default_registry().register_function(func, metadata, parameters)
        return func

    return decorator


_default_registry: Optional[ToolRegistry] = None


def get_default_registry() -> ToolRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = ToolRegistry()
    return _default_registry


def set_default_registry(registry: ToolRegistry) -> None:
    global _default_registry
    _default_registry = registry
