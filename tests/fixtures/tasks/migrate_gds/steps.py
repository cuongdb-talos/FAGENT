"""Representative layout step implementations."""

# === SECTION 1: Fixture steps ===
class LayoutSteps:
    """Return state updates for a minimal layout workflow."""

    @staticmethod
    def run(context):
        """Return the input path as a state update."""
        return {"source": context.inputs["input_gds"]}

    def __init__(self):
        """Initialize the stateless fixture."""

    @staticmethod
    def validate(context):
        """Return whether a prior step supplied a layout path."""
        return {"validated": bool(context.state["source"])}


read_gds = LayoutSteps.run
validate_gds = LayoutSteps.validate
