"""Replay one model_request event through a FAGENT chat adapter."""

import argparse
import json
import sys
import warnings
from pathlib import Path


class ModelReplay:
    def run(self):
        parser = argparse.ArgumentParser(description=__doc__)
        source = parser.add_mutually_exclusive_group()
        source.add_argument('request', nargs='?', help='one complete JSON object, or - for stdin')
        source.add_argument('--file', type=Path, help='file containing one model_request object')
        parser.add_argument('--config', type=Path, help='FAGENT model configuration file')
        args = parser.parse_args()

        if args.file is not None:
            request_source = str(args.file)
        elif args.request in (None, '-'):
            request_source = 'stdin'
        else:
            request_source = 'request argument'

        try:
            if args.file is not None:
                request_text = args.file.read_text(encoding='utf-8-sig')
            else:
                request_text = sys.stdin.read() if request_source == 'stdin' else args.request
            request = json.loads(request_text)
        except json.JSONDecodeError as exc:
            parser.error(
                f'Invalid JSON in {request_source}: {exc}. '
                'Provide one complete model_request object enclosed in { and }. '
                'For a file, use --file PATH or redirect it to stdin.'
            )
        except (OSError, UnicodeError) as exc:
            parser.error(f'Cannot read {request_source}: {exc}')

        try:
            if not isinstance(request, dict) or request.get('type') != 'model_request':
                raise ValueError('Expected a model_request JSON object')
            messages = request.get('messages')
            if not isinstance(messages, list) or not messages:
                raise ValueError('messages must be a non-empty array')
            tools = request.get('tools', [])
            schema = request.get('response_schema', {
                'type': 'object',
                'properties': {'answer': {'type': 'string'}},
                'required': ['answer'],
                'additionalProperties': False,
            })
            if not isinstance(tools, list) or not isinstance(schema, (dict, bool)):
                raise ValueError('Expected an array of tools and a JSON response_schema')

            with warnings.catch_warnings():
                from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

                warnings.filterwarnings(
                    'ignore', message=r'The default value of `allowed_objects` will change',
                    category=LangChainPendingDeprecationWarning,
                )
                from FAGENT.fagent import Fagent
                from FAGENT.integrations.models.llama_adapter import LlamaAdapter
                from FAGENT.integrations.models.openai_adapter import OpenAIAdapter

            config_path = args.config if args.config is not None else Fagent.DEFAULT_CONFIG
            config = json.loads(config_path.read_text(encoding='utf-8'))
            backend = config.get('chat_backend', 'llama')
            adapters = {'llama': LlamaAdapter, 'openai': OpenAIAdapter}
            if backend not in adapters:
                raise ValueError(f'Unknown chat_backend: {backend!r}')
            client = adapters[backend](config)
        except (OSError, ValueError, KeyError) as exc:
            parser.exit(1, f'{parser.prog}: {exc}\n')

        try:
            message = client.chat(messages, tools, schema)
        except (OSError, ValueError, KeyError) as exc:
            self.response = {
                'type': 'model_error',
                'request_id': request.get('request_id'),
                'error_message': str(exc),
                'raw_response': client.contract.raw_response,
            }
            print(json.dumps(self.response, ensure_ascii=False, indent=2))
            return 1

        self.response = {
            'type': 'model_response',
            'request_id': request.get('request_id'),
            'message': message,
        }
        print(json.dumps(self.response, ensure_ascii=False, indent=2))
        return 0


if __name__ == '__main__':
    sys.exit(ModelReplay().run())
