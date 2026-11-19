"""Verify the same graph can run a newly defined task."""

# === SECTION 1: Imports ===
from io import StringIO
from pathlib import Path
from shutil import copytree
from types import SimpleNamespace

import pytest

from FAGENT.engine.graph_builder import GraphBuilder
from FAGENT.engine.task_contracts import ToolSpec
from FAGENT.engine.trace import Trace
from FAGENT.engine.workflow_loader import WorkflowLoader


# === SECTION 2: Generic workflow tests ===
class TestTaskExecution:
    """Run an unregistered fixture without requesting a model."""

    def run(self, tmp_path, name="migrate_gds", inputs=None, fail=False):
        """Load and run a task containing two normal steps."""
        fixture = Path(__file__).parent / "fixtures/tasks/migrate_gds"
        directory = copytree(fixture, tmp_path / name)
        path = directory / "workflow.yaml"
        path.write_text(
            f"task: {name}\nversion: 1\ninputs: [input_gds]\nsteps:\n"
            "  - {id: read, action: 'steps:read_gds', next: validate}\n"
            "  - {id: validate, action: 'steps:validate_gds', next: END}\n"
        )
        if fail:
            steps = directory / "steps.py"
            steps.write_text(steps.read_text().replace('bool(context.state["source"])', '1 / 0'))
        workflow = WorkflowLoader(path, tmp_path).run()
        graph = GraphBuilder(SimpleNamespace(workflow=workflow), None).run()
        return graph.invoke({"inputs": {"input_gds": "cell.gds"} if inputs is None else inputs})

    def test_fixture_task_runs_in_declared_order(self, tmp_path):
        """Finish the read to validate route."""
        assert list(self.run(tmp_path)["task_state"]) == ["source", "validated"]

    def test_step_state_updates_reach_next_step(self, tmp_path):
        """Expose earlier step results through the task context."""
        assert self.run(tmp_path)["task_state"]["validated"] is True

    def test_new_task_needs_no_engine_registration(self, tmp_path):
        """Run a task whose name is unknown to the engine."""
        assert self.run(tmp_path, "new_layout_task")["task_state"]["source"] == "cell.gds"

    def test_failed_step_never_claims_success(self, tmp_path):
        """Stop at a failed step and include its identity."""
        result = self.run(tmp_path, fail=True)["result"]
        assert result["status"] == "incomplete"
        assert "migrate_gds/validate" in result["reason"]

    def test_unknown_task_and_missing_input_report_context(self, tmp_path):
        """Identify an unknown task and a missing required input."""
        with pytest.raises(ValueError, match="unknown_task"):
            WorkflowLoader(tmp_path / "unknown_task/workflow.yaml", tmp_path).run()
        assert "input_gds" in self.run(tmp_path, inputs={})["result"]["reason"]

    def test_agent_calls_task_tool_and_receives_result(self):
        """Keep the tool ID and return the actual callable result."""
        from_context = TestAgentLoop().run({"layer": 2})
        assert from_context["response"] == {"layer": 2}
        assert from_context["messages"][-2]["tool_call_id"] == "inspect-1"

    @pytest.mark.parametrize("arguments", [{"layer": "bad"}, {"layer": 1, "extra": True}])
    def test_tool_arguments_validated_before_invocation(self, arguments):
        """Return an error for invalid arguments."""
        loop = TestAgentLoop()
        assert "error" in loop.run(arguments)["response"]
        assert not loop.invocations

    def test_undeclared_tool_is_rejected(self):
        """Restrict tools to the current agent step."""
        assert "error" in TestAgentLoop().run({"layer": 2}, name="unknown")["response"]

    def test_tool_failure_is_returned_to_agent(self):
        """Let the agent receive an error from a failing callable."""
        assert "error" in TestAgentLoop().run({"layer": -1})["response"]

    def test_iteration_limit_is_incomplete(self):
        """End the agent cycle once the configured limit is reached."""
        assert TestAgentLoop().run({"layer": 2}, limit=1)["result"]["status"] == "incomplete"

    def test_multiple_agent_steps_use_their_own_tools(self):
        """Provide only the declared tool list for each step."""
        loop = TestAgentLoop()
        loop.run({"layer": 2}, second=True)
        assert loop.seen_specs == [["inspect"], ["inspect"], []]

    def test_loop_and_multiple_tools_emit_each_visit(self):
        """Trace repeated agent visits and each callable dispatch."""
        loop = TestAgentLoop()
        loop.run({"layer": 2}, second=True)
        trace = loop.trace.stream.getvalue()
        assert trace.count("[Step agent] Requesting a response from the model") == 2
        assert trace.index("Next node: agent--tools.") < trace.index("[Step agent] Calling tool inspect.")
        assert trace.index("[Step agent] Calling tool inspect.") < trace.index("Next node: agent.")



class TestAgentLoop:
    """Model adapter and tool for a synthetic workflow."""

    def run(self, arguments, name="inspect", limit=3, second=False):
        """Invoke agent, tool, and optional second agent through LangGraph."""
        self.arguments = arguments
        self.name = name
        self.seen_specs = []
        self.invocations = []
        self.trace = Trace(True, StringIO())
        tool = ToolSpec(self.inspect, "Inspect a layer", {
            "type": "object", "properties": {"layer": {"type": "integer"}},
            "required": ["layer"], "additionalProperties": False,
        })
        steps = [SimpleNamespace(id="agent", action="agent", function=None, tools={"inspect": tool},
                                 next="second" if second else "END")]
        if second:
            steps.append(SimpleNamespace(id="second", action="agent", function=None, tools={}, next="END"))
        workflow = SimpleNamespace(task="fixture", inputs=[], steps=steps, response_schema={"type": "object"})
        services = SimpleNamespace(workflow=workflow, max_iterations=limit, chat=self.chat, trace=self.trace)
        return GraphBuilder(services, None).run().invoke({"messages": []})

    def chat(self, messages, specs):
        """Request one call and echo the returned tool payload on the next turn."""
        self.seen_specs.append([spec["function"]["name"] for spec in specs])
        if len(self.seen_specs) == 1:
            return {"role": "assistant", "tool_calls": [{
                "id": "inspect-1", "function": {"name": self.name, "arguments": self.arguments},
            }]}
        return {"role": "assistant", "content": messages[-1].get("content", "{}")}

    def inspect(self, context, layer):
        """Return the layer or raise on invalid domain input."""
        self.invocations.append(layer)
        if layer < 0:
            raise ValueError("Negative layer")
        return {"layer": layer}
