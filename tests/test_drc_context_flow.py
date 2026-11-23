# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from langgraph.checkpoint.memory import MemorySaver

from FAGENT.engine.conversation_trace import ConversationTrace
from FAGENT.engine.graph_builder import GraphBuilder
from FAGENT.engine.graph_runner import GraphRunner
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import TaskContext
from FAGENT.engine.workflow_loader import WorkflowLoader
from FAGENT.tasks.drc.steps import Steps

# === SECTION 2: Test Suite Class ===


class TestDrcContextFlow:
    """Test suite verifying DRC context flow, graph routing, and trace correlation."""

    def test_read_node_assembles_context_and_publishes_it(self):
        report = {'rules': [{'name': 'RULE_A', 'reported_count': 1, 'total_count': 1, 'geometry': []}, {'name': 'RULE_CLEAN', 'reported_count': 0, 'total_count': 0, 'geometry': []}], 'total_violations': 1}
        layout = SimpleNamespace(read_drc=Mock(return_value=report), gds_data={'source': 'layout.gds'})
        knowledge = [{'rule': 'RULE_A', 'content': 'rule data'}]
        services = GraphServices.__new__(GraphServices)
        services.layout = layout
        services.rag = SimpleNamespace(retrieve=Mock(return_value=knowledge))
        services.config = {'goal': 'repair', 'rag_limit': 4}
        state = {'messages': [{'role': 'system', 'content': 'instructions'}]}
        updates = Steps().read_drc(TaskContext('drc', {}, state, services))
        context = updates['drc_context']
        assert context['knowledge']['references'] == knowledge
        assert context['knowledge']['constraints'][0] in context['goal']
        assert updates['report'] == report
        sent = json.loads(updates['messages'][-1]['content'])['drc_context']
        assert sent['drc']['rules'] == report['rules'][:1]
        assert sent['drc']['omitted_clean_rule_count'] == 1
        assert 'gds' not in sent and sent['knowledge']['references'] == knowledge

    def test_workflow_routes_context_through_plan_to_agent(self):
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()
        assert [(step.id, step.action, step.next) for step in workflow.steps] == [
            ('read', 'steps:read_drc', 'plan'), ('plan', 'plan', 'repair'), ('repair', 'agent', 'END')]
        assert {'read_gds', 'replace_polygon', 'read_checklist', 'update_checklist'} <= set(workflow.steps[-1].tools)

    def test_graph_passes_read_context_through_plan_and_agent(self, tmp_path):
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()
        report = {'rules': [{'name': 'RULE_A', 'reported_count': 1, 'total_count': 1, 'geometry': [{'id': 7, 'points_dbu': [[0, 0], [1, 1]]}]}]}
        services = GraphServices.__new__(GraphServices)
        services.layout = SimpleNamespace(read_drc=Mock(return_value=report), gds_data={'source': 'layout.gds'})
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[{'rule': 'RULE_A', 'content': 'reference'}]))
        services.config = {'goal': 'repair', 'rag_limit': 4}
        services.rundir = tmp_path
        services.workflow = workflow
        services.max_iterations = 4
        services.check_output_path = Mock()
        services.trace = None
        plan = {'summary': 'inspect', 'steps': [{'tool': 'get_violation', 'purpose': 'inspect', 'task_id': 'task-1'}]}
        final = {'decision': 'stop', 'status': 'incomplete', 'reason': 'needs review', 'violations_analysis': [], 'recommendations': 'inspect'}
        services.chat = Mock(side_effect=[{'role': 'assistant', 'content': json.dumps(plan)}, {'role': 'assistant', 'content': json.dumps(final)}])
        graph = GraphBuilder(services, MemorySaver()).run()
        state = graph.invoke({'messages': [{'role': 'system', 'content': 'instructions'}], 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0}, {'configurable': {'thread_id': 'test'}})
        assert state['report'] == report
        assert state['drc_context']['drc']['rules'] == report['rules']
        assert state['checklist']['tasks'][0]['rule'] == 'RULE_A'
        plan_messages = services.chat.call_args_list[0].args[0]
        agent_messages = services.chat.call_args_list[1].args[0]
        assert json.loads(plan_messages[1]['content'])['active_violations'][0]['rule'] == 'RULE_A'
        assert json.loads(agent_messages[-1]['content'])['current_checklist'] == state['checklist']
        assert json.loads(agent_messages[-1]['content'])['goal'] == state['drc_context']['goal']
        assert services.rag.retrieve.call_count == 1
        assert json.loads((tmp_path / 'checklist.json').read_text())['tasks'] == state['checklist']['tasks']

    def test_runner_reports_agent_result_without_review(self, tmp_path):
        services = SimpleNamespace(check_output_path=Mock(), input_sources=None)
        response = {'status': 'verified', 'reason': 'model assessment'}
        result = GraphRunner(services, tmp_path).finish({'response': response, 'messages': [], 'inputs': {}})
        assert result['response'] == response
        assert result['status'] == 'incomplete'
        assert 'validation' in result['reason'].lower()

    def test_integration_trace_plan_read_gds_repair(self, tmp_path):
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()
        report = {'rules': [{'name': 'CT_R_11', 'reported_count': 1, 'total_count': 1, 'geometry': [{'id': 1, 'points_dbu': [[0, 0], [10, 10]]}]}], 'total_violations': 1}

        tool_read_gds_spec = Mock()
        tool_read_gds_spec.model_spec.return_value = {'name': 'read_gds', 'description': 'Read GDS'}
        tool_read_gds_spec.function = Mock(return_value={'cell': 'ROM_MCELL0', 'polygons': 3})
        tool_read_gds_spec.parameters = {'type': 'object'}

        for step in workflow.steps:
            if step.id == 'repair':
                step.tools['read_gds'] = tool_read_gds_spec

        config = {'goal': 'Fix DRC errors', 'rag_limit': 2}
        services = GraphServices(tmp_path, config, workflow)
        services.layout = SimpleNamespace(read_drc=Mock(return_value=report), gds_data={'source': 'layout.gds'}, check_output_path=Mock())
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[{'id': 'rec-1', 'score': 0.9}]))

        mock_client = Mock()
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
        mock_client.chat.side_effect = [resp_plan, resp_call_tool, resp_final]
        services.client = mock_client

        graph = GraphBuilder(services, MemorySaver()).run()
        graph.invoke(
            {'messages': [{'role': 'system', 'content': 'instructions'}], 'inputs': {'gds': 'g', 'drc': 'd', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'trace_test'}},
        )

        trace_file = tmp_path / 'conversation.json'
        assert trace_file.is_file()
        data = json.loads(trace_file.read_text(encoding='utf-8'))
        assert data.get('version') == 2
        events = data.get('events', [])

        # Verify DRC and RAG provenance events exist
        drc_events = [e for e in events if e.get('name') == 'drc_read']
        assert len(drc_events) == 2
        assert drc_events[0]['type'] == 'operation_start'
        assert drc_events[1]['type'] == 'operation_finish'
        assert drc_events[1]['status'] == 'completed'
        assert drc_events[1]['outputs']['report'] == report

        rag_events = [e for e in events if e.get('name') == 'rag_retrieve']
        assert len(rag_events) == 2
        assert rag_events[0]['type'] == 'operation_start'
        assert rag_events[1]['type'] == 'operation_finish'
        assert rag_events[1]['status'] == 'completed'
        assert rag_events[1]['outputs']['records'] == [{'id': 'rec-1', 'score': 0.9}]

        # Verify ordering: drc_read finish before rag_retrieve finish, before model_request
        drc_finish_idx = events.index(drc_events[1])
        rag_finish_idx = events.index(rag_events[1])
        first_model_req_idx = next(i for i, e in enumerate(events) if e.get('type') == 'model_request')
        assert drc_finish_idx < rag_finish_idx < first_model_req_idx

        # Model and tool events sequence preserved
        model_and_tool = [e for e in events if e.get('type') in ('model_request', 'model_response', 'tool_result')]
        assert [e['type'] for e in model_and_tool] == [
            'model_request',
            'model_response',
            'model_request',
            'model_response',
            'tool_result',
            'model_request',
            'model_response',
        ]

        # Verify readable trace.log
        log = (tmp_path / 'trace.log').read_text()
        assert 'read_drc' in log or 'drc_read' in log or 'READ DRC' in log
        assert 'rag_retrieve' in log or 'RAG RETRIEVE' in log

    def test_drc_read_and_rag_empty_and_error_paths(self, tmp_path):
        """Verify drc_read and rag_retrieve handle empty outputs and record error paths."""
        workflow = SimpleNamespace(
            task='drc',
            version=1,
            digest='wf_digest',
            steps=[],
            layout_services=True,
            response_schema={'type': 'object'},
            prompt_text=['system prompt'],
            protocol_text='',
            reference_label='',
            knowledge_review=False,
            inputs=[],
        )
        services = GraphServices(tmp_path, {'goal': 'test', 'rag_limit': 5}, workflow)

        # 1. Empty DRC report and empty RAG results
        empty_report = {'rules': [], 'total_violations': 0}
        services.layout = SimpleNamespace(read_drc=Mock(return_value=empty_report), gds_data={'source': 'l.gds'}, check_output_path=Mock())
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[]))
        ctx = services.build_drc_context()
        assert ctx['drc']['rules'] == []

        trace_data = json.loads((tmp_path / 'conversation.json').read_text())
        drc_finishes = [e for e in trace_data['events'] if e.get('name') == 'drc_read' and e.get('type') == 'operation_finish']
        assert len(drc_finishes) == 1
        assert drc_finishes[0]['status'] == 'completed'
        assert drc_finishes[0]['outputs']['report'] == empty_report

        rag_finishes = [e for e in trace_data['events'] if e.get('name') == 'rag_retrieve' and e.get('type') == 'operation_finish']
        assert len(rag_finishes) == 1
        assert rag_finishes[0]['status'] == 'completed'
        assert rag_finishes[0]['outputs']['records'] == []

        # 2. DRC read failure records failed operation
        services.layout = SimpleNamespace(read_drc=Mock(side_effect=IOError('DRC results corrupted')), check_output_path=Mock())
        with pytest.raises(IOError, match='DRC results corrupted'):
            services.build_drc_context()

        trace_data2 = json.loads((tmp_path / 'conversation.json').read_text())
        failed_drc = [e for e in trace_data2['events'] if e.get('name') == 'drc_read' and e.get('type') == 'operation_finish'][-1]
        assert failed_drc['status'] == 'failed'
        assert 'DRC results corrupted' in str(failed_drc['error'])

        # 3. RAG retrieval failure records failed operation
        services.layout = SimpleNamespace(read_drc=Mock(return_value={'rules': [{'name': 'R1'}]}), check_output_path=Mock())
        services.rag = SimpleNamespace(retrieve=Mock(side_effect=RuntimeError('Embedding service offline')))
        with pytest.raises(RuntimeError, match='Embedding service offline'):
            services.build_drc_context()

        trace_data3 = json.loads((tmp_path / 'conversation.json').read_text())
        failed_rag = [e for e in trace_data3['events'] if e.get('name') == 'rag_retrieve' and e.get('type') == 'operation_finish'][-1]
        assert failed_rag['status'] == 'failed'
        assert 'Embedding service offline' in str(failed_rag['error'])
