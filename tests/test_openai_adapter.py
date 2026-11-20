"""Tests for OpenAIAdapter and shared schema/response normalization."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
import json
import pytest

from FAGENT.integrations.models.llama_adapter import LlamaAdapter
from FAGENT.integrations.models.openai_adapter import OpenAIAdapter


# ==============================================================================
# SECTION 2: Module-Level Test Doubles and Fixtures
# ==============================================================================
class FakeCompletions:
    """Records create() calls and returns a preconfigured completion or raises."""

    def run(self):
        return self

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


class FakeChat:
    """Namespace mimicking openai.OpenAI().chat."""

    def run(self):
        return self

    def __init__(self, completions):
        self.completions = completions


class FakeOpenAIClient:
    """Fake OpenAI SDK client for unit tests."""

    def run(self):
        return self

    def __init__(self, response=None, error=None):
        self.completions = FakeCompletions(response=response, error=error)
        self.chat = FakeChat(self.completions)


class FakeModelDumpResponse:
    """Mimics an OpenAI SDK ChatCompletion object with model_dump()."""

    def run(self):
        return self

    def __init__(self, payload):
        self._payload = payload

    def model_dump(self):
        return self._payload


class FakeLlamaModel:
    """Fake llama.cpp model for LlamaAdapter regression tests."""

    def run(self):
        return self

    def __init__(self, response):
        self.response = response
        self.calls = []

    def create_chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def sample_config():
    return {
        'chat_backend': 'openai',
        'base_url': 'https://api.tokenlab.amd.com/v1',
        'agent_model_path': 'DeepSeek-V4-Flash',
        'api_key_env': 'TOKENLAB_API_KEY',
    }


def sample_tools():
    return [
        {
            'type': 'function',
            'function': {
                'name': 'read_drc',
                'description': 'Read DRC results',
                'parameters': {
                    'type': 'object',
                    'properties': {'rule': {'type': 'string'}},
                    'required': ['rule'],
                },
            },
        }
    ]


def sample_response_schema():
    return {
        'type': 'object',
        'properties': {
            'status': {'type': 'string'},
            'summary': {'type': 'string'},
        },
        'required': ['status', 'summary'],
        'additionalProperties': False,
    }


# ==============================================================================
# SECTION 3: OpenAIAdapter Contract and Error Tests
# ==============================================================================
def test_openai_adapter_final_json_and_request_payload():
    payload = {
        'choices': [
            {
                'finish_reason': 'stop',
                'message': {
                    'role': 'assistant',
                    'content': json.dumps({'status': 'done', 'summary': 'ok'}),
                    'reasoning_content': 'checked rules',
                },
            }
        ]
    }
    fake_client = FakeOpenAIClient(response=FakeModelDumpResponse(payload))
    adapter = OpenAIAdapter(sample_config(), client=fake_client)
    messages = [{'role': 'user', 'content': 'check drc'}]
    tools = sample_tools()
    final_schema = sample_response_schema()

    result = adapter.chat(messages, tools, final_schema)

    assert len(fake_client.completions.calls) == 1
    call_kwargs = fake_client.completions.calls[0]
    assert call_kwargs['model'] == 'DeepSeek-V4-Flash'
    assert call_kwargs['messages'] == messages
    expected_schema = LlamaAdapter({}).response_schema(tools, final_schema)
    assert call_kwargs['response_format'] == {'type': 'json_object', 'schema': expected_schema}
    assert result == {'role': 'assistant', 'content': '{"status": "done", "summary": "ok"}'}
    assert adapter.message == result
    assert adapter.last_thinking == 'checked rules'


def test_openai_adapter_tool_json_normalizes_to_tool_calls():
    payload = {
        'choices': [
            {
                'finish_reason': 'stop',
                'message': {
                    'role': 'assistant',
                    'content': json.dumps({'name': 'read_drc', 'arguments': {'rule': 'M1.W.1'}}),
                },
            }
        ]
    }
    fake_client = FakeOpenAIClient(response=FakeModelDumpResponse(payload))
    adapter = OpenAIAdapter(sample_config(), client=fake_client)

    result = adapter.chat([{'role': 'user', 'content': 'go'}], sample_tools(), sample_response_schema())

    assert result['role'] == 'assistant'
    assert result['content'] is None
    assert len(result['tool_calls']) == 1
    tool_call = result['tool_calls'][0]
    assert tool_call['id'].startswith('call_')
    assert tool_call['type'] == 'function'
    assert tool_call['function']['name'] == 'read_drc'
    assert json.loads(tool_call['function']['arguments']) == {'rule': 'M1.W.1'}


def test_openai_adapter_missing_or_empty_env_key_fails_before_request(monkeypatch):
    monkeypatch.delenv('TOKENLAB_API_KEY', raising=False)
    adapter = OpenAIAdapter(sample_config())
    with pytest.raises(ValueError, match='TOKENLAB_API_KEY'):
        adapter.chat([{'role': 'user', 'content': 'hi'}], sample_tools(), sample_response_schema())

    monkeypatch.setenv('TOKENLAB_API_KEY', '   ')
    adapter_empty = OpenAIAdapter(sample_config())
    with pytest.raises(ValueError, match='TOKENLAB_API_KEY'):
        adapter_empty.chat([{'role': 'user', 'content': 'hi'}], sample_tools(), sample_response_schema())


def test_openai_adapter_propagates_api_error_without_exposing_key(monkeypatch):
    secret = 'sk-super-secret-token-value'
    monkeypatch.setenv('TOKENLAB_API_KEY', secret)
    fake_client = FakeOpenAIClient(error=RuntimeError('401 Unauthorized'))
    adapter = OpenAIAdapter(sample_config(), client=fake_client)

    with pytest.raises(RuntimeError, match='401 Unauthorized') as exc_info:
        adapter.chat([{'role': 'user', 'content': 'hi'}], sample_tools(), sample_response_schema())

    assert secret not in str(exc_info.value)


@pytest.mark.parametrize(
    'choice',
    [
        {'finish_reason': 'length', 'message': {'role': 'assistant', 'content': '{"status": "done", "summary": "ok"}'}},
        {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{not-json'}},
        {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': '{"status": "done"}'}},
        {'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': None}},
        {'finish_reason': 'stop', 'message': {'role': 'user', 'content': '{"status": "done", "summary": "ok"}'}},
    ],
)
def test_openai_adapter_rejects_invalid_responses(choice):
    fake_client = FakeOpenAIClient(response=FakeModelDumpResponse({'choices': [choice]}))
    adapter = OpenAIAdapter(sample_config(), client=fake_client)

    with pytest.raises(ValueError):
        adapter.chat([{'role': 'user', 'content': 'hi'}], sample_tools(), sample_response_schema())


def test_llama_adapter_regression_preserves_schema_and_message_conversion():
    payload = {
        'choices': [
            {
                'finish_reason': 'stop',
                'message': {
                    'role': 'assistant',
                    'content': json.dumps({'name': 'read_drc', 'arguments': {'rule': 'M1.S.1'}}),
                    'thinking': 'local reasoning',
                },
            }
        ]
    }
    fake_llama = FakeLlamaModel(payload)
    adapter = LlamaAdapter({'agent_model_path': 'dummy.gguf'}, model_factory=lambda **kw: fake_llama)
    result = adapter.chat([{'role': 'user', 'content': 'hi'}], sample_tools(), sample_response_schema())

    assert result['role'] == 'assistant'
    assert result['content'] is None
    assert result['tool_calls'][0]['function']['name'] == 'read_drc'
    assert json.loads(result['tool_calls'][0]['function']['arguments']) == {'rule': 'M1.S.1'}
    assert adapter.last_thinking == 'local reasoning'
