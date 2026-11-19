"""Runtime contracts for task-local steps and model tools."""

# === SECTION 1: Imports ===
from dataclasses import dataclass
from functools import partial
from typing import Callable


# === SECTION 2: Task contracts ===
@dataclass(frozen=True)
class TaskContext:
    """Expose task inputs, prior state, and runtime services to a callable."""

    task: str
    inputs: dict
    state: dict
    services: object


@dataclass(frozen=True)
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


class ToolDecorator:
    """Attach tool metadata without wrapping its callable."""

    @staticmethod
    def run(function, *, description, parameters):
        """Return the callable with metadata attached."""
        function.task_tool = ToolSpec(function, description, parameters)
        return function

    def __init__(self):
        """Initialize the stateless decorator factory."""


tool = partial(partial, ToolDecorator.run)


@dataclass(frozen=True)
class ToolResult:
    """Return a model-visible payload and separate checkpoint state updates."""

    payload: dict
    updates: dict
