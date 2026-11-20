"""OpenAI-compatible API integration for schema-constrained chat completions."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
import os

from openai import OpenAI

from .llama_adapter import StructuredChatContract


# ==============================================================================
# SECTION 2: OpenAI-Compatible Chat Adapter Class
# ==============================================================================
class OpenAIAdapter:
    """Provides chat completions via an OpenAI-compatible HTTP endpoint."""

    # --------------------------------------------------------------------------
    # Main Setup and Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self):
        self.load_client()
        return self

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, config, client=None):
        self.config = config
        self.client = client
        self.message = None
        self.last_thinking = None
        self.contract = StructuredChatContract('OpenAI')

    # --------------------------------------------------------------------------
    # Client Lifecycle & Configuration Validation
    # --------------------------------------------------------------------------
    def load_client(self):
        if self.client is not None:
            return self.client
        env_name = self.config.get('api_key_env')
        if not isinstance(env_name, str) or not env_name.strip():
            raise ValueError('Missing api_key_env in configuration')
        api_key = os.environ.get(env_name)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError(f'Missing or empty API key in environment variable: {env_name}')
        base_url = self.config.get('base_url')
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError('Missing base_url in configuration')
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        return self.client

    # --------------------------------------------------------------------------
    # Chat Completion & Response Validation
    # --------------------------------------------------------------------------
    def chat(self, messages, tools, response_schema):
        self.last_thinking = None
        model = self.config.get('agent_model_path')
        if not isinstance(model, str) or not model.strip():
            raise ValueError('Missing agent_model_path in configuration')
        client = self.load_client()
        schema = self.contract.build_schema(tools, response_schema)
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            response_format={'type': 'json_object', 'schema': schema},
        )
        payload = response.model_dump() if hasattr(response, 'model_dump') else response
        if not isinstance(payload, dict):
            raise ValueError('Invalid OpenAI chat response: completion payload must be a dict')
        self.message = self.contract.normalize_response(payload, schema)
        self.last_thinking = self.contract.last_thinking
        return self.message
