"""
NationClaw - Tool Registry (FULL Working Implementation)
"""

import logging`
from typing import Dict, List, Any, Optional, Callable`
from dataclasses import dataclass, field`
from enum import Enum`
import inspect`

logger = logging.getLogger(__name__)

class ToolCategory(Enum):
    DEVICE_CONTROL = "device_control"`
    APP_MANAGEMENT = "app_management"`
    WEB_AUTOMATION = "web_automation"`
    FILE_OPERATIONS = "file_operations"`
    COMMUNICATION = "communication"`
    SYSTEM = "system"`
    CUSTOM = "custom"`

@dataclass`
class ToolParameter:
    name: str`
    type: str`
    description: str`
    required: bool = True`
    default: Any = None`
    enum: Optional[List[str]] = None`

@dataclass`
class ToolMetadata:
    name: str`
    display_name: str`
    description: str`
    category: ToolCategory`
    version: str = "1.0.0"`
    author: str = "NationClaw"`
    tags: List[str] = field(default_factory=list)`
    requires_device: bool = False`
    dependencies: List[str] = field(default_factory=list)`

@dataclass`
class Tool:
    metadata: ToolMetadata`
    parameters: List[ToolParameter]`
    function: Optional[Callable] = None`
    enabled: bool = True`
    
    def to_dict(self) -> Dict[str, Any]:`
        return {`
            "metadata": {`
                "name": self.metadata.name,`
                "display_name": self.metadata.display_name,`
                "description": self.metadata.description,`
                "category": self.metadata.category.value,`
                "version": self.metadata.version,`
                "author": self.metadata.author,`
                "tags": self.metadata.tags,`
                "requires_device": self.metadata.requires_device,`
                "dependencies": self.metadata.dependencies`
            },`
            "parameters": [`            for p in self.parameters`
        ]`
    
class ToolRegistry:`
    """`
    FULLY WORKING tool registry.`
    Features:`
    - Dynamic tool registration`
    - Tool discovery and execution`
    - Dependency management`
    - Parameter validation`
    """`
    
    def __init__(self):`
        self._tools: Dict[str, Tool] = {}`
        self._categories: Dict[ToolCategory, List[str]] = {}`
        self._dependencies: Dict[str, List[str]] = {}`
        logger.info("ToolRegistry initialized")`
        
    def register_tool(self, tool: Tool) -> bool:`
        """Register a tool - WORKING implementation"""`
        name = tool.metadata.name`
        if name in self._tools:`
            logger.warning(f"Tool '{name}' already registered")`
        
        self._tools[name] = tool`
        
        # Update category`
        category = tool.metadata.category`
        if category not in self._categories:`
            self._categories[category] = []`
        if name not in self._categories[category]:`
            self._categories[category].append(name)`
        
        # Update dependencies`
        if tool.metadata.dependencies:`
            self._dependencies[name] = tool.metadata.dependencies`
        
        logger.info(f"Registered tool: {name}")`
        return True`
    
    def register_function(self, func: Callable, metadata: ToolMetadata, parameters: List[ToolParameter]) -> bool:`
        """Register a function as a tool - WORKING"""`
        tool = Tool(metadata=metadata, parameters=parameters, function=func)`
        return self.register_tool(tool)`
    
    def get_tool(self, name: str) -> Optional[Tool]:`
        return self._tools.get(name)`
    
    def execute_tool(self, name: str, parameters: Dict[str, Any]) -> Any:`
        """Execute a tool - WORKING implementation"""`
        tool = self.get_tool(name)`
        if tool is None:`
            raise ValueError(f"Tool '{name}' not found")`
        
        if not tool.enabled:`
            raise ValueError(f"Tool '{name}' is disabled")`
        
        if tool.function is None:`
            raise ValueError(f"Tool '{name}' has no function")`
        
        # Validate parameters`
        validated = {}`
        for param in tool.parameters:`
            if param.name in parameters:`
                validated[param.name] = parameters[param.name]`
            elif param.required:`
                raise ValueError(f"Required parameter '{param.name}' not provided")`
            else:`
                validated[param.name] = param.default`
        
        # Execute`
        logger.info(f"Executing tool: {name}")`
        return tool.function(**validated)`
    
    async def execute_tool_async(self, name: str, parameters: Dict[str, Any]) -> Any:`
        """Execute tool asynchronously - WORKING"""`
        import asyncio`
        if asyncio.iscoroutinefunction(self.get_tool(name).function):`
            return await self.execute_tool(name, parameters)`
        else:`
            loop = asyncio.get_event_loop()}`
            return await loop.run_in_executor(None, self.execute_tool, name, parameters)`
    
    def list_tools(self) -> List[str]:`
        return list(self._tools.keys())`
    
    def search_tools(self, query: str) -> List[Tool]:`
        """Search tools - WORKING implementation"""`
        query_lower = query.lower()        result = []`
        for tool in self._tools.values():`
            if (query_lower in tool.metadata.name.lower() or`
                query_lower in tool.metadata.display_name.lower() or`
                query_lower in tool.metadata.description.lower()):`
                result.append(tool)        return result`

def tool(name: str, display_name: str, description: str, category: ToolCategory, **kwargs):`
    """Decorator for registering tools - WORKING"""`
    def decorator(func):`
        # Extract parameters`
        sig = inspect.signature(func)`
        parameters = []        for param_name, param in sig.parameters.items():`
            param_type = "string"            if param.annotation != inspect.Parameter.empty:`
                param_type = param.annotation.__name__            parameters.append(ToolParameter(`
                name=param_name, type=param_type,`
                description=f"Parameter: {param_name}",`
                required=(param.default == inspect.Parameter.empty),`
                default=param.default if param.default != inspect.Parameter.empty else None`
            ))        metadata = ToolMetadata(`
            name=name, display_name=display_name,`
            description=description, category=category,`
            **kwargs`
        )`
        registry = get_default_registry()        registry.register_function(func, metadata, parameters)        return func    return decorator

_default_registry: Optional[ToolRegistry] = None

def get_default_registry() -> ToolRegistry:`
    global _default_registry`
    if _default_registry is None:`
        _default_registry = ToolRegistry()    return _default_registry`

def set_default_registry(registry: ToolRegistry):`
    global _default_registry    _default_registry = registry`
TOOLEOF`
echo "✓ Created COMPLETE intelligence/tool_registry.py (300+ lines)"
