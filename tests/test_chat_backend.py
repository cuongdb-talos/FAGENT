"""Tests for GraphServices chat_backend routing and independent embedding client."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
from pathlib import Path
from types import SimpleNamespace
import pytest

from FAGENT.engine import graph_services as gs_module
from FAGENT.engine.graph_services import GraphServices


# ==============================================================================
# SECTION 2: Module-Level Test Doubles and Fixtures
# ==============================================================================
class FakeLlamaAdapter:
    """Tracks LlamaAdapter instantiations and chat calls."""

    def run(self):
        return self

    def __init__(self, config):
        self.config = config
        self.chat_calls = []

    def chat(self, messages, tools, response_schema):
        self.chat_calls.append((messages, tools, response_schema))
        return {'role': 'assistant', 'content': '{"decision": "continue"}'}


class FakeOpenAIAdapter:
    """Tracks OpenAIAdapter instantiations and chat calls."""

    def run(self):
        return self

    def __init__(self, config):
        self.config = config
        self.chat_calls = []

    def chat(self, messages, tools, response_schema):
        self.chat_calls.append((messages, tools, response_schema))
        return {'role': 'assistant', 'content': '{"decision": "stop"}'}


class FakeLayoutService:
    """Minimal LayoutService double for GraphServices.prepare()."""

    def __init__(self, rundir, verbose, drc_files):
        self.rundir = rundir
        self.verbose = verbose
        self.drc_files = drc_files or []
        self.gds_path = rundir / 'cell.gds'
        self.gds_data = {'top_cell': 'CELL', 'layer_maps': []}
        self.drc_data = {'source': 'cell.drc.results', 'rules': [{'name': 'M1.W.1', 'description': 'width'}]}

    def run(self):
        return self


class FakeHybridRetriever:
    """Captures embedding_client passed from GraphServices."""

    instances = []

    def __init__(self, knowledge_dir, config, embedding_client=None):
        self.knowledge_dir = Path(knowledge_dir) if knowledge_dir else Path('.')
        self.config = config
        self.embedding_client = embedding_client
        FakeHybridRetriever.instances.append(self)

    def run(self):
        return self

    def migrate_legacy(self, path):
        return None

    def index_documents(self, ref_dir):
        return None

    def index_rule_decks(self, decks):
        return None

    def retrieve(self, query, names, limit):
        return []


class FakeCaseLearner:
    """Captures chat client passed from GraphServices.store_knowledge()."""

    instances = []

    def __init__(self, rundir, knowledge_dir, config, client, verbose=False):
        self.rundir = rundir
        self.knowledge_dir = knowledge_dir
        self.config = config
        self.client = client
        self.verbose = verbose
        FakeCaseLearner.instances.append(self)

    def run(self, review):
        self.review = review
        return self


def make_workflow(layout_services=True):
    return SimpleNamespace(
        layout_services=layout_services,
        prompt_text=['system prompt'],
        reference_label='References:',
        protocol_text='protocol',
        response_schema={'type': 'object'},
        review_text='review prompt',
        feedback_schema={'type': 'object', 'properties': {'decision': {'type': 'string'}}},
    )


def patch_services(monkeypatch):
    FakeHybridRetriever.instances.clear()
    FakeCaseLearner.instances.clear()
    monkeypatch.setattr(gs_module, 'LlamaAdapter', FakeLlamaAdapter)
    monkeypatch.setattr(gs_module, 'OpenAIAdapter', FakeOpenAIAdapter, raising=False)
    monkeypatch.setattr(gs_module, 'LayoutService', FakeLayoutService)
    monkeypatch.setattr(gs_module, 'HybridRetriever', FakeHybridRetriever)
    monkeypatch.setattr(gs_module, 'CaseLearner', FakeCaseLearner)


# ==============================================================================
# SECTION 3: Backend Selection and Embedding Injection Tests
# ==============================================================================
@pytest.mark.parametrize('backend_cfg', [{}, {'chat_backend': 'llama'}])
def test_absent_or_llama_backend_selects_local_chat(tmp_path, monkeypatch, backend_cfg):
    patch_services(monkeypatch)
    config = {'agent_model_path': 'local.gguf', 'embedding_model_path': 'embed.gguf', 'rag_limit': 3, **backend_cfg}
    services = GraphServices(tmp_path, config, make_workflow(layout_services=True))

    services.prepare(resume=False)

    assert isinstance(services.client, FakeLlamaAdapter)
    assert isinstance(services.embedding_client, FakeLlamaAdapter)
    assert FakeHybridRetriever.instances[-1].embedding_client is services.embedding_client


def test_openai_backend_selects_api_chat_and_local_embedding_layout(tmp_path, monkeypatch):
    patch_services(monkeypatch)
    config = {
        'chat_backend': 'openai',
        'base_url': 'https://api.tokenlab.amd.com/v1',
        'agent_model_path': 'DeepSeek-V4-Flash',
        'api_key_env': 'TOKENLAB_API_KEY',
        'embedding_model_path': 'embed.gguf',
        'rag_limit': 3,
    }
    services = GraphServices(tmp_path, config, make_workflow(layout_services=True))

    services.prepare(resume=False)

    assert isinstance(services.client, FakeOpenAIAdapter)
    assert isinstance(services.embedding_client, FakeLlamaAdapter)
    assert services.embedding_client is not services.client
    assert FakeHybridRetriever.instances[-1].embedding_client is services.embedding_client

    services.chat([{'role': 'user', 'content': 'hi'}], ())
    feedback = services.interpret_feedback('stop', {'validation': {}})
    services.store_knowledge({'completed': True, 'save_requested': True})

    assert len(services.client.chat_calls) == 2
    assert feedback == {'decision': 'stop'}
    assert FakeCaseLearner.instances[-1].client is services.client


def test_openai_backend_non_layout_and_resume(tmp_path, monkeypatch):
    patch_services(monkeypatch)
    config = {
        'chat_backend': 'openai',
        'base_url': 'https://api.tokenlab.amd.com/v1',
        'agent_model_path': 'DeepSeek-V4-Flash',
        'api_key_env': 'TOKENLAB_API_KEY',
    }
    non_layout = GraphServices(tmp_path, config, make_workflow(layout_services=False))
    non_layout.prepare(resume=False)
    assert isinstance(non_layout.client, FakeOpenAIAdapter)
    assert non_layout.embedding_client is None
    assert FakeHybridRetriever.instances == []

    resumed = GraphServices(tmp_path, config, make_workflow(layout_services=True))
    resumed.prepare(resume=True)
    assert isinstance(resumed.client, FakeOpenAIAdapter)
    assert resumed.embedding_client is None
    assert FakeHybridRetriever.instances == []


@pytest.mark.parametrize('layout_services', [False, True])
def test_unknown_chat_backend_raises_value_error(tmp_path, monkeypatch, layout_services):
    patch_services(monkeypatch)
    config = {'chat_backend': 'unknown_provider', 'agent_model_path': 'x'}
    services = GraphServices(tmp_path, config, make_workflow(layout_services=layout_services))

    with pytest.raises(ValueError, match='chat_backend'):
        services.prepare(resume=False)
