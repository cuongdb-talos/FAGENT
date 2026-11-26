"""Run a chat model directly, without the FAGENT workflow or request format."""

import argparse
import json
import os
import sys
from pathlib import Path


class ModelReplay:
    """Load one configured chat backend and run a standalone prompt."""

    def run(self):
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('prompt', nargs='?', help='prompt text, or - to read stdin')
        parser.add_argument('--messages', type=Path, help='JSON file containing chat messages')
        parser.add_argument('--backend', choices=('llama', 'openai'), default='llama')
        parser.add_argument('--config', type=Path, help='optional JSON adapter configuration')
        parser.add_argument('--model', help='model path/name (or MODEL_NAME for openai)')
        parser.add_argument('--base-url', help='OpenAI-compatible API base URL')
        parser.add_argument('--api-key-env', help='environment variable containing API key')
        parser.add_argument('--system', help='optional system prompt')
        args = parser.parse_args()

        try:
            config = self.load_config(args.config)
            self.apply_options(config, args)
            messages = self.load_messages(args, parser)
            self.validate(config, messages, args.backend)
            if args.backend == 'llama':
                from FAGENT.integrations.models.llama_adapter import LlamaAdapter
                client = LlamaAdapter(config)
            else:
                from FAGENT.integrations.models.openai_adapter import OpenAIAdapter
                client = OpenAIAdapter(config)
            response_schema = {
                'type': 'object',
                'properties': {'answer': {'type': 'string'}},
                'required': ['answer'],
                'additionalProperties': False,
            }
            message = client.chat(messages, [], response_schema)
            print(json.dumps(message, ensure_ascii=False, indent=2))
            return 0
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError) as exc:
            parser.exit(1, f'{parser.prog}: [ERROR] {exc}\n')

    def __init__(self):
        self.config = {}
        self.messages = []

    def load_config(self, path):
        if path is None:
            return {}
        config = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(config, dict):
            raise ValueError('config must be a JSON object')
        return config

    def apply_options(self, config, args):
        config['chat_backend'] = args.backend
        if args.model:
            config['agent_model_path'] = args.model
        if args.base_url:
            config['base_url'] = args.base_url
        if args.api_key_env:
            config['api_key_env'] = args.api_key_env
    def load_messages(self, args, parser):
        if args.messages is not None:
            messages = json.loads(args.messages.read_text(encoding='utf-8-sig'))
        else:
            prompt = sys.stdin.read() if args.prompt in (None, '-') else args.prompt
            if not prompt.strip():
                parser.error('Provide prompt text, stdin, or --messages PATH')
            messages = []
            if args.system:
                messages.append({'role': 'system', 'content': args.system})
            messages.append({'role': 'user', 'content': prompt})
        if not isinstance(messages, list) or not messages:
            raise ValueError('messages must be a non-empty JSON array')
        return messages

    def validate(self, config, messages, backend):
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or not isinstance(message.get('role'), str) or not isinstance(message.get('content'), str):
                raise ValueError(f'messages[{index}] must contain string role and content fields')
        model = config.get('agent_model_path')
        if not isinstance(model, str) or not model.strip():
            raise ValueError('Provide --model or agent_model_path in --config')
        if backend == 'llama' and not Path(model).expanduser().is_file():
            raise ValueError(f'LLama model file does not exist: {model}')
        if backend == 'openai':
            base_url = config.get('base_url')
            key_env = config.get('api_key_env')
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError('Provide --base-url or base_url in --config')
            if not isinstance(key_env, str) or not key_env.strip() or not os.environ.get(key_env):
                raise ValueError('Provide --api-key-env and set that environment variable')


if __name__ == '__main__':
    sys.exit(ModelReplay().run())
