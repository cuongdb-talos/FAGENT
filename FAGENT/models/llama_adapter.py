"""Local llama.cpp inference and embedding models."""
import json
import math
import re
import uuid
from copy import deepcopy
from pathlib import Path
import jsonschema


class LlamaAdapter:
    def run(self):
        return self

    def __init__(self, config, model_factory=None):
        self.config = config
        self.model_factory = model_factory
        self.agent_model = None
        self.embedding_model = None
        self.embedding_revision = None
        self.message = None
        self.vectors = None

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
                from llama_cpp import Llama
                factory = Llama
            options = {'model_path': model_path, 'verbose': False}
            for key in ('n_ctx', 'n_threads', 'n_gpu_layers'):
                value = self.config.get(f'{purpose}_{key}')
                if value is not None:
                    options[key] = value
            if purpose == 'agent' and self.config.get('agent_chat_format'):
                options['chat_format'] = self.config['agent_chat_format']
            if purpose == 'embedding':
                options['embedding'] = True
            model = factory(**options)
            setattr(self, attr, model)
        return model

    def chat(self, messages, tools):
        schema = self.response_schema(tools)
        response = self.load_model('agent').create_chat_completion(
            messages=messages, response_format={'type': 'json_object', 'schema': schema})
        try:
            if response['choices'][0].get('finish_reason') == 'length':
                raise ValueError('response truncated')
            message = response['choices'][0]['message']
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                raise ValueError('invalid assistant message')
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
            raise ValueError(f'Invalid llama.cpp chat response: {exc}') from exc
        self.message = message
        return self.message

    def response_schema(self, tools):
        alternatives = []
        for tool in tools:
            function = tool['function']
            parameters = deepcopy(function['parameters'])
            parameters['additionalProperties'] = False
            alternatives.append({'type': 'object', 'properties': {
                'name': {'const': function['name']}, 'arguments': parameters},
                'required': ['name', 'arguments'], 'additionalProperties': False})
        alternatives.append({'type': 'object', 'properties': {
            'decision': {'type': 'string', 'enum': ['completed', 'stop']},
            'status': {'type': 'string', 'enum': ['verified', 'incomplete']},
            'reason': {'type': 'string'},
            'violations_analysis': {'type': 'array', 'items': {'type': 'object',
                'properties': {'rule': {'type': 'string'}, 'action': {'type': 'string'}, 'details': {'type': 'string'}},
                'required': ['rule', 'action', 'details'], 'additionalProperties': False}},
            'recommendations': {'type': 'string'}},
            'required': ['decision', 'status', 'reason', 'violations_analysis', 'recommendations'],
            'additionalProperties': False})
        return {'oneOf': alternatives}

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
