# NationClaw Plugin SDK

This document defines the proposed plugin architecture for NationClaw. The goal of the Plugin SDK is to let developers extend NationClaw without modifying the core agent runtime.

## Component tree

```text
Plugin SDK
│
├── Tool Plugin
├── AI Plugin
├── UI Plugin
├── Storage Plugin
├── Device Plugin
├── Network Plugin
└── Workflow Plugin
```

## Goals

The Plugin SDK should provide:

- a stable extension interface for third-party developers;
- explicit plugin permissions and capability declarations;
- safe runtime loading and validation;
- a unified manifest format;
- compatibility with NationClaw's existing agent, device, file, chat, and model interfaces;
- clear boundaries between plugins and core runtime internals.

## Runtime architecture

```text
NationClaw Core Runtime
│
├── Plugin Manager
│   ├── Manifest Loader
│   ├── Plugin Registry
│   ├── Permission Validator
│   ├── Lifecycle Manager
│   └── Sandbox / Policy Gate
│
├── Plugin SDK
│   ├── Tool Plugin API
│   ├── AI Plugin API
│   ├── UI Plugin API
│   ├── Storage Plugin API
│   ├── Device Plugin API
│   ├── Network Plugin API
│   └── Workflow Plugin API
│
└── Core Interfaces
    ├── Agent API
    ├── File Interface
    ├── Device Interface
    ├── FM / Model Interface
    ├── Chat Interface
    └── Observability / Audit Interface
```

## Plugin types

### 1. Tool Plugin

Purpose: expose callable tools to the agent's reasoning loop.

Examples:

- calculator;
- calendar lookup;
- document parser;
- app-specific helper action;
- custom business API connector;
- data transformation utility.

Expected capabilities:

- define tool metadata;
- define input schema;
- define output schema;
- validate parameters;
- execute synchronously or asynchronously;
- return structured results.

Suggested Python interface:

```python
class ToolPlugin:
    name: str
    description: str
    input_schema: dict
    output_schema: dict

    def run(self, params: dict, context: dict) -> dict:
        ...
```

### 2. AI Plugin

Purpose: extend or replace AI/model behavior.

Examples:

- custom model provider;
- custom prompt strategy;
- embedding provider;
- reranker;
- summarizer;
- domain-specific planner;
- GUI grounding model adapter.

Expected capabilities:

- register a model backend;
- expose supported modalities;
- provide generation APIs;
- provide embedding APIs where supported;
- optionally provide streaming responses;
- expose provider-specific configuration.

Suggested Python interface:

```python
class AIPlugin:
    name: str
    provider: str
    modalities: list[str]

    def generate(self, messages: list[dict], options: dict | None = None) -> dict:
        ...

    def embed(self, texts: list[str], options: dict | None = None) -> list[list[float]]:
        ...
```

### 3. UI Plugin

Purpose: add user-facing interfaces, dashboards, panels, or interaction surfaces.

Examples:

- local web dashboard;
- task monitor;
- mobile overlay extension;
- approval dialog;
- debugging view;
- chat-style local UI.

Expected capabilities:

- render UI components;
- expose actions to the agent;
- receive runtime events;
- request user approval for sensitive actions;
- show logs, screenshots, and task progress.

Suggested Python interface:

```python
class UIPlugin:
    name: str

    def mount(self, context: dict) -> None:
        ...

    def handle_event(self, event: dict) -> None:
        ...
```

### 4. Storage Plugin

Purpose: customize memory, file storage, vector storage, and artifact persistence.

Examples:

- S3-compatible storage;
- Google Drive adapter;
- SQLite memory store;
- vector database backend;
- encrypted local memory;
- organization-level knowledge base.

Expected capabilities:

- read and write files or records;
- search content;
- optionally provide semantic search;
- handle access control;
- encrypt/decrypt sensitive data when configured;
- provide migration/export hooks.

Suggested Python interface:

```python
class StoragePlugin:
    name: str

    def read(self, path: str) -> bytes:
        ...

    def write(self, path: str, content: bytes) -> None:
        ...

    def search(self, query: str, options: dict | None = None) -> list[dict]:
        ...
```

### 5. Device Plugin

Purpose: add new device types or device-control backends.

Examples:

- Android backend;
- iOS backend;
- browser automation backend;
- desktop automation backend;
- remote VM backend;
- cloud phone provider;
- smart home device adapter.

Expected capabilities:

- list devices;
- connect/disconnect;
- capture screen or state;
- execute atomic actions;
- return action traces;
- expose supported actions and coordinate systems.

Suggested Python interface:

```python
class DevicePlugin:
    name: str
    device_type: str

    def list_devices(self) -> list[dict]:
        ...

    def connect(self, device_id: str) -> object:
        ...

    def screenshot(self, device: object) -> bytes:
        ...

    def execute_action(self, device: object, action: dict) -> dict:
        ...
```

### 6. Network Plugin

Purpose: add networked services and communication integrations.

Examples:

- REST API connector;
- GraphQL connector;
- webhook receiver;
- WebSocket integration;
- proxy provider;
- chat platform integration;
- enterprise API gateway.

Expected capabilities:

- send requests;
- receive events;
- normalize responses;
- manage authentication safely;
- expose rate-limit metadata;
- support retries and timeouts.

Suggested Python interface:

```python
class NetworkPlugin:
    name: str

    def request(self, method: str, url: str, **kwargs) -> dict:
        ...

    def subscribe(self, topic: str, handler) -> None:
        ...
```

### 7. Workflow Plugin

Purpose: package reusable multi-step procedures.

Examples:

- daily report workflow;
- app onboarding workflow;
- data extraction workflow;
- customer support workflow;
- scheduled reminder workflow;
- app testing workflow.

Expected capabilities:

- define workflow metadata;
- define required inputs;
- define steps;
- call tools/devices/models;
- persist progress;
- resume interrupted workflows;
- expose final output artifacts.

Suggested Python interface:

```python
class WorkflowPlugin:
    name: str
    description: str

    def start(self, inputs: dict, context: dict) -> dict:
        ...

    def resume(self, workflow_id: str, context: dict) -> dict:
        ...

    def cancel(self, workflow_id: str) -> None:
        ...
```

## Plugin manifest

Each plugin should include a manifest file:

```yaml
name: example_tool
version: 0.1.0
type: tool
entrypoint: example_tool.plugin:Plugin
description: Example NationClaw tool plugin
author: Example Developer
license: MIT

permissions:
  - file.read
  - network.request

capabilities:
  - tool.execute

config_schema:
  type: object
  properties:
    api_key:
      type: string
      secret: true
```

Recommended manifest file names:

```text
nationclaw-plugin.yaml
plugin.yaml
```

## Plugin directory layout

```text
example_plugin/
├── nationclaw-plugin.yaml
├── README.md
├── example_plugin/
│   ├── __init__.py
│   └── plugin.py
└── tests/
    └── test_plugin.py
```

## Plugin lifecycle

```text
Discover → Validate → Load → Configure → Enable → Run → Disable → Unload
```

Lifecycle hooks:

```python
class PluginBase:
    def on_load(self, context: dict) -> None:
        ...

    def on_enable(self) -> None:
        ...

    def on_disable(self) -> None:
        ...

    def on_unload(self) -> None:
        ...
```

## Permission model

Plugins must declare permissions before accessing sensitive resources.

| Permission | Description |
| --- | --- |
| `agent.read` | Read basic agent context |
| `agent.execute` | Request agent task execution |
| `file.read` | Read files from the agent workspace |
| `file.write` | Write files to the agent workspace |
| `device.list` | List configured devices |
| `device.control` | Execute actions on devices |
| `model.generate` | Use configured language/vision models |
| `model.embed` | Use embedding models |
| `chat.send` | Send chat messages |
| `network.request` | Make outbound network requests |
| `storage.external` | Use external storage providers |
| `ui.render` | Render user-facing UI |
| `workflow.run` | Start or resume workflows |

## Security requirements

- Plugins must be disabled by default unless installed by the user or organization admin.
- Plugins must declare all requested permissions in the manifest.
- Secrets must not be stored in plugin source files.
- Plugins should not receive raw credentials unless absolutely necessary.
- Network plugins must support timeouts and rate-limit handling.
- Device plugins must be auditable when performing UI actions.
- Workflow plugins must persist resumable state safely.
- High-risk actions should support user approval policies.
- Plugin load failures must not crash the main NationClaw runtime.

## Python package integration

Plugins can be discovered from:

1. local plugin directories;
2. Python entry points;
3. organization-managed plugin registries.

Suggested Python entry point group:

```toml
[project.entry-points."nationclaw.plugins"]
example_tool = "example_tool.plugin:Plugin"
```

Legacy `setup.py` equivalent:

```python
entry_points={
    "nationclaw.plugins": [
        "example_tool=example_tool.plugin:Plugin",
    ],
}
```

## Registry integration

Tool plugins should integrate with the existing `nationclaw.intelligence.tool_registry` module.

Example:

```python
from nationclaw.intelligence.tool_registry import (
    Tool,
    ToolCategory,
    ToolMetadata,
    ToolParameter,
    get_default_registry,
)


def add(a: int, b: int) -> int:
    return a + b


registry = get_default_registry()
registry.register_function(
    add,
    ToolMetadata(
        name="add",
        display_name="Add",
        description="Add two integers",
        category=ToolCategory.CUSTOM,
    ),
    [
        ToolParameter(name="a", type="int", description="First integer"),
        ToolParameter(name="b", type="int", description="Second integer"),
    ],
)
```

## Suggested implementation order

1. Plugin manifest schema.
2. Base plugin interfaces.
3. Plugin manager and local plugin discovery.
4. Tool Plugin integration with `ToolRegistry`.
5. AI Plugin adapter for model providers.
6. Storage Plugin adapter for memory backends.
7. Device Plugin adapter for new device controllers.
8. Network Plugin adapter for external services.
9. Workflow Plugin runner.
10. UI Plugin hooks and approval surfaces.

## Minimal MVP

The minimum useful Plugin SDK should support:

- loading local plugins from a configured directory;
- parsing `nationclaw-plugin.yaml`;
- validating plugin type and permissions;
- registering Tool Plugins into `ToolRegistry`;
- disabling plugins safely when loading fails.

This provides immediate extensibility while keeping the first implementation small and safe.
