"""Extract labeled repair cases and add them to local RAG knowledge."""
import hashlib
import json
from pathlib import Path

from .retriever import HybridRetriever
from ..agent.review import ReviewState


class CaseLearner:
    def run(self, rundir=None, knowledge_dir=None, config=None, client=None, verbose=None, review=None):
        if rundir is not None:
            self.rundir = Path(rundir).resolve()
        if knowledge_dir is not None:
            self.knowledge_dir = Path(knowledge_dir).resolve()
        if config is not None:
            self.config = config
        if client is not None:
            self.client = client
        if verbose is not None:
            self.verbose = verbose
        if self.rundir is None:
            raise ValueError('rundir must be provided')
        self.review = ReviewState().run((review or {}).get('feedback', ''))
        if not self.review.authorized:
            self.learned = False
            return self
        self.result_data = self.read_json('result.json')
        self.drc_data = self.read_json('drc.json')
        self.conversation_data = self.read_json('conversation.json')
        if not isinstance(self.result_data, dict) or not self.result_data:
            raise ValueError('result.json must be a non-empty JSON object')
        validation = self.result_data.get('validation') or {}
        status = self.result_data.get('status')
        validation_status = validation.get('status')
        outcome = 'fail' if validation_status in ('fail', 'failed') or status in ('fail', 'failed') else 'pass' if status == 'verified' and validation_status in ('verified', 'pass', 'good') else 'incomplete'
        validation_source = 'simulated' if validation.get('simulated') is True else 'real' if validation.get('simulated') is False else 'unknown'
        analyses = self.result_data.get('response') or {}
        if isinstance(analyses, str):
            analyses = json.loads(analyses)
        analysis_by_rule = {x['rule']: x for x in analyses.get('violations_analysis', []) if isinstance(x, dict) and x.get('rule')} if isinstance(analyses, dict) else {}
        rules = self.drc_data.get('rules') or [{'name': key, 'description': ''} for key in self.drc_data.get('violations_by_rule', {})]
        fingerprint = hashlib.sha256(json.dumps({'path': str(self.rundir), 'result': self.result_data, 'conversation': [m for m in self.conversation_data if isinstance(m, dict) and m.get('role') in ('assistant', 'tool')]}, sort_keys=True).encode()).hexdigest()
        self.extracted_records = []
        for rule in rules:
            name = rule.get('name')
            if not name:
                continue
            description = rule.get('description', '')
            if isinstance(description, list):
                description = ' '.join(description)
            analysis = analysis_by_rule.get(name, {})
            record = {'id': f'case:{fingerprint}:{name}', 'rule': name, 'content': json.dumps({'description': description, 'analysis': analysis}, ensure_ascii=False), 'outcome': outcome, 'validation_source': validation_source, 'source': str(self.rundir), 'validation': validation, 'review': self.review.evidence}
            self.extracted_records.append(record)
        self.extracted_records.append({'id': f'run:{fingerprint}', 'kind': 'run_context', 'rule': '', 'content': json.dumps({'rules': [r.get('name') for r in rules], 'conversation': [m for m in self.conversation_data if isinstance(m, dict) and m.get('role') in ('assistant', 'tool')]}, ensure_ascii=False), 'outcome': outcome, 'validation_source': validation_source, 'source': str(self.rundir), 'validation': validation, 'review': self.review.evidence})
        self.rag = HybridRetriever(self.knowledge_dir, self.config).run()
        self.stored_ids = self.rag.index_records(self.extracted_records)
        self.knowledge_dir = self.rag.knowledge_dir
        self.learned = True
        return self

    def __init__(self, rundir=None, knowledge_dir=None, config=None, client=None, verbose=True):
        self.rundir = Path(rundir).resolve() if rundir else None
        self.knowledge_dir = Path(knowledge_dir).resolve() if knowledge_dir else None
        self.config = config or {}
        self.client = client
        self.verbose = verbose
        self.learned = False
        self.extracted_records = []
        self.stored_ids = []
        self.rag = None

    def read_json(self, name):
        path = self.rundir / name
        if not path.is_file():
            raise FileNotFoundError(f'Missing required file: {path}')
        return json.loads(path.read_text())
