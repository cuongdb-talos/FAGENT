"""Verify live trace content, ordering, and quiet mode."""

# === SECTION 1: Imports ===
from io import StringIO
from types import SimpleNamespace

from FAGENT.engine.graph_builder import GraphBuilder
from FAGENT.engine.trace import Trace


# === SECTION 2: Trace tests ===
class TestTrace:
    """Exercise trace output through graph routing."""

    def run(self, enabled=True, failure=False):
        """Execute one step and return the trace stream."""
        from_trace = Trace(enabled, StringIO())
        step = SimpleNamespace(id="read", action="steps:read", next="END", function=self.read)
        workflow = SimpleNamespace(task="fixture", inputs=["required"] if failure else [], steps=[step])
        services = SimpleNamespace(workflow=workflow, trace=from_trace)
        GraphBuilder(services, None).run().invoke({"inputs": {"data": "secret-sentinel"}})
        return from_trace.stream.getvalue()

    def read(self, context):
        """Return the input so the test can detect accidental logging."""
        return {"payload": context.inputs["data"]}

    def test_verbose_event_order_and_actual_next(self):
        """Print the selected destination after the step completes."""
        assert self.run().splitlines() == [
            "[Step read] Running workflow action steps:read (node: read)",
            "[Step read] Completed (node: read). Workflow finished.",
        ]

    def test_no_trace_without_flag(self):
        """Keep the trace quiet by default."""
        assert self.run(enabled=False) == ""

    def test_waiting_and_failed_status(self, capsys):
        """Report failures regardless of trace mode."""
        assert "[Step read] Failed" in self.run(failure=True)
        self.run(enabled=False, failure=True)
        assert "[ERROR] fixture/read" in capsys.readouterr().err

    def test_trace_excludes_payloads(self):
        """Keep input and output payloads out of the trace."""
        assert "secret-sentinel" not in self.run()

