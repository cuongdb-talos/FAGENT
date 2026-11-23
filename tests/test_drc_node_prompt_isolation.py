# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from langgraph.checkpoint.memory import MemorySaver

from FAGENT.engine.graph_builder import GraphBuilder
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.workflow_loader import WorkflowLoader


# === SECTION 2: Helper Classes ===
class MockChatClient:
    """Module-level mock chat client to avoid nested defs and record requests cleanly."""

    def run(self):
        return self

    def __init__(self, canned_responses):
        self.canned_responses = list(canned_responses)
        self.captured_requests = []

    def chat(self, messages, tools=None, response_schema=None):
        self.captured_requests.append([dict(m) for m in messages])
        if not self.canned_responses:
            raise RuntimeError('No more canned responses in mock chat client')
        return self.canned_responses.pop(0)


# === SECTION 3: Test Suite Class ===
class TestDrcNodePromptIsolation:
    """Test suite verifying DRC node prompt isolation, system message scoping, and non-DRC compatibility."""

    def test_drc_node_prompt_isolation_plan_and_repair_requests(self, tmp_path: Path):
        """Integration test using real DRC workflow to verify strict isolation of plan and repair prompts."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        constraints = ['Preserve connectivity', 'Minimize width changes']
        constraint_file = tmp_path / 'constraints.json'
        constraint_file.write_text(json.dumps(constraints))

        report = {
            'rules': [{'name': 'CT_R_11', 'reported_count': 1, 'total_count': 1, 'geometry': [{'id': 1, 'points_dbu': [[0, 0], [10, 10]]}]}],
            'total_violations': 1,
        }

        tool_read_gds_spec = Mock()
        tool_read_gds_spec.model_spec.return_value = {'name': 'read_gds', 'description': 'Read GDS'}
        tool_read_gds_spec.function = Mock(return_value={'cell': 'ROM_MCELL0', 'polygons': 3})
        tool_read_gds_spec.parameters = {'type': 'object'}

        for step in workflow.steps:
            if step.id == 'repair':
                step.tools['read_gds'] = tool_read_gds_spec

        goal = 'Fix DRC errors for ROM cell'
        config = {
            'goal': goal,
            'rag_limit': 2,
            'constraints_file': str(constraint_file),
        }
        services = GraphServices(tmp_path, config, workflow)
        services.layout = SimpleNamespace(
            read_drc=Mock(return_value=report),
            gds_data={'source': 'layout.gds'},
            check_output_path=Mock(),
        )
        services.rag = SimpleNamespace(
            retrieve=Mock(return_value=[{'id': 'rec-1', 'rule': 'CT_R_11', 'content': 'Keep contact spacing >= 10'}])
        )

        plan_obj = {'summary': 'Fix CT_R_11', 'steps': [{'tool': 'read_gds', 'purpose': 'inspect', 'task_id': 'task-1'}]}
        resp_plan = {'role': 'assistant', 'content': json.dumps(plan_obj)}
        resp_call_tool = {
            'role': 'assistant',
            'content': None,
            'tool_calls': [{'id': 'call-gds-001', 'type': 'function', 'function': {'name': 'read_gds', 'arguments': '{"cell": "ROM_MCELL0"}'}}],
        }
        resp_final = {
            'role': 'assistant',
            'content': json.dumps({'decision': 'stop', 'status': 'incomplete', 'reason': 'done', 'violations_analysis': [], 'recommendations': 'none'}),
        }

        mock_client = MockChatClient([resp_plan, resp_call_tool, resp_final])
        services.client = mock_client

        graph = GraphBuilder(services, MemorySaver()).run()
        graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'isolation_test'}},
        )

        captured_requests = mock_client.captured_requests
        assert len(captured_requests) == 3, f'Expected 3 model requests, got {len(captured_requests)}'

        plan_req = captured_requests[0]
        repair_req_1 = captured_requests[1]
        repair_req_2 = captured_requests[2]

        # 1. System message assertions across all DRC requests
        for req_idx, req in enumerate([plan_req, repair_req_1, repair_req_2]):
            sys_msg = req[0]
            assert sys_msg['role'] == 'system', f'Request {req_idx} first message must be system'
            assert goal in sys_msg['content']
            assert 'Preserve connectivity' in sys_msg['content']
            assert 'Minimize width changes' in sys_msg['content']
            # System content must NOT contain role prompts or protocol
            assert 'planning agent' not in sys_msg['content'].lower()
            assert 'repair agent' not in sys_msg['content'].lower()
            assert 'Return exactly one JSON object' not in sys_msg['content']

        # 2. Plan request assertions
        plan_all_content = ' '.join(str(m.get('content', '')) for m in plan_req)
        assert 'planning agent' in plan_all_content.lower() or 'plan_instruction' in plan_all_content
        assert 'repair agent' not in plan_all_content.lower()
        assert 'replace_polygon' not in plan_all_content
        # Preserves report and RAG references
        assert 'CT_R_11' in plan_all_content
        assert 'Keep contact spacing' in plan_all_content

        # 3. Repair request 1 assertions
        repair_1_all_content = ' '.join(str(m.get('content', '')) for m in repair_req_1)
        assert 'planning agent' not in repair_1_all_content.lower()
        assert 'plan_instruction' not in repair_1_all_content
        assert 'repair agent' in repair_1_all_content.lower()
        assert 'Return exactly one JSON object' in repair_1_all_content
        # Preserves plan response and checklist
        assert 'Fix CT_R_11' in repair_1_all_content
        assert 'current_checklist' in repair_1_all_content

        # 4. Repair request 2 assertions (tool loop)
        repair_2_all_content = ' '.join(str(m.get('content', '')) for m in repair_req_2)
        assert 'planning agent' not in repair_2_all_content.lower()
        assert 'plan_instruction' not in repair_2_all_content
        assert 'repair agent' in repair_2_all_content.lower()
        # Preserves tool call and result correlation
        tool_call_msgs = [m for m in repair_req_2 if m.get('tool_calls')]
        tool_res_msgs = [m for m in repair_req_2 if m.get('role') == 'tool']
        assert len(tool_call_msgs) == 1
        assert len(tool_res_msgs) == 1
        assert tool_call_msgs[0]['tool_calls'][0]['id'] == 'call-gds-001'
        assert tool_res_msgs[0]['tool_call_id'] == 'call-gds-001'

    def test_empty_constraints_system_message(self, tmp_path: Path):
        """Verify that empty constraints yield system content reflecting only the goal without fabricated text."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        constraint_file = tmp_path / 'empty_constraints.json'
        constraint_file.write_text('[]')

        report = {'rules': [{'name': 'R1', 'reported_count': 1, 'total_count': 1, 'geometry': [{'id': 1, 'points_dbu': [[0, 0], [1, 1]]}]}]}
        config = {'goal': 'Clean violations', 'rag_limit': 1, 'constraints_file': str(constraint_file)}

        services = GraphServices(tmp_path, config, workflow)
        services.layout = SimpleNamespace(read_drc=Mock(return_value=report), gds_data={'source': 'l.gds'}, check_output_path=Mock())
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[]))

        resp_plan = {'role': 'assistant', 'content': json.dumps({'summary': 's', 'steps': [{'tool': 'read_gds', 'purpose': 'w', 'task_id': 'task-1'}]})}
        resp_final = {'role': 'assistant', 'content': json.dumps({'decision': 'stop', 'status': 'incomplete', 'reason': 'r', 'violations_analysis': [], 'recommendations': ''})}
        mock_client = MockChatClient([resp_plan, resp_final])
        services.client = mock_client

        graph = GraphBuilder(services, MemorySaver()).run()
        graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'empty_test'}},
        )

        captured_requests = mock_client.captured_requests
        assert len(captured_requests) >= 1
        sys_msg = captured_requests[0][0]
        assert sys_msg['role'] == 'system'
        assert sys_msg['content'].strip() == 'Clean violations'
        assert 'Constraints:' not in sys_msg['content']
        assert 'planning agent' not in sys_msg['content'].lower()
        assert 'repair agent' not in sys_msg['content'].lower()

    def test_custom_repair_prompt_isolated_to_repair_node(self, tmp_path: Path):
        """Verify user-configured custom repair prompt is delivered to repair node only, not plan or system."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        custom_prompt_text = 'CUSTOM SITE DIRECTIVE: DO NOT TOUCH METAL1'
        prompt_file = tmp_path / 'custom_prompt.md'
        prompt_file.write_text(custom_prompt_text)

        report = {'rules': [{'name': 'R1', 'reported_count': 1, 'total_count': 1, 'geometry': [{'id': 1, 'points_dbu': [[0, 0], [1, 1]]}]}]}
        config = {
            'goal': 'Fix DRC',
            'system_prompt_file': str(prompt_file),
            'rag_limit': 1,
        }

        services = GraphServices(tmp_path, config, workflow)
        services.layout = SimpleNamespace(read_drc=Mock(return_value=report), gds_data={'source': 'l.gds'}, check_output_path=Mock())
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[]))

        resp_plan = {'role': 'assistant', 'content': json.dumps({'summary': 's', 'steps': [{'tool': 'read_gds', 'purpose': 'w', 'task_id': 'task-1'}]})}
        resp_final = {'role': 'assistant', 'content': json.dumps({'decision': 'stop', 'status': 'incomplete', 'reason': 'r', 'violations_analysis': [], 'recommendations': ''})}
        mock_client = MockChatClient([resp_plan, resp_final])
        services.client = mock_client

        graph = GraphBuilder(services, MemorySaver()).run()
        graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'custom_prompt_test'}},
        )

        captured_requests = mock_client.captured_requests
        assert len(captured_requests) == 2
        plan_req = captured_requests[0]
        repair_req = captured_requests[1]

        # System message must not contain custom repair prompt
        assert custom_prompt_text not in plan_req[0]['content']
        assert custom_prompt_text not in repair_req[0]['content']

        # Plan request messages must not contain custom repair prompt
        plan_text = ' '.join(str(m.get('content', '')) for m in plan_req)
        assert custom_prompt_text not in plan_text

        # Repair request messages must contain custom repair prompt
        repair_text = ' '.join(str(m.get('content', '')) for m in repair_req)
        assert custom_prompt_text in repair_text

    def test_resume_history_preserves_isolation(self, tmp_path: Path):
        """Verify resume/history preserves tool call correlation without leaking plan instructions."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        report = {'rules': [{'name': 'R1', 'reported_count': 1, 'total_count': 1, 'geometry': [{'id': 1, 'points_dbu': [[0, 0], [1, 1]]}]}]}
        config = {'goal': 'Fix DRC', 'rag_limit': 1}

        services = GraphServices(tmp_path, config, workflow)
        services.layout = SimpleNamespace(read_drc=Mock(return_value=report), gds_data={'source': 'l.gds'}, check_output_path=Mock())
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[]))

        plan_obj = {'summary': 's', 'steps': [{'tool': 'read_gds', 'purpose': 'w', 'task_id': 'task-1'}]}
        resp_plan = {'role': 'assistant', 'content': json.dumps(plan_obj)}
        resp_final = {'role': 'assistant', 'content': json.dumps({'decision': 'stop', 'status': 'incomplete', 'reason': 'r', 'violations_analysis': [], 'recommendations': ''})}

        mock_client = MockChatClient([resp_plan, resp_final])
        services.client = mock_client

        saver = MemorySaver()
        graph = GraphBuilder(services, saver).run()
        thread_config = {'configurable': {'thread_id': 'resume_test'}}
        state = graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0},
            thread_config,
        )

        captured_requests = mock_client.captured_requests
        assert len(captured_requests) == 2
        repair_req = captured_requests[1]
        repair_text = ' '.join(str(m.get('content', '')) for m in repair_req)
        assert 'planning agent' not in repair_text.lower()
        assert 'plan_instruction' not in repair_text

    def test_dynamic_prompt_resolution_by_node_name(self, tmp_path: Path):
        """Verify prompt is dynamically resolved by node name without hard-coding task in Graph."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        # Step 'repair' gets prompt matching its name 'repair'
        repair_step = next(s for s in workflow.steps if s.id == 'repair')
        plan_step = next(s for s in workflow.steps if s.id == 'plan')

        assert repair_step.prompt is not None
        assert 'repair agent' in repair_step.prompt.lower()
        assert plan_step.prompt is not None
        assert 'planning agent' in plan_step.prompt.lower()

    def test_non_drc_workflow_preserves_prompt_behavior(self, tmp_path: Path):
        """Verify non-DRC workflows preserve standard system prompt composition with prompts and protocol."""
        workflow = SimpleNamespace(
            task='sample_task',
            version=1,
            digest='sample_digest',
            steps=[],
            layout_services=False,
            response_schema={'type': 'object'},
            prompt_text=['You are a sample task agent.'],
            protocol_text='Sample protocol: return JSON.',
            reference_label='References:',
            knowledge_review=False,
            inputs=[],
        )
        config = {'system_prompt': 'Custom generic instruction'}
        services = GraphServices(tmp_path, config, workflow)
        services.prepare()

        assert len(services.messages) == 1
        sys_msg = services.messages[0]
        assert sys_msg['role'] == 'system'
        assert 'You are a sample task agent.' in sys_msg['content']
        assert 'Custom generic instruction' in sys_msg['content']
        assert 'Sample protocol: return JSON.' in sys_msg['content']
