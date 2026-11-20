"""Runtime contracts for task-local steps and model tools."""

# === SECTION 1: Imports ===
from dataclasses import dataclass
from typing import Callable


# === SECTION 2: Task contracts ===
class TaskContext:
    """Expose task inputs, prior state, and runtime services to a callable."""

    task: str
    inputs: dict
    state: dict
    services: object


TaskContext = dataclass(TaskContext, frozen=True)


class ToolSpec:
    """Bind a model-visible schema to its task callable."""

    function: Callable
    description: str
    parameters: dict

    def model_spec(self, name):
        """Return the adapter's function-calling definition."""
        return {"type": "function", "function": {
            "name": name, "description": self.description, "parameters": self.parameters,
        }}


ToolSpec = dataclass(ToolSpec, frozen=True)


class ToolResult:
    """Return a model-visible payload and separate checkpoint state updates."""

    payload: dict
    updates: dict


ToolResult = dataclass(ToolResult, frozen=True)
