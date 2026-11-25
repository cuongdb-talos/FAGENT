"""Runtime service container for LLM, layout tools, RAG, and EDA validation."""

# ==============================================================================
# SECTION 1: Standard, Third-Party, and Internal Module Imports
# ==============================================================================
from hashlib import sha256
import copy
import json
import os
import shutil
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

import gdstk

from ..capabilities.layout.layout_service import LayoutService
from ..capabilities.layout.polygon_ids import PolygonIds
from ..integrations.eda_tools.drc_runner import DrcRunner
from ..integrations.eda_tools.lvs_runner import LvsRunner
from ..integrations.models.llama_adapter import LlamaAdapter
from ..integrations.models.openai_adapter import OpenAIAdapter
from ..rag.case_learner import CaseLearner
from ..rag.hybrid_retriever import HybridRetriever
from .conversation_trace import ConversationTrace
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
        self.drc_report = None
        self.conversation_trace = ConversationTrace(self.rundir, self.check_output_path)

    # --------------------------------------------------------------------------
    # Static Validation and Checksum Utilities
    # --------------------------------------------------------------------------
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

    candidate_digest = staticmethod(candidate_digest)

    def clean_rule(rule):
        return (
            isinstance(rule, dict)
            and type(rule.get('reported_count')) is int
            and rule['reported_count'] == 0
            and type(rule.get('total_count')) is int
            and rule['total_count'] == 0
            and rule.get('geometry') == []
        )

    clean_rule = staticmethod(clean_rule)

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

    def build_system_message(self, goal=None, constraints=None):
        """Construct system message consisting only of configured goal and constraints."""
        cfg = getattr(self, 'config', {}) or {}
        if goal is None:
            goal = cfg.get('goal', '')
        if constraints is None:
            path_str = cfg.get('constraints_file')
            path = Path(path_str) if path_str else Path(__file__).resolve().parents[1] / 'knowledge' / 'constraints.json'
            if path.is_file():
                try:
                    loaded = json.loads(path.read_text(encoding='utf-8'))
                    if isinstance(loaded, list):
                        constraints = loaded
                    else:
                        constraints = []
                except Exception:
                    constraints = []
            else:
                constraints = []
        parts = []
        if goal and str(goal).strip():
            parts.append(str(goal).strip())
        if constraints:
            cleaned = [str(c).strip() for c in constraints if str(c).strip()]
            if cleaned:
                parts.append('Constraints:\n' + '\n'.join(f'- {item}' for item in cleaned))
        return '\n\n'.join(parts).strip()

    drc_system_prompt = build_system_message

    def node_prompt(self, step=None):
        """Build node-local instruction including site prompt and protocol."""
        texts = []
        step_prompt = getattr(step, 'prompt', None) if step else None
        if step_prompt:
            texts.append(step_prompt.strip())
        elif self.workflow and getattr(self.workflow, 'prompt_text', None):
            texts.extend(self.workflow.prompt_text)
        custom = getattr(self, 'custom_prompt', None)
        if custom:
            texts.append(custom.strip())
        protocol = getattr(self.workflow, 'protocol_text', None) if self.workflow else None
        if protocol:
            texts.append(protocol.strip())
        return '\n\n'.join(texts).strip()

    repair_prompt = node_prompt

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
        self.recover_candidate_transaction()
        op_id = None
        if hasattr(self, 'conversation_trace') and self.conversation_trace:
            op_id = self.conversation_trace.start_operation(
                'service_setup',
                {'resume': resume, 'layout_services': self.workflow.layout_services, 'task': self.workflow.task},
            )
        try:
            if not self.workflow.layout_services:
                self.client = self.build_chat_client()
                self.embedding_client = None
                self.messages = [{'role': 'system', 'content': self.system_prompt([])}]
                if op_id and hasattr(self, 'conversation_trace') and self.conversation_trace:
                    self.conversation_trace.finish_operation(op_id, 'service_setup', status='completed')
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
                if op_id and hasattr(self, 'conversation_trace') and self.conversation_trace:
                    self.conversation_trace.finish_operation(
                        op_id,
                        'service_setup',
                        status='completed',
                        outputs={'input_sources': self.input_sources},
                    )
                return self.messages
            if self.workflow.layout_services:
                self.messages = [{'role': 'system', 'content': self.build_system_message()}]
            else:
                self.messages = [{'role': 'system', 'content': self.system_prompt([])}]
            if op_id and hasattr(self, 'conversation_trace') and self.conversation_trace:
                self.conversation_trace.finish_operation(
                    op_id,
                    'service_setup',
                    status='completed',
                    outputs={'input_sources': self.input_sources},
                )
            return self.messages
        except Exception as exc:
            if op_id and hasattr(self, 'conversation_trace') and self.conversation_trace:
                self.conversation_trace.finish_operation(op_id, 'service_setup', status='failed', error=exc)
            raise

    def build_drc_context(self):
        """Read DRC and layout data, then retrieve related knowledge for the graph."""
        read_op = None
        if hasattr(self, 'conversation_trace') and self.conversation_trace:
            read_op = self.conversation_trace.start_operation(
                'drc_read',
                {'source': getattr(self.layout, 'drc_path', None) or getattr(self.layout, 'drc_data', {}).get('source')},
            )
        try:
            report = self.layout.read_drc()
            self.drc_report = report
            if read_op and hasattr(self, 'conversation_trace') and self.conversation_trace:
                self.conversation_trace.finish_operation(
                    read_op,
                    'drc_read',
                    status='completed',
                    outputs={'report': report, 'total_violations': report.get('total_violations', len(report.get('rules', [])))},
                )
        except Exception as exc:
            if read_op and hasattr(self, 'conversation_trace') and self.conversation_trace:
                self.conversation_trace.finish_operation(read_op, 'drc_read', status='failed', error=exc)
            raise

        if self.rag is None:
            rag_init_op = None
            if hasattr(self, 'conversation_trace') and self.conversation_trace:
                rag_init_op = self.conversation_trace.start_operation(
                    'rag_init',
                    {
                        'knowledge_dir': str(self.config.get('knowledge_dir')),
                        'reference_dir': str(self.config.get('reference_dir')) if self.config.get('reference_dir') else None,
                        'drc_files': self.config.get('drc_files', []),
                    },
                )
            try:
                self.embedding_client = LlamaAdapter(self.config)
                self.rag = HybridRetriever(self.config.get('knowledge_dir'), self.config, embedding_client=self.embedding_client).run()
                self.rag.migrate_legacy(self.rag.knowledge_dir / 'knowledge_db.json')
                doc_count = 0
                if self.config.get('reference_dir'):
                    doc_count = self.rag.index_documents(self.config['reference_dir'])
                deck_count = self.rag.index_rule_decks(self.config.get('drc_files', []))
                if rag_init_op and hasattr(self, 'conversation_trace') and self.conversation_trace:
                    self.conversation_trace.finish_operation(
                        rag_init_op,
                        'rag_init',
                        status='completed',
                        outputs={'indexed_documents': doc_count, 'indexed_rules': deck_count},
                    )
            except Exception as exc:
                if rag_init_op and hasattr(self, 'conversation_trace') and self.conversation_trace:
                    self.conversation_trace.finish_operation(rag_init_op, 'rag_init', status='failed', error=exc)
                raise

        context = self.drc_context(report)
        rules = context.get('rules') or [{'name': key} for key in context.get('violations_by_rule', {})]
        names = [rule['name'] for rule in rules if rule.get('name')]
        query = ' '.join(names + [str(rule.get('description', '')) for rule in rules])

        rag_ret_op = None
        if hasattr(self, 'conversation_trace') and self.conversation_trace:
            rag_ret_op = self.conversation_trace.start_operation(
                'rag_retrieve',
                {
                    'query': query,
                    'rules': names,
                    'limit': self.config.get('rag_limit', 0),
                },
            )
        try:
            references = self.rag.retrieve(query, names, self.config['rag_limit']) if query else []
            if rag_ret_op and hasattr(self, 'conversation_trace') and self.conversation_trace:
                self.conversation_trace.finish_operation(
                    rag_ret_op,
                    'rag_retrieve',
                    status='completed',
                    outputs={
                        'query': query,
                        'rules': names,
                        'limit': self.config.get('rag_limit', 0),
                        'results_count': len(references),
                        'records': references,
                    },
                )
        except Exception as exc:
            if rag_ret_op and hasattr(self, 'conversation_trace') and self.conversation_trace:
                self.conversation_trace.finish_operation(rag_ret_op, 'rag_retrieve', status='failed', error=exc)
            raise

        compact = {key: report[key] for key in ('top_cell', 'dbu_per_user_unit', 'total_violations', 'clean') if key in report}
        compact['rules'] = [
            {key: rule[key] for key in ('name', 'description', 'reported_count', 'total_count', 'geometry') if key in rule}
            for rule in context.get('rules', [])
        ]
        compact['omitted_clean_rule_count'] = context.get('omitted_clean_rule_count', 0)
        path = Path(self.config.get('constraints_file') or Path(__file__).resolve().parents[1] / 'knowledge' / 'constraints.json')
        constraints = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(constraints, list) or any(not isinstance(item, str) or not item.strip() for item in constraints):
            raise ValueError('Knowledge constraints must be a list of non-empty strings')
        goal = self.config.get('goal', '')
        return {'drc': compact, 'knowledge': {'references': references, 'constraints': constraints}, 'goal': goal}

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
        """Atomically stage and commit candidate.gds and polygon_ids.json together."""
        self.recover_candidate_transaction()
        self.layout.check_output_path(self.layout.candidate)
        ids_path = self.rundir / 'polygon_ids.json'
        self.check_output_path(ids_path)
        self.rundir.mkdir(parents=True, exist_ok=True)
        self.layout.load_library()
        if self.layout.polygon_ids is None:
            raise ValueError('Polygon IDs are not initialized for candidate checkpoint')

        tmp_gds = NamedTemporaryFile(dir=self.rundir, suffix='.gds', delete=False)
        tmp_gds_path = Path(tmp_gds.name)
        tmp_gds.close()
        tmp_ids = NamedTemporaryFile(dir=self.rundir, suffix='.json', delete=False)
        tmp_ids_path = Path(tmp_ids.name)
        tmp_ids.close()
        try:
            self.layout.library.write_gds(str(tmp_gds_path))
            digest = self.candidate_digest(tmp_gds_path)
            if not digest:
                raise ValueError('Missing candidate digest for staged GDS')
            self.layout.polygon_ids.save(tmp_ids_path, digest)
            previous = [(self.layout.candidate, self.rundir / '.candidate.previous'),
                        (ids_path, self.rundir / '.polygon_ids.previous')]
            existed = [target.is_file() for target, _ in previous]
            for (target, backup), present in zip(previous, existed):
                if present:
                    shutil.copyfile(target, backup)
            journal = self.rundir / '.candidate-transaction.json'
            with NamedTemporaryFile('w', dir=self.rundir, delete=False, encoding='utf-8') as handle:
                journal_tmp = Path(handle.name)
                json.dump({'candidate_existed': existed[0], 'ids_existed': existed[1]}, handle)
                handle.flush()
                os.fsync(handle.fileno())
            journal_tmp.replace(journal)
            tmp_gds_path.replace(self.layout.candidate)
            tmp_ids_path.replace(ids_path)
            journal.unlink()
            for _, backup in previous:
                backup.unlink(missing_ok=True)
            if getattr(self.layout, 'verbose', True):
                print(f"    [Candidate] Candidate layout saved to {self.layout.candidate}")
            return str(self.layout.candidate)
        except Exception:
            self.recover_candidate_transaction()
            raise
        finally:
            tmp_gds_path.unlink(missing_ok=True)
            tmp_ids_path.unlink(missing_ok=True)

    def recover_candidate_transaction(self):
        """Restore the previous pair after an interrupted candidate commit."""
        journal = self.rundir / '.candidate-transaction.json'
        if not journal.exists():
            return
        transaction = json.loads(journal.read_text(encoding='utf-8'))
        pairs = [('candidate_existed', 'candidate.gds', '.candidate.previous'),
                 ('ids_existed', 'polygon_ids.json', '.polygon_ids.previous')]
        for key, name, backup_name in pairs:
            target = self.rundir / name
            backup = self.rundir / backup_name
            if transaction[key]:
                if not backup.is_file():
                    raise ValueError(f'Missing candidate transaction backup: {backup}')
                with NamedTemporaryFile(dir=self.rundir, delete=False) as handle:
                    temporary = Path(handle.name)
                try:
                    shutil.copyfile(backup, temporary)
                    temporary.replace(target)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                target.unlink(missing_ok=True)
        journal.unlink()
        for _, _, backup_name in pairs:
            (self.rundir / backup_name).unlink(missing_ok=True)

    def validate_drc(self, candidate):
        return DrcRunner(self.layout, candidate, self.workflow.backend).run()

    def restore_candidate(self, path, validation, expected_digest=None):
        candidate = Path(path).resolve()
        if candidate != self.layout.candidate.resolve() or not candidate.is_file():
            raise ValueError('Invalid checkpoint candidate path')
        self.check_output_path(candidate)
        digest = expected_digest or self.candidate_digest(candidate)
        library = gdstk.read_gds(str(candidate))
        self.layout.polygon_ids = PolygonIds().restore(self.rundir / 'polygon_ids.json', library, digest)
        self.layout.library = library
        self.layout.dirty = True
        self.layout.last_validation = validation

    # --------------------------------------------------------------------------
    # LLM Interaction and Review Feedback Interpretation
    # --------------------------------------------------------------------------
    def embed_texts(self, texts):
        """Embed texts using the configured local GGUF and reuse the RAG embedding client."""
        batch_size = self.config.get('embedding_batch_size', 32)
        if type(batch_size) is not int or batch_size < 1:
            raise ValueError('embedding_batch_size must be a positive integer')
        if self.embedding_client is None:
            self.embedding_client = LlamaAdapter(self.config)
        vectors = []
        for start in range(0, len(texts), batch_size):
            vectors.extend(self.embedding_client.embed_batch(texts[start:start + batch_size]))
        return vectors

    def chat(self, messages, specs, response_schema=None):
        schema = self.workflow.response_schema if response_schema is None else response_schema
        req_id = self.conversation_trace.allocate_request_id()
        self.conversation_trace.append({
            'type': 'model_request',
            'request_id': req_id,
            'messages': copy.deepcopy(messages),
            'tools': copy.deepcopy(specs),
            'response_schema': copy.deepcopy(schema),
        })
        try:
            response = self.client.chat(messages, specs, schema)
        except Exception as exc:
            self.conversation_trace.append({
                'type': 'model_error',
                'request_id': req_id,
                'error_type': type(exc).__name__,
                'error_message': str(exc),
            })
            raise
        self.conversation_trace.append({
            'type': 'model_response',
            'request_id': req_id,
            'message': copy.deepcopy(response),
        })
        return response

    def interpret_feedback(self, feedback, state):
        if self.workflow.feedback_schema is None:
            return {'decision': feedback.strip().lower()}
        messages = [
            {'role': 'system', 'content': self.workflow.review_text},
            {'role': 'user', 'content': json.dumps({
                'feedback': feedback,
                'validation': state.get('validation'),
                'result': state.get('result'),
                'checklist': state.get('checklist'),
            })},
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
