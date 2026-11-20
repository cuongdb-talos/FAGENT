"""Deterministic example step for a task-local workflow."""


class NormalizeStep:
    """Validate task input and produce a JSON-serializable summary."""

    def run(self, context):
        """Return the state updates that the graph will checkpoint."""
        text = context.inputs["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")

        # Keep the summary available to later steps and the final artifact.
        self.result = {
            "summary": " ".join(text.split()),
            "result": {"status": "complete"},
        }
        return self.result

    def __init__(self):
        self.result = None


# The workflow loader resolves this module-level callable.
normalize = NormalizeStep().run
