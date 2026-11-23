# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from FAGENT.engine.conversation_trace import ConversationTrace
from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.graph_runner import GraphRunner
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import TaskContext

# === SECTION 2: Test Suite ===


class TestConversationSnapshots:
    """Test suite for conversation trace, tool capturing, and artifact protection."""

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

    def test_tool_result_is_recorded_in_trace(self, tmp_path):
        workflow = SimpleNamespace(response_schema=None)
        config = {'goal': 'repair'}
        services = GraphServices(tmp_path, config, workflow)
        call = {'id': 'call-101', 'function': {'name': 'inspect', 'arguments': '{"target": "cellA"}'}}
        message = {'role': 'assistant', 'tool_calls': [call]}

        spec = SimpleNamespace(parameters={'type': 'object'}, function=Mock(return_value={'status': 'inspected'}))
        context = TaskContext('task', {}, {'messages': [message]}, services)
        updates = GraphNodes(services).execute_tools(context, SimpleNamespace(tools={'inspect': spec}, id='agent'))

        assert updates['messages'][-1] == {
            'role': 'tool',
            'tool_call_id': 'call-101',
            'content': json.dumps({'status': 'inspected'}),
        }
        trace_file = tmp_path / 'conversation.json'
        assert trace_file.is_file()
        data = json.loads(trace_file.read_text(encoding='utf-8'))
        assert data.get('version') == 2
        events = data.get('events', [])
        assert len(events) == 1
        assert events[0] == {
            'type': 'tool_result',
            'message': {
                'role': 'tool',
                'tool_call_id': 'call-101',
                'content': json.dumps({'status': 'inspected'}),
            },
        }

    def test_tool_exception_records_error_returned_to_model(self, tmp_path):
        workflow = SimpleNamespace(response_schema=None)
        config = {'goal': 'repair'}
        services = GraphServices(tmp_path, config, workflow)
        call = {'id': 'call-102', 'function': {'name': 'fail_tool', 'arguments': '{}'}}
        message = {'role': 'assistant', 'tool_calls': [call]}

        spec = SimpleNamespace(parameters={'type': 'object'}, function=Mock(side_effect=ValueError('Invalid polygon ID')))
        context = TaskContext('task', {}, {'messages': [message]}, services)
        updates = GraphNodes(services).execute_tools(context, SimpleNamespace(tools={'fail_tool': spec}, id='agent'))

        assert 'Invalid polygon ID' in updates['messages'][-1]['content']
        trace_file = tmp_path / 'conversation.json'
        data = json.loads(trace_file.read_text(encoding='utf-8'))
        assert data.get('version') == 2
        events = data.get('events', [])
        assert len(events) == 1
        assert events[0]['type'] == 'tool_result'
        assert events[0]['message']['tool_call_id'] == 'call-102'
        assert events[0]['message']['content'] == updates['messages'][-1]['content']
        assert 'Invalid polygon ID' in events[0]['message']['content']

    def test_invalid_agent_response_preserves_trace_and_reports_failure(self, tmp_path):
        workflow = SimpleNamespace(task='task', inputs=[], response_schema={'type': 'object', 'required': ['verdict']})
        config = {'goal': 'repair'}
        services = GraphServices(tmp_path, config, workflow)
        mock_client = Mock()
        mock_client.chat.return_value = {'role': 'assistant', 'content': 'not valid json'}
        services.client = mock_client

        state = {'messages': [{'role': 'user', 'content': 'start repair'}]}
        step = SimpleNamespace(id='agent', action='agent', tools={}, schema={'type': 'object', 'required': ['verdict']})
        updates = GraphNodes(services).run_task_step(state, step)

        assert updates['failed'] is True
        trace_file = tmp_path / 'conversation.json'
        data = json.loads(trace_file.read_text(encoding='utf-8'))
        assert data.get('version') == 2
        events = data.get('events', [])
        assert len(events) == 2
        assert events[0]['type'] == 'model_request'
        assert events[1]['type'] == 'model_response'
        assert events[1]['message']['content'] == 'not valid json'

    def test_finish_does_not_overwrite_conversation_trace(self, tmp_path):
        workflow = SimpleNamespace(response_schema={'type': 'object'})
        config = {'goal': 'repair'}
        services = GraphServices(tmp_path, config, workflow)
        trace_file = tmp_path / 'conversation.json'
        services.conversation_trace.append({'type': 'tool_result', 'message': {'role': 'tool', 'tool_call_id': 'c1', 'content': '{}'}})

        runner = GraphRunner(services, tmp_path)
        dummy_state = {'messages': [{'role': 'user', 'content': 'legacy plain message'}], 'inputs': {}, 'response': {'status': 'done'}}
        runner.finish(dummy_state)

        data = json.loads(trace_file.read_text(encoding='utf-8'))
        assert data.get('version') == 2
        assert len(data.get('events', [])) == 1
        assert data['events'][0]['type'] == 'tool_result'
