"""Representative model-visible layout tool."""

# === SECTION 1: Imports ===
from FAGENT.engine.task_contracts import tool


# === SECTION 2: Fixture tool ===
class LayoutTools:
    """Report the requested layer."""

    @staticmethod
    @tool(description="Inspect a layer", parameters={
        "type": "object", "properties": {"layer": {"type": "integer"}},
        "required": ["layer"], "additionalProperties": False,
    })
    def run(context, layer):
        """Return the layer number for the current task."""
        return {"layer": layer}

    def __init__(self):
        """Initialize the stateless fixture."""


inspect_layer = LayoutTools.run
