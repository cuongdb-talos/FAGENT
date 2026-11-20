"""Optional read-only tool for the sample agent step."""

from FAGENT.engine.task_contracts import ToolSpec


def read_summary(context):
    """Return the checkpointed summary as a model-visible payload."""
    return {"summary": context.state["summary"]}


read_summary.task_tool = ToolSpec(
    function=read_summary,
    description="Read the normalized text from the preceding workflow step.",
    parameters={"type": "object", "properties": {}, "additionalProperties": False},
)
