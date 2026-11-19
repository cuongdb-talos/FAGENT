"""Module FAGENT.agent.caller: Bộ điều khiển vòng lặp gọi công cụ (Tool-Calling Controller) cho LLM.

Trách nhiệm & Chức năng:
- Cung cấp lớp `Agent_caller` quản lý việc giao tiếp với mô hình llama.cpp qua LlamaAdapter.
- Đóng gói schema các công cụ (function calling JSON schema) và đưa vào prompt hệ thống.
- Phân tích cú pháp JSON phản hồi từ LLM, trích xuất chuỗi suy nghĩ CoT (last_thinking) và kích hoạt tool calls.
- Quản lý các dịch vụ nền tảng: LayoutService (functions), HybridRetriever (rag), CaseLearner (learn).
"""
import json
from pathlib import Path
from ..models.llama_adapter import LlamaAdapter
from ..tools.layout import LayoutService
from ..rag.learning import CaseLearner
from ..rag.retriever import HybridRetriever, KNOWLEDGE_ROOT
from .prompts import build_system_prompt
from .review import ReviewState



class Agent_caller:

    def run(self, rundir=None, config=None, verbose=None):
        if rundir is not None:
            self.rundir = Path(rundir).resolve()
        if config is not None:
            self.config = config
        if verbose is not None:
            self.verbose = verbose

        if self.rundir is None or self.config is None:
            raise ValueError('rundir and config must be provided')

        self.functions = LayoutService(rundir=self.rundir, verbose=self.verbose).run()
        self.gds_data = json.loads((self.rundir / 'gds.json').read_text())
        self.drc_data = json.loads((self.rundir / 'drc.json').read_text())

        provider = 'llama.cpp'
        max_iterations = self.config['max_iterations']

        if self.verbose:
            print(f"[Phase 2/5] Context Assembly & Agent Setup")
            print(f"  * Provider: {provider} | Max iterations: {max_iterations}")
            print(f"  * Loaded rules, contexts, and skills from .agent/")
            print(f"[Phase 3/5] Starting Tool-Calling Optimization Loop")

        self.client = self.create_client()
        self.rag = HybridRetriever(self.config.get('knowledge_dir'), self.config, embedding_client=self.client).run()
        self.rag.migrate_legacy(self.rag.knowledge_dir / 'knowledge_db.json')
        references = self.config.get('reference_dir')
        if references:
            self.rag.index_documents(references)
        self.rag.index_rule_decks(self.config.get('drc_files', []))
        self.drc_context = self.initial_drc_context()
        self.messages = [
            {
                'role': 'system',
                'content': self.system_prompt(),
            },
            {
                'role': 'user',
                'content': json.dumps({
                    'gds': self.gds_data,
                    'drc': self.drc_context,
                }),
            },
        ]
        self.result = {
            'status': 'incomplete',
            'reason': 'Iteration limit reached',
        }
        self.review = ReviewState()
        self.save_requested = False
        iteration = 0
        while True:
            if iteration >= max_iterations:
                self.result = {'status': 'incomplete', 'reason': 'Iteration limit reached'}
                if not self.request_review():
                    break
                iteration = 0
            iteration += 1
            if self.verbose:
                print(f"  -> [Iteration {iteration}/{max_iterations}] Requesting assistant response ({provider})...")

            message = self.chat()
            self.messages.append(message)
            calls = message.get('tool_calls') or []
            if not calls:
                if self.verbose:
                    print(f"  -> [Iteration {iteration}] Assistant completed tool calling (no further tools).")
                parsed_response = json.loads(message['content'])
                
                is_val_passed = (
                    isinstance(self.functions.last_validation, dict)
                    and self.functions.last_validation.get('status') in ('verified', 'pass', 'good')
                    and self.functions.last_validation.get('simulated') is False
                )
                if is_val_passed:
                    self.result = {
                        'status': 'verified',
                        'reason': 'Simulated validation passed' if self.functions.last_validation.get('simulated') else 'DRC validation passed',
                        'response': parsed_response,
                    }
                else:
                    self.result = {
                        'status': 'incomplete',
                        'reason': 'Simulated validation is not a real DRC pass' if isinstance(self.functions.last_validation, dict) and self.functions.last_validation.get('simulated') is True else 'Agent stopped without verified DRC',
                        'response': parsed_response,
                    }
                if not self.request_review():
                    break
                iteration = 0
                continue

            for call in calls:
                call_id = call.get('id')
                if not call_id:
                    raise ValueError('Tool call is missing its id')
                name = call.get('function', {}).get('name', '')
                args = call.get('function', {}).get('arguments', {})
                try:
                    if isinstance(args, str):
                        args = json.loads(args)
                    if name == 'store_knowledge':
                        if not self.review.authorized:
                            raise ValueError('Explicit completion and save request are required')
                        if (not isinstance(args, dict) or not isinstance(args.get('analysis'), dict)
                                or not isinstance(args['analysis'].get('violations_analysis'), list)):
                            raise ValueError('Current structured analysis is required before saving')
                        self.result['response'] = args['analysis']
                        self.save_requested = True
                        result = {'status': 'approved_for_storage'}
                        self.messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(result)})
                        break
                    if name not in self.functions.allowed_tools() or not isinstance(args, dict):
                        raise ValueError('Unknown tool or invalid arguments')

                    if self.verbose:
                        args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                        if len(args_str) > 80:
                            args_str = args_str[:77] + '...'
                        print(f"    * Executing Tool: {name}({args_str})")

                    if name in ('replace_polygon', 'validate'):
                        self.review.reset()
                    result = getattr(self.functions, name)(**args)
                    if name == 'validate':
                        self.result['validation'] = result
                except Exception as exc:
                    result = {'error': str(exc)}

                if self.verbose:
                    res_str = json.dumps(result, ensure_ascii=False) if isinstance(result, (dict, list)) else str(result)
                    if len(res_str) > 100:
                        res_str = res_str[:97] + '...'
                    print(f"    * Tool Output: {res_str}")

                self.messages.append({
                    'role': 'tool',
                    'tool_call_id': call_id,
                    'content': json.dumps(result),
                })
            if self.save_requested:
                break

        if self.verbose:
            print(f"[Phase 5/5] Finalizing Execution & Generating Output")
            print(f"  * Run Status: {self.result.get('status')}")
            print(f"  * Termination Reason: {self.result.get('reason')}")
            print(f"  * Artifacts saved: conversation.json, result.json -> {self.rundir}")

        self.result['validation'] = self.functions.last_validation
        self.result['review'] = {'authorized': self.review.authorized, **self.review.evidence}
        self.functions.check_output_path(self.rundir / 'conversation.json')
        self.functions.check_output_path(self.rundir / 'result.json')
        Path(self.rundir, 'conversation.json').write_text(json.dumps(self.messages, indent=2) + '\n')
        Path(self.rundir, 'result.json').write_text(json.dumps(self.result, indent=2) + '\n')

        self.knowledge_dir = self.rag.knowledge_dir
        if self.save_requested and self.review.authorized:
            self.learner = CaseLearner(
                rundir=self.rundir, knowledge_dir=self.knowledge_dir,
                config=self.config, client=self.client, verbose=self.verbose,
            ).run(review=self.review.evidence)

        return self.result

    def request_review(self):
        try:
            feedback = input('Review task (EOF to finish): ')
        except EOFError:
            return False
        self.review.run(feedback)
        if not self.review.authorized:
            print('To allow saving after completion, enter: The task is complete. Please save the knowledge from this run.')
        self.messages.append({'role': 'user', 'content': feedback})
        return True

    def __init__(self, rundir=None, config=None, verbose=True):
        self.rundir = Path(rundir).resolve() if rundir else None
        self.functions = None
        self.config = config
        self.verbose = verbose
        self.client = None
        self.messages = []
        self.result = {}
        self.learner = None
        self.rag = None
        self.drc_data = {}
        self.drc_context = {}
        self.knowledge_dir = None

    def initial_drc_context(self):
        context = dict(self.drc_data)
        rules = context.get('rules')
        if isinstance(rules, list):
            retained = []
            for rule in rules:
                if (isinstance(rule, dict)
                        and type(rule.get('reported_count')) is int and rule['reported_count'] == 0
                        and type(rule.get('total_count')) is int and rule['total_count'] == 0
                        and rule.get('geometry') == []):
                    continue
                retained.append(rule)
            context['rules'] = retained
            context['omitted_clean_rule_count'] = len(rules) - len(retained)
        return context

    def system_prompt(self):
        mandatory = KNOWLEDGE_ROOT / 'mandatory'
        texts = [p.read_text() for p in sorted(mandatory.glob('*.md'))]
        drc = self.drc_context or self.initial_drc_context()
        rules = drc.get('rules') or [{'name': key} for key in drc.get('violations_by_rule', {})]
        names = [rule.get('name', '') for rule in rules if rule.get('name')]
        descriptions = [str(rule.get('description', '')) for rule in rules]
        query = ' '.join(names + descriptions)
        results = self.rag.retrieve(query, names, self.config['rag_limit']) if self.rag and query else []
        rag_prompt = build_system_prompt(self.config, results)
        if rag_prompt:
            texts.append(rag_prompt)
        return '\n\n'.join(texts)


    def create_client(self):
        return LlamaAdapter(self.config)

    def chat(self, msg=None, specs=None):
        if msg is None:
            msg = self.messages
        if specs is None:
            from langchain_core.utils.function_calling import convert_to_openai_tool
            from ..tools.agent_tools import build_tools
            specs = [convert_to_openai_tool(tool) for tool in build_tools(self.functions)]
        specs = specs + [{
            'type': 'function', 'function': {
                'name': 'store_knowledge',
                'description': 'Store experience only after explicit user confirmation of completion and a request to save.',
                'parameters': {'type': 'object', 'properties': {
                    'analysis': {'type': 'object', 'description': 'Current final analysis.',
                                 'properties': {'violations_analysis': {'type': 'array', 'items': {'type': 'object'}}},
                                 'required': ['violations_analysis']}},
                    'required': ['analysis']},
            },
        }]
        instructions = ('Return exactly one JSON object, without Markdown. To request a function, '
                        'use {"name": <function name>, "arguments": <object>}. '
                        'When finished, return the final analysis JSON. Available functions: ' +
                        json.dumps(specs, ensure_ascii=False))
        messages = list(msg)
        messages[0] = {**messages[0], 'content': messages[0]['content'] + '\n\n' + instructions}
        return self.client.chat(messages, specs)
