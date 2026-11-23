# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from langgraph.checkpoint.memory import MemorySaver

from FAGENT.engine.graph_builder import GraphBuilder
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult, ToolSpec
from FAGENT.engine.workflow_loader import WorkflowLoader
from FAGENT.tasks.drc.checklist import Checklist


# === SECTION 2: Mock Chat Client ===
class StubChatClient:
    """Stub chat client for recording requests and returning canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.captured_requests = []

    def run(self):
        return self

    def chat(self, messages, tools=None, response_schema=None):
        self.captured_requests.append([dict(m) for m in messages])
        if not self.responses:
            raise RuntimeError('No more responses in StubChatClient')
        return self.responses.pop(0)


# === SECTION 3: Test Suite Class ===
class TestDrcReplanning:
    """Test suite verifying DRC plan state, replan routing, and candidate persistence."""

    def test_plan_receives_active_violations_and_rag_without_drc_context(self, tmp_path: Path):
        """Assert plan receives active violations and RAG but no drc_context; repair receives plan and checklist."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        report = {
            'top_cell': 'ROM_MCELL0',
            'dbu_per_user_unit': 1000,
            'source': 'test.drc.results',
            'rules': [
                {
                    'name': 'CT_R_11',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 101, 'type': 'p', 'points_dbu': [[0, 0], [10, 10]]}],
                }
            ],
            'total_violations': 1,
        }

        services = GraphServices(tmp_path, {'goal': 'Repair CT errors', 'rag_limit': 2}, workflow)
        services.layout = SimpleNamespace(
            read_drc=Mock(return_value=report),
            gds_data={'source': 'layout.gds'},
            check_output_path=Mock(),
        )
        services.rag = SimpleNamespace(
            retrieve=Mock(return_value=[{'rule': 'CT_R_11', 'content': 'Enclosure rule text'}])
        )

        plan_content = {
            'summary': 'Repair contact enclosure',
            'steps': [{'tool': 'get_violation', 'purpose': 'Inspect marker', 'task_id': 'task-1'}],
        }
        resp_plan = {'role': 'assistant', 'content': json.dumps(plan_content)}
        resp_repair = {
            'role': 'assistant',
            'content': json.dumps({
                'decision': 'stop',
                'status': 'incomplete',
                'reason': 'Inspection done',
                'violations_analysis': [],
                'recommendations': 'none',
            }),
        }

        mock_client = StubChatClient([resp_plan, resp_repair])
        services.client = mock_client

        graph = GraphBuilder(services, MemorySaver()).run()
        state = graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'test_plan_isolation'}},
        )

        assert len(mock_client.captured_requests) == 2
        plan_req = mock_client.captured_requests[0]
        repair_req = mock_client.captured_requests[1]

        # Verify plan request does NOT contain 'drc_context' wholesale payload
        plan_str = json.dumps(plan_req)
        assert 'drc_context' not in plan_str
        assert 'CT_R_11' in plan_str
        assert 'Enclosure rule text' in plan_str

        # Verify repair request receives plan and current_checklist
        repair_user_msgs = [json.loads(m['content']) for m in repair_req if m.get('role') == 'user' and m.get('content')]
        repair_payload = next(p for p in repair_user_msgs if 'current_checklist' in p)
        assert 'plan' in repair_payload
        assert repair_payload['plan']['summary'] == 'Repair contact enclosure'
        assert repair_payload['current_checklist']['tasks'][0]['task_id'] == 'task-1'

    def test_empty_plan_with_active_tasks_yields_incomplete(self, tmp_path: Path):
        """Assert that an empty plan returned by the model while active tasks exist terminates with incomplete."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        report = {
            'top_cell': 'ROM_MCELL0',
            'dbu_per_user_unit': 1000,
            'source': 'test.drc.results',
            'rules': [
                {
                    'name': 'CT_R_11',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 101, 'type': 'p', 'points_dbu': [[0, 0], [10, 10]]}],
                }
            ],
            'total_violations': 1,
        }

        services = GraphServices(tmp_path, {'goal': 'Fix all', 'rag_limit': 1}, workflow)
        services.layout = SimpleNamespace(
            read_drc=Mock(return_value=report),
            gds_data={'source': 'layout.gds'},
            check_output_path=Mock(),
        )
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[]))

        empty_plan = {'summary': 'Empty plan', 'steps': []}
        services.client = StubChatClient([{'role': 'assistant', 'content': json.dumps(empty_plan)}])

        graph = GraphBuilder(services, MemorySaver()).run()
        state = graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'test_empty_plan'}},
        )

        assert state.get('failed') is True
        assert state.get('result', {}).get('status') == 'incomplete'
        assert state.get('checklist') is not None
        assert len(state['checklist']['tasks']) == 1

    def test_no_active_tasks_does_not_produce_fabricated_plan(self, tmp_path: Path):
        """Assert that a clean report with zero violations does not produce a fabricated plan."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        report = {
            'top_cell': 'ROM_MCELL0',
            'dbu_per_user_unit': 1000,
            'source': 'test.drc.results',
            'rules': [{'name': 'CLEAN_RULE', 'reported_count': 0, 'total_count': 0, 'geometry': []}],
            'total_violations': 0,
        }

        services = GraphServices(tmp_path, {'goal': 'Clean', 'rag_limit': 1}, workflow)
        services.layout = SimpleNamespace(
            read_drc=Mock(return_value=report),
            gds_data={'source': 'layout.gds'},
            check_output_path=Mock(),
        )
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[]))

        # Model should NOT even need to be called for planning if there are no active tasks,
        # or if called, receives empty tasks.
        resp_repair = {
            'role': 'assistant',
            'content': json.dumps({
                'decision': 'stop',
                'status': 'verified',
                'reason': 'Layout clean',
                'violations_analysis': [],
                'recommendations': 'none',
            }),
        }
        mock_client = StubChatClient([resp_repair])
        services.client = mock_client

        graph = GraphBuilder(services, MemorySaver()).run()
        state = graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'test_clean_report'}},
        )

        assert state.get('plan') is not None
        assert state['plan']['steps'] == []

    def test_tool_failure_routes_to_plan_with_candidate_digest_and_failure_conversation(self, tmp_path: Path):
        """Assert tool failure after saved edit routes back to plan with failure conversation and candidate digest."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        report = {
            'top_cell': 'ROM_MCELL0',
            'dbu_per_user_unit': 1000,
            'source': 'test.drc.results',
            'rules': [
                {
                    'name': 'CT_R_11',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 101, 'type': 'p', 'points_dbu': [[0, 0], [10, 10]]}],
                }
            ],
            'total_violations': 1,
        }

        services = GraphServices(tmp_path, {'goal': 'Repair CT', 'rag_limit': 1}, workflow)
        services.max_iterations = 3
        services.layout = SimpleNamespace(
            read_drc=Mock(return_value=report),
            gds_data={'source': 'layout.gds'},
            check_output_path=Mock(),
            read_gds=Mock(return_value={'cell': 'ROM_MCELL0', 'polygons': 2}),
            replace_polygon=Mock(return_value={'status': 'modified'}),
        )
        services.checkpoint_candidate = Mock(return_value=tmp_path / 'candidate.gds')
        (tmp_path / 'candidate.gds').write_text('fake_gds_content')
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[{'rule': 'CT_R_11', 'content': 'RAG text'}]))

        # Define a tool that fails deliberately
        failing_tool_call = Mock(side_effect=RuntimeError('Geometry kernel exploded'))
        for step in workflow.steps:
            if step.id == 'repair':
                step.tools['failing_tool'] = ToolSpec(
                    function=failing_tool_call,
                    description='A tool that always fails',
                    parameters={'type': 'object', 'properties': {'task_id': {'type': 'string'}}, 'required': ['task_id']},
                )

        # 1. First plan: propose calling failing_tool
        plan_1 = {'summary': 'Try repair', 'steps': [{'tool': 'failing_tool', 'purpose': 'Fix', 'task_id': 'task-1'}]}
        # 2. Repair calls replace_polygon first to establish candidate, then failing_tool
        repair_call_1 = {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {'id': 'call-1', 'type': 'function', 'function': {'name': 'read_gds', 'arguments': '{"cell_name": "ROM_MCELL0"}'}},
            ],
        }
        repair_call_2 = {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {
                    'id': 'call-2',
                    'type': 'function',
                    'function': {
                        'name': 'replace_polygon',
                        'arguments': '{"cell_name": "ROM_MCELL0", "polygon_id": "ROM_MCELL0:1", "points": [[0,0],[10,0],[10,10],[0,10]]}',
                    },
                }
            ],
        }
        repair_call_3 = {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {'id': 'call-3', 'type': 'function', 'function': {'name': 'failing_tool', 'arguments': '{"task_id": "task-1"}'}},
            ],
        }
        # 3. Second plan: receives failure info and current candidate digest, proposes finish
        plan_2 = {'summary': 'Replan after failure', 'steps': [{'tool': 'read_checklist', 'purpose': 'Check', 'task_id': 'task-1'}]}
        repair_final = {
            'role': 'assistant',
            'content': json.dumps({
                'decision': 'stop',
                'status': 'incomplete',
                'reason': 'Stopped after replanning',
                'violations_analysis': [],
                'recommendations': 'none',
            }),
        }

        mock_client = StubChatClient([
            {'role': 'assistant', 'content': json.dumps(plan_1)},
            repair_call_1,
            repair_call_2,
            repair_call_3,
            {'role': 'assistant', 'content': json.dumps(plan_2)},
            repair_final,
        ])
        services.client = mock_client

        graph = GraphBuilder(services, MemorySaver()).run()
        state = graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'test_replanning_flow'}},
        )

        assert state.get('candidate_digest') is not None
        assert state.get('replan_count') == 1

        # Check the 4th request made to the model (which is the second plan request)
        plan_2_req = mock_client.captured_requests[4]
        plan_2_content = ' '.join(str(m.get('content', '')) for m in plan_2_req)
        assert 'Geometry kernel exploded' in plan_2_content
        assert state['candidate_digest'] in plan_2_content
        assert 'task-1' in plan_2_content
