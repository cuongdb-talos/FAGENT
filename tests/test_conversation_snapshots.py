# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import TaskContext
from FAGENT.engine.conversation_trace import ConversationTrace

# === SECTION 2: Test Suite ===


class TestConversationSnapshots:
    """Test suite for conversation trace and snapshot persistence."""

    def test_agent_saves_response_before_returning(self, tmp_path):
        message = {'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'function': {'name': 'inspect', 'arguments': '{}'}}]}
        services = SimpleNamespace(rundir=tmp_path, max_iterations=2, chat=Mock(return_value=message), check_output_path=Mock())
        context = TaskContext('task', {}, {'messages': [{'role': 'user', 'content': 'inspect'}]}, services)
        updates = GraphNodes(services).execute_agent(context, SimpleNamespace(tools={}, schema=None))
        assert json.loads((tmp_path / 'conversation.json').read_text()) == updates['messages']

    def test_tool_result_is_saved_before_next_response(self, tmp_path):
        message = {'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'function': {'name': 'inspect', 'arguments': '{}'}}]}
        services = SimpleNamespace(rundir=tmp_path, check_output_path=Mock())
        spec = SimpleNamespace(parameters={'type': 'object'}, function=Mock(return_value={'status': 'ok'}))
        context = TaskContext('task', {}, {'messages': [message]}, services)
        updates = GraphNodes(services).execute_tools(context, SimpleNamespace(tools={'inspect': spec}, id='agent'))
        assert json.loads((tmp_path / 'conversation.json').read_text()) == updates['messages']

    def test_invalid_agent_response_is_retained_in_failed_state(self, tmp_path):
        message = {'role': 'assistant', 'content': '{invalid json'}
        workflow = SimpleNamespace(task='task', inputs=[])
        services = SimpleNamespace(rundir=tmp_path, max_iterations=2, workflow=workflow, chat=Mock(return_value=message), check_output_path=Mock())
        state = {'messages': [{'role': 'user', 'content': 'inspect'}]}
        step = SimpleNamespace(id='agent', action='agent', tools={}, schema=None)
        updates = GraphNodes(services).run_task_step(state, step)
        assert updates['failed'] is True
        assert updates['messages'][-1] == message
        assert json.loads((tmp_path / 'conversation.json').read_text()) == updates['messages']

    def test_chat_records_requests_and_responses_in_order(self, tmp_path):
        workflow = SimpleNamespace(response_schema={'type': 'object', 'title': 'default'})
        config = {'goal': 'test_goal'}
        services = GraphServices(tmp_path, config, workflow)
        mock_client = Mock()
        resp1 = {'role': 'assistant', 'content': json.dumps({'step': 1})}
        resp2 = {'role': 'assistant', 'content': json.dumps({'step': 2})}
        mock_client.chat.side_effect = [resp1, resp2]
        services.client = mock_client

        msg1 = [{'role': 'user', 'content': json.dumps({'current_checklist': {'item': 1}})}]
        specs1 = [{'name': 'tool1'}]
        msg2 = [{'role': 'user', 'content': json.dumps({'current_checklist': {'item': 2}})}]
        specs2 = [{'name': 'tool2'}]
        custom_schema = {'type': 'string', 'title': 'custom'}

        out1 = services.chat(msg1, specs1)
        out2 = services.chat(msg2, specs2, response_schema=custom_schema)

        assert out1 == resp1
        assert out2 == resp2
        assert mock_client.chat.call_count == 2
        mock_client.chat.assert_any_call(msg1, specs1, workflow.response_schema)
        mock_client.chat.assert_any_call(msg2, specs2, custom_schema)

        trace_file = tmp_path / 'conversation.json'
        assert trace_file.is_file()
        data = json.loads(trace_file.read_text(encoding='utf-8'))
        assert data.get('version') == 2
        events = data.get('events', [])
        assert len(events) == 4

        assert events[0] == {
            'type': 'model_request',
            'request_id': '1',
            'messages': msg1,
            'tools': specs1,
            'response_schema': workflow.response_schema,
        }
        assert events[1] == {
            'type': 'model_response',
            'request_id': '1',
            'message': resp1,
        }
        assert events[2] == {
            'type': 'model_request',
            'request_id': '2',
            'messages': msg2,
            'tools': specs2,
            'response_schema': custom_schema,
        }
        assert events[3] == {
            'type': 'model_response',
            'request_id': '2',
            'message': resp2,
        }

    def test_chat_records_model_error_when_client_raises(self, tmp_path):
        workflow = SimpleNamespace(response_schema={'type': 'object'})
        config = {'goal': 'test_goal'}
        services = GraphServices(tmp_path, config, workflow)
        mock_client = Mock()
        mock_client.chat.side_effect = RuntimeError('Model inference exploded')
        services.client = mock_client

        msg = [{'role': 'user', 'content': 'hello'}]
        specs = [{'name': 'tool1'}]

        with pytest.raises(RuntimeError, match='Model inference exploded'):
            services.chat(msg, specs)

        trace_file = tmp_path / 'conversation.json'
        assert trace_file.is_file()
        data = json.loads(trace_file.read_text(encoding='utf-8'))
        assert data.get('version') == 2
        events = data.get('events', [])
        assert len(events) == 2

        assert events[0]['type'] == 'model_request'
        assert events[0]['request_id'] == '1'
        assert events[0]['messages'] == msg
        assert events[0]['tools'] == specs

        assert events[1]['type'] == 'model_error'
        assert events[1]['request_id'] == '1'
        assert events[1]['error_type'] == 'RuntimeError'
        assert 'Model inference exploded' in events[1]['error_message']

    def test_trace_resume_appends_without_loss_or_duplicate_ids(self, tmp_path):
        trace_file = tmp_path / 'conversation.json'
        initial_events = [
            {'type': 'model_request', 'request_id': '1', 'messages': [{'role': 'user', 'content': 'step 1'}], 'tools': [], 'response_schema': {}},
            {'type': 'model_response', 'request_id': '1', 'message': {'role': 'assistant', 'content': 'ok 1'}},
        ]
        trace_file.write_text(json.dumps({'version': 2, 'events': initial_events}, indent=2) + '\n', encoding='utf-8')

        workflow = SimpleNamespace(response_schema={'type': 'object'})
        config = {'goal': 'test_goal'}
        services = GraphServices(tmp_path, config, workflow)
        mock_client = Mock()
        resp = {'role': 'assistant', 'content': 'ok 2'}
        mock_client.chat.return_value = resp
        services.client = mock_client

        msg2 = [{'role': 'user', 'content': 'step 2'}]
        services.chat(msg2, [])

        data = json.loads(trace_file.read_text(encoding='utf-8'))
        assert data.get('version') == 2
        events = data.get('events', [])
        assert len(events) == 4
        assert events[0] == initial_events[0]
        assert events[1] == initial_events[1]
        assert events[2]['type'] == 'model_request'
        assert events[2]['request_id'] == '2'
        assert events[3]['type'] == 'model_response'
        assert events[3]['request_id'] == '2'
