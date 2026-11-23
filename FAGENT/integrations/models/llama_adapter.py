"""Local llama.cpp integration for schema-constrained chat completion and embeddings."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import sys
import uuid

import jsonschema
from llama_cpp import (
    GGML_TYPE_F16,
    GGML_TYPE_F32,
    GGML_TYPE_Q4_0,
    GGML_TYPE_Q4_1,
    GGML_TYPE_Q5_0,
    GGML_TYPE_Q5_1,
    GGML_TYPE_Q8_0,
    Llama,
)


# ==============================================================================
# SECTION 2: Shared Structured Chat Schema and Response Normalization
# ==============================================================================
class StructuredChatContract:
    """Composes JSON schemas and normalizes structured chat completion responses."""

    # --------------------------------------------------------------------------
    # Main Setup and Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, response, tools, response_schema):
        schema = self.build_schema(tools, response_schema)
        return self.normalize_response(response, schema)

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, provider_label='llama.cpp'):
        self.provider_label = provider_label
        self.schema = None
        self.last_thinking = None
        self.message = None

    # --------------------------------------------------------------------------
    # JSON Schema Composition
    # --------------------------------------------------------------------------
    def build_schema(self, tools, response_schema):
        alternatives = []
        for tool in tools:
            function = tool['function']
            parameters = deepcopy(function['parameters'])
            parameters['additionalProperties'] = False
            alternatives.append({'type': 'object', 'properties': {
                'name': {'const': function['name']}, 'arguments': parameters},
                'required': ['name', 'arguments'], 'additionalProperties': False})
        alternatives.append(response_schema)
        self.schema = {'oneOf': alternatives}
        return self.schema

    # --------------------------------------------------------------------------
    # Response Validation and Message Normalization
    # --------------------------------------------------------------------------
    def normalize_response(self, response, schema):
        self.last_thinking = None
        try:
            if response['choices'][0].get('finish_reason') == 'length':
                raise ValueError('response truncated')
            message = response['choices'][0]['message']
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                raise ValueError('invalid assistant message')
            thinking = message.get('reasoning_content') or message.get('thinking')
            if isinstance(thinking, str) and thinking.strip():
                self.last_thinking = thinking.strip()
            content = message.get('content')
            if not isinstance(content, str):
                raise ValueError('response content must be JSON')
            content = content.strip()
            match = re.fullmatch(r'```(?:json)?\s*([\s\S]*?)\s*```', content, flags=re.IGNORECASE)
            if match:
                content = match.group(1)
            data = json.loads(content)
            jsonschema.validate(data, schema)
            if 'name' in data:
                message = {'role': 'assistant', 'content': None, 'tool_calls': [{
                    'id': 'call_' + uuid.uuid4().hex, 'type': 'function',
                    'function': {'name': data['name'], 'arguments': json.dumps(data['arguments'], ensure_ascii=False)}}]}
            else:
                message = {'role': 'assistant', 'content': json.dumps(data, ensure_ascii=False)}
            calls = message.get('tool_calls') or []
            if not isinstance(calls, list):
                raise ValueError('invalid tool calls')
            for call in calls:
                function = call['function']
                if not isinstance(call.get('id'), str) or not call['id'] or not isinstance(function.get('name'), str) or not function['name']:
                    raise ValueError('tool call missing id or name')
                arguments = function['arguments']
                if not isinstance(arguments, str) or not isinstance(json.loads(arguments), dict):
                    raise ValueError('invalid tool arguments')
        except (KeyError, IndexError, TypeError, ValueError, jsonschema.ValidationError) as exc:
            raise ValueError(f'Invalid {self.provider_label} chat response: {exc}') from exc
        self.message = message
        return self.message


# ==============================================================================
# SECTION 3: Llama Model Integration Adapter Class
# ==============================================================================
class LlamaAdapter:
    """Provides local inference with GGUF models via llama.cpp for tool-calling chat and embeddings."""

    # --------------------------------------------------------------------------
    # Main Setup and Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, purpose='agent'):
        self.load_model(purpose)
        return self

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, config, model_factory=None):
        self.config = config
        self.model_factory = model_factory
        self.agent_model = None
        self.embedding_model = None
        self.embedding_revision = None
        self.message = None
        self.last_thinking = None
        self.vectors = None
        self.contract = StructuredChatContract('llama.cpp')

    # --------------------------------------------------------------------------
    # Model Lifecycle & Lazy Loading
    # --------------------------------------------------------------------------
    def load_model(self, purpose):
        attr = 'agent_model' if purpose == 'agent' else 'embedding_model'
        model = getattr(self, attr)
        if purpose == 'embedding':
            path = Path(self.config['embedding_model_path']).expanduser().resolve(strict=True)
            stat = path.stat()
            revision = (str(path), stat.st_size, stat.st_mtime_ns)
            if model is not None and revision != self.embedding_revision:
                if hasattr(model, 'close'):
                    model.close()
                model = None
                self.embedding_model = None
            self.embedding_revision = revision
        if model is None:
            factory = self.model_factory
            model_path = self.config[f'{purpose}_model_path']
            if factory is None:
                model_path = str(Path(model_path).expanduser().resolve(strict=True))
                factory = Llama
            options = {'model_path': model_path, 'verbose': False}
            for key in ('n_ctx', 'n_threads', 'n_gpu_layers', 'flash_attn', 'offload_kqv'):
                value = self.config.get(f'{purpose}_{key}')
                if value is not None:
                    options[key] = value
            for key in ('type_k', 'type_v'):
                raw_val = self.config.get(f'{purpose}_{key}')
                if raw_val is not None:
                    options[key] = self.parse_ggml_type(raw_val, f'{purpose}_{key}')
            if purpose == 'agent' and self.config.get('agent_chat_format'):
                options['chat_format'] = self.config['agent_chat_format']
            if purpose == 'embedding':
                options['embedding'] = True
            model = factory(**options)
            setattr(self, attr, model)
        return model

    # --------------------------------------------------------------------------
    # GGML Quantization Type Parsing
    # --------------------------------------------------------------------------
    def parse_ggml_type(self, raw_value, param_name):
        """Converts raw string or integer GGML quantization type specification."""
        type_map = {
            'f32': GGML_TYPE_F32,
            'f16': GGML_TYPE_F16,
            'q4_0': GGML_TYPE_Q4_0,
            'q4_1': GGML_TYPE_Q4_1,
            'q5_0': GGML_TYPE_Q5_0,
            'q5_1': GGML_TYPE_Q5_1,
            'q8_0': GGML_TYPE_Q8_0,
        }
        if isinstance(raw_value, int):
            return raw_value
        if isinstance(raw_value, str):
            key = raw_value.strip().lower()
            if key in type_map:
                return type_map[key]
        sys.stderr.write(f"[ERROR] Invalid {param_name}: '{raw_value}'. Must be int or one of {list(type_map.keys())}\n")
        raise ValueError(f"Invalid {param_name}: '{raw_value}'")

    # --------------------------------------------------------------------------
    # Chat Completion & Response Validation
    # --------------------------------------------------------------------------
    def chat(self, messages, tools, response_schema):
        self.last_thinking = None
        schema = self.response_schema(tools, response_schema)
        response = self.load_model('agent').create_chat_completion(
            messages=messages, response_format={'type': 'json_object', 'schema': schema})
        self.message = self.contract.normalize_response(response, schema)
        self.last_thinking = self.contract.last_thinking
        return self.message

    # --------------------------------------------------------------------------
    # JSON Schema Composition for Structured Outputs
    # --------------------------------------------------------------------------
    def response_schema(self, tools, response_schema):
        return self.contract.build_schema(tools, response_schema)

    # --------------------------------------------------------------------------
    # Text-Based Tool Call Parsing Fallback
    # --------------------------------------------------------------------------
    def normalize_text_calls(self, message):
        content = message.get('content')
        if message.get('tool_calls') or not isinstance(content, str):
            return message
        pattern = r'<(tool_call|xml)>\s*(.*?)\s*</\1>'
        matches = list(re.finditer(pattern, content, flags=re.DOTALL))
        remainder = re.sub(pattern, '', content, flags=re.DOTALL).strip()
        if any(tag in remainder for tag in ('<tool_call>', '</tool_call>', '<xml>', '</xml>')):
            raise ValueError('Malformed textual tool call')
        if not matches:
            return message
        calls = []
        for match in matches:
            data = json.loads(match.group(2))
            if not isinstance(data, dict) or not isinstance(data.get('name'), str) or not data['name'] or not isinstance(data.get('arguments'), dict):
                raise ValueError('Invalid textual tool call')
            calls.append({'id': 'call_' + uuid.uuid4().hex, 'type': 'function', 'function': {
                'name': data['name'], 'arguments': json.dumps(data['arguments'], ensure_ascii=False)}})
        return {**message, 'content': remainder or None, 'tool_calls': calls}

    # --------------------------------------------------------------------------
    # Dense Vector Embedding Generation
    # --------------------------------------------------------------------------
    def embed_batch(self, texts):
        response = self.load_model('embedding').create_embedding(input=texts)
        try:
            data = response['data']
            if len(data) != len(texts) or not data:
                raise ValueError('embedding count mismatch')
            indices = [item['index'] for item in data]
            if sorted(indices) != list(range(len(texts))):
                raise ValueError('embedding indices invalid')
            vectors = [item['embedding'] for item in sorted(data, key=lambda item: item['index'])]
            dimension = len(vectors[0])
            if not dimension or any(not isinstance(vector, list) or len(vector) != dimension or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in vector) for vector in vectors):
                raise ValueError('invalid embedding vectors')
        except (KeyError, TypeError, IndexError, ValueError) as exc:
            raise ValueError(f'Invalid llama.cpp embedding response: {exc}') from exc
        self.vectors = vectors
        return self.vectors
