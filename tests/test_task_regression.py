"""Test the production task definitions through the shared loader."""

# === SECTION 1: Imports ===
from hashlib import sha256
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
import pytest

from FAGENT.engine.graph_builder import GraphBuilder
from FAGENT.engine.graph_runner import GraphRunner
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import TaskContext
from FAGENT.engine.trace import Trace
from FAGENT.fagent import Fagent


# === SECTION 2: Production workflow tests ===
class TestProductionTasks:
    """Check routing and tool ownership at the CLI boundary."""

    @pytest.mark.parametrize("task", ["drc", "lvs"])
    def test_loads_production_task(self, task, tmp_path):
        """Resolve production steps from their own package."""
        workflow = Fagent().load_task_workflow(task, {}, tmp_path)
        assert workflow.steps[0].function is not None
        assert workflow.steps[-1].action == "review"
        assert workflow.steps[-1].continue_to == "repair"
        assert workflow.backend == "local"

    def test_drc_inspection_before_edit_and_candidate_digest(self, tmp_path):
        """Require the inspection and persist the actual candidate checksum."""
        candidate = tmp_path / "candidate.gds"
        layout = SimpleNamespace(read_gds=Mock(return_value={}),
                                 replace_polygon=Mock(return_value={"status": "modified"}))
        workflow = Fagent().load_task_workflow("drc", {}, tmp_path)
        tools = workflow.steps[1].tools
        services = SimpleNamespace(layout=layout, checkpoint_candidate=lambda: str(candidate))
        context = TaskContext("drc", {}, {}, services)
        arguments = {"cell_name": "test_cell", "polygon_index": 0, "points": [[0, 0], [0, 1], [1, 1]]}
        with pytest.raises(ValueError, match="Inspect"):
            tools["replace_polygon"].function(context, **arguments)
        layout.replace_polygon.assert_not_called()
        inspected = tools["read_gds"].function(context, "test_cell")
        candidate.write_bytes(b"fixture candidate")
        result = tools["replace_polygon"].function(
            TaskContext("drc", {}, inspected.updates, services), **arguments,
        )
        assert result.updates["candidate_digest"] == sha256(candidate.read_bytes()).hexdigest()

    def test_drc_validation_preserves_simulated_provenance(self, tmp_path):
        """Reject a pass without matching simulated evidence."""
        workflow = Fagent().load_task_workflow("drc", {}, tmp_path)
        validate = workflow.steps[2].function
        services = SimpleNamespace(workflow=workflow, validate_drc=Mock())
        candidate = tmp_path / "candidate.gds"
        candidate.write_bytes(b"candidate")
        context = TaskContext("drc", {}, {
            "candidate_path": str(candidate), "candidate_digest": sha256(candidate.read_bytes()).hexdigest(),
        }, services)
        services.validate_drc.return_value = {"status": "pass", "simulated": True, "candidate": str(candidate)}
        assert validate(context)["validation"]["simulated"] is True
        services.validate_drc.return_value = {"status": "pass"}
        assert validate(context)["validation"]["status"] == "error"
        services.validate_drc.return_value = {"status": "pass", "simulated": True, "candidate": str(candidate)}
        candidate.write_bytes(b"changed")
        assert validate(context)["validation"]["status"] == "error"

    def test_lvs_placeholder_and_mock_are_labeled(self, tmp_path):
        """Keep the placeholder and explicit mock provenance alongside review."""
        workflow = Fagent().load_task_workflow("lvs", {}, tmp_path)
        services = GraphServices(tmp_path, {}, workflow)
        graph = GraphBuilder(services, InMemorySaver()).run()
        config = {"configurable": {"thread_id": "lvs"}}
        state = graph.invoke({"inputs": {}}, config)
        assert state["response"]["repair"]["status"] == "unavailable"
        assert state["validation"] == {"status": "pass", "simulated": True, "backend": "mock"}
        stopped = graph.invoke(Command(resume={"feedback": None}), config)
        assert stopped["result"]["status"] == "incomplete"
        assert stopped["review"]["authorized"] is False

    @pytest.mark.parametrize(("decision", "target"), [("continue", "prepare_step"), ("unclear", "review_gate"), ("stop", None)])
    def test_review_continue_stop_unclear_and_consent(self, tmp_path, decision, target):
        """Resume review and exercise every configured route."""
        workflow = SimpleNamespace(task="fixture", inputs=[], knowledge_review=True, steps=[
            SimpleNamespace(id="prepare_step", action="steps:prepare", function=self.prepare, next="review_gate"),
            SimpleNamespace(id="review_gate", action="review", next="END", continue_to="prepare_step"),
        ])
        stream = StringIO()
        services = SimpleNamespace(workflow=workflow, interpret_feedback=Mock(return_value={"decision": decision}),
                                   trace=Trace(True, stream))
        graph = GraphBuilder(services, InMemorySaver()).run()
        config = {"configurable": {"thread_id": str(tmp_path)}}
        assert "__interrupt__" in graph.invoke({"inputs": {}}, config)
        state = graph.invoke(Command(resume={"feedback": "user feedback", "storage_consent": False}), config)
        assert state["review"]["authorized"] is False
        assert "[Step review_gate] Waiting for human feedback" in stream.getvalue()
        assert (f"Next node: {target}." if target else "Workflow finished.") in stream.getvalue()

    def prepare(self, context):
        """Provide one update to make a review retry observable."""
        return {"visits": context.state.get("visits", 0) + 1}

    def test_resume_rejects_changed_identity_or_candidate(self, tmp_path):
        """Validate both identities before accepting feedback."""
        workflow = SimpleNamespace(task="fixture", version=1, digest="original",
                                   steps=[SimpleNamespace(id="review_gate", action="review")])
        services = SimpleNamespace(workflow=workflow, check_output_path=Mock(), restore_candidate=Mock())
        runner = GraphRunner(services, tmp_path, checkpointer=InMemorySaver())
        candidate = tmp_path / "candidate.gds"
        candidate.write_bytes(b"changed")
        values = {"identity": runner.workflow_identity(), "candidate_path": str(candidate), "candidate_digest": "old"}
        runner.graph = SimpleNamespace(get_state=Mock(return_value=SimpleNamespace(
            next=("review_gate",), values=values, tasks=[SimpleNamespace(interrupts=[object()])],
        )))
        with pytest.raises(ValueError, match="candidate"):
            runner.resume("stop")
        workflow.digest = "changed"
        with pytest.raises(ValueError, match="identity"):
            runner.resume("stop")
        services.restore_candidate.assert_not_called()

    def test_current_drc_setup_per_cell(self, monkeypatch, tmp_path):
        """Keep the legacy list and setup paths at the preparation boundary."""
        workflow = Fagent().load_task_workflow("drc", {}, tmp_path)
        (tmp_path / "cells").write_text("one\n# comment\ntwo\n")
        (tmp_path / "deck").touch()
        loader = Mock()
        monkeypatch.setitem(workflow.prepare.__func__.__globals__, "InputLoader", loader)
        runs = list(workflow.prepare(
            {"drc_folder": "DRC", "drc_files": "deck"},
            {"list_cells": "cells", "layer_map_files": ["map"]}, tmp_path, tmp_path / "out", {}, False,
        ))
        assert [rundir.parent.name for rundir, _, _ in runs] == ["one", "two"]
        assert [Path(inputs["gds"]).name for _, _, inputs in runs] == ["one.gds", "two.gds"]
        assert loader.call_count == 2

    def test_fresh_runner_resumes_sqlite_review(self, tmp_path):
        """Reopen a persisted review with a separate runner."""
        workflow = Fagent().load_task_workflow("lvs", {}, tmp_path)
        runner = GraphRunner(GraphServices(tmp_path, {}, workflow), tmp_path)
        runner.prepare_graph()
        graph = GraphBuilder(runner.services, runner.checkpointer).run()
        config = {"configurable": {"thread_id": runner.thread_id}}
        state = graph.invoke({"identity": runner.workflow_identity()}, config)
        assert state["__interrupt__"]
        runner.connection.close()

        resumed = GraphRunner(GraphServices(tmp_path, {}, workflow), tmp_path,
                              thread_id=runner.thread_id)
        try:
            assert resumed.resume(None)["review"]["decision"] == "stop"
        finally:
            resumed.connection.close()

    def test_system_prompt_leaves_tools_to_the_active_step(self, tmp_path):
        """Keep a misleading global tool list out of the prompt."""
        workflow = Fagent().load_task_workflow("drc", {}, tmp_path)
        services = GraphServices(tmp_path, {}, workflow)
        assert not services.system_prompt([]).rstrip().endswith("[]")
