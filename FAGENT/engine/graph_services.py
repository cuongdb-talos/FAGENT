"""Runtime service container for LLM, layout tools, RAG, and EDA validation."""

# ==============================================================================
# SECTION 1: Standard, Third-Party, and Internal Module Imports
# ==============================================================================
from hashlib import sha256
import json
import sys
from pathlib import Path

import gdstk

from ..capabilities.layout.layout_service import LayoutService
from ..integrations.eda_tools.drc_runner import DrcRunner
from ..integrations.eda_tools.lvs_runner import LvsRunner
from ..integrations.models.llama_adapter import LlamaAdapter
from ..integrations.models.openai_adapter import OpenAIAdapter
from ..rag.case_learner import CaseLearner
from ..rag.hybrid_retriever import HybridRetriever
from .trace import Trace

# ==============================================================================
# SECTION 2: Service Container and Coordinator Class
# ==============================================================================
class GraphServices:
    """Provides dependency injection and execution services (Layout, LLM, RAG, EDA) for graph nodes."""

    # --------------------------------------------------------------------------
    # Main Setup Entry Point
    # --------------------------------------------------------------------------
    def run(self):
        self.prepare()
        return self

    # --------------------------------------------------------------------------
    # Initialization and Service State Configuration
    # --------------------------------------------------------------------------
    def __init__(self, rundir, config, workflow, verbose=False):
        self.rundir = Path(rundir).resolve()
        self.config = config
        self.workflow = workflow
        self.verbose = False
        self.trace = Trace(verbose, sys.stderr)
        self.max_iterations = config.get('max_iterations', 20)
        if type(self.max_iterations) is not int or self.max_iterations < 1:
            raise ValueError('max_iterations must be a positive integer')
        prompt_file = config.get('system_prompt_file')
        self.custom_prompt = Path(prompt_file).read_text() if prompt_file else config.get('system_prompt')
        self.config_digest = sha256(json.dumps([config, self.custom_prompt], sort_keys=True).encode()).hexdigest()
        self.layout = None
        self.client = None
        self.embedding_client = None
        self.rag = None
        self.messages = []
        self.input_sources = None
        self.learner = None

    # --------------------------------------------------------------------------
    # Static Validation and Checksum Utilities
    # --------------------------------------------------------------------------
    @staticmethod
    def candidate_digest(path):
        digest = sha256()
        if not path.is_file():
            return None
        with path.open('rb') as stream:
            chunk = stream.read(1024 * 1024)
            while chunk:
                digest.update(chunk)
                chunk = stream.read(1024 * 1024)
        return digest.hexdigest()

    @staticmethod
    def clean_rule(rule):
        return (
            isinstance(rule, dict)
            and type(rule.get('reported_count')) is int
            and rule['reported_count'] == 0
            and type(rule.get('total_count')) is int
            and rule['total_count'] == 0
            and rule.get('geometry') == []
        )

    # --------------------------------------------------------------------------
    # Prompt Composition and RAG Context Formatting
    # --------------------------------------------------------------------------
    def system_prompt(self, retrieved):
        texts = list(self.workflow.prompt_text)
        if self.custom_prompt:
            texts.append(self.custom_prompt.strip())
        maximum = self.config.get('rag_context_chars', 8000)
        selected = []
        for record in retrieved:
            item = {key: record.get(key) for key in ('id', 'rule', 'outcome', 'validation_source', 'source', 'retrieval_mode', 'score')}
            review = record.get('review')
            if isinstance(review, dict):
                item['review'] = {
                    'completed': review.get('completed'),
                    'save_requested': review.get('save_requested'),
                    'feedback': str(review.get('feedback', ''))[:512],
                }
            item['content'] = str(record.get('content', ''))[:maximum]
            while len(json.dumps(selected + [item], ensure_ascii=False)) > maximum and item['content']:
                item['content'] = item['content'][:len(item['content']) // 2]
            if len(json.dumps(selected + [item], ensure_ascii=False)) <= maximum:
                selected.append(item)
        if selected:
            texts.append(self.workflow.reference_label + '\n' + json.dumps(selected, ensure_ascii=False))

        if self.workflow.protocol_text:
            texts.append(self.workflow.protocol_text)
        return '\n\n'.join(texts)

    # --------------------------------------------------------------------------
    # Environment and Subsystem Preparation
    # --------------------------------------------------------------------------
    def build_chat_client(self):
        backend = self.config.get('chat_backend', 'llama')
        if backend == 'llama':
            return LlamaAdapter(self.config)
        if backend == 'openai':
            return OpenAIAdapter(self.config)
        raise ValueError(f'Unknown chat_backend: {backend!r}')

    def prepare(self, resume=False):
        if not self.workflow.layout_services:
            self.client = self.build_chat_client()
            self.embedding_client = None
            self.messages = [{'role': 'system', 'content': self.system_prompt([])}]
            return self.messages
        self.layout = LayoutService(self.rundir, self.verbose, self.config.get('drc_files')).run()
        self.input_sources = {
            'gds': str(self.layout.gds_path),
            'drc': self.layout.drc_data.get('source'),
            'layer_maps': self.layout.gds_data.get('layer_maps', []),
            'decks': self.layout.drc_files,
        }
        self.client = self.build_chat_client()
        if resume:
            self.rag = None
            self.embedding_client = None
            self.messages = []
            return self.messages
        self.embedding_client = LlamaAdapter(self.config)
        self.rag = HybridRetriever(self.config.get('knowledge_dir'), self.config, embedding_client=self.embedding_client).run()
        self.rag.migrate_legacy(self.rag.knowledge_dir / 'knowledge_db.json')
        if self.config.get('reference_dir'):
            self.rag.index_documents(self.config['reference_dir'])
        self.rag.index_rule_decks(self.config.get('drc_files', []))
        context = self.drc_context(self.layout.drc_data)
        rules = context.get('rules') or [{'name': key} for key in context.get('violations_by_rule', {})]
        names = [rule['name'] for rule in rules if rule.get('name')]
        query = ' '.join(names + [str(rule.get('description', '')) for rule in rules])
        references = self.rag.retrieve(query, names, self.config['rag_limit']) if query else []
        payload = {'gds': self.layout.gds_data}
        if self.config.get('goal'):
            payload['goal'] = self.config['goal']
        self.messages = [
            {'role': 'system', 'content': self.system_prompt(references)},
            {'role': 'user', 'content': json.dumps(payload)},
        ]
        return self.messages

    # --------------------------------------------------------------------------
    # DRC and Layout Operations
    # --------------------------------------------------------------------------
    def drc_context(self, report):
        context = dict(report)
        rules = context.get('rules')
        if isinstance(rules, list):
            retained = [rule for rule in rules if not self.clean_rule(rule)]
            context.update(rules=retained, omitted_clean_rule_count=len(rules) - len(retained))
        return context

    def read_drc(self):
        return self.drc_context(self.layout.read_drc())

    def checkpoint_candidate(self):
        return self.layout.save_gds()['candidate']

    def validate_drc(self, candidate):
        return DrcRunner(self.layout, candidate, self.workflow.backend).run()

    def restore_candidate(self, path, validation):
        candidate = Path(path).resolve()
        if candidate != self.layout.candidate.resolve() or not candidate.is_file():
            raise ValueError('Invalid checkpoint candidate path')
        self.check_output_path(candidate)
        self.layout.library = gdstk.read_gds(str(candidate))
        self.layout.dirty = True
        self.layout.last_validation = validation

    # --------------------------------------------------------------------------
    # LLM Interaction and Review Feedback Interpretation
    # --------------------------------------------------------------------------
    def chat(self, messages, specs):
        return self.client.chat(messages, specs, self.workflow.response_schema)

    def interpret_feedback(self, feedback, state):
        if self.workflow.feedback_schema is None:
            return {'decision': feedback.strip().lower()}
        messages = [
            {'role': 'system', 'content': self.workflow.review_text},
            {'role': 'user', 'content': json.dumps({'feedback': feedback, 'validation': state.get('validation')})},
        ]
        response = self.client.chat(messages, (), self.workflow.feedback_schema)
        return json.loads(response['content'])

    # --------------------------------------------------------------------------
    # LVS Tool Operations
    # --------------------------------------------------------------------------
    def read_lvs(self):
        report = self.config.get('lvs_report')
        return {
            'status': 'unavailable',
            'source': report,
            'reason': 'LVS report format is unavailable' if report else 'No LVS report supplied',
        }

    def validate_lvs(self):
        return LvsRunner(self.workflow.backend, self.workflow.mock_pass).run()

    # --------------------------------------------------------------------------
    # Output Directory Validation and Knowledge Storage
    # --------------------------------------------------------------------------
    def check_output_path(self, path):
        if not path.resolve().is_relative_to(self.rundir):
            raise ValueError(f'Output escapes rundir: {path}')
        if self.layout is not None:
            self.layout.check_output_path(path)
        else:
            sources = [
                Path(value)
                for key in ('lvs_report', 'layout', 'netlist', 'deck')
                if (value := self.config.get(key))
            ]
            if any(
                path.resolve() == source.resolve()
                or (path.exists() and source.exists() and path.samefile(source))
                for source in sources
            ):
                raise ValueError(f'Unsafe LVS output path: {path}')

    def store_knowledge(self, review):
        self.learner = CaseLearner(
            rundir=self.rundir,
            knowledge_dir=self.config.get('knowledge_dir'),
            config=self.config,
            client=self.client,
            verbose=self.verbose,
        ).run(review=review)
        return self.learner
