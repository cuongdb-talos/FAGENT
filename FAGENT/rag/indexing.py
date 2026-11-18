"""Ingest reference documents and rule decks."""
import hashlib
import json
import re
from pathlib import Path


class KnowledgeIndexer:
    def run(self):
        return self

    def knowledge_root(self):
        from .retriever import KNOWLEDGE_ROOT
        return KNOWLEDGE_ROOT

    def migrate_legacy(self, path):
        source = Path(path)
        if not source.is_file():
            return 0
        data = json.loads(source.read_text())
        records = []
        for name, item in data.get('rules', {}).items():
            records.append({'id': 'legacy:' + name, 'rule': name, 'content': json.dumps(item, ensure_ascii=False),
                            'outcome': 'unknown', 'validation_source': 'unknown',
                            'source': str(source), 'legacy': item})
        self.index_records(records)
        self.migrated_count = len(records)
        return self.migrated_count

    def index_documents(self, directory):
        directory = Path(directory).resolve()
        if not directory.is_relative_to(self.knowledge_root().resolve()):
            raise ValueError('Reference documents must be inside FAGENT/knowledge')
        if not directory.is_dir():
            raise FileNotFoundError(f'Reference directory not found: {directory}')
        records = []
        for path in sorted(directory.rglob('*.md')):
            if not path.resolve().is_relative_to(directory):
                raise ValueError('Reference document escapes knowledge directory')
            chunks = re.split(r'(?m)(?=^#{1,6}\s)', path.read_text())
            for index, chunk in enumerate(chunks):
                if chunk.strip():
                    identity = hashlib.sha256(str(path.relative_to(directory)).encode()).hexdigest()
                    records.append({'id': f'doc:{identity}:{index}', 'kind': 'reference', 'rule': '', 'content': chunk.strip(), 'outcome': 'reference', 'validation_source': 'unknown', 'source': str(path)})
        self.document_ids = self.index_records(records)
        return self.document_ids

    def index_rule_decks(self, paths):
        if isinstance(paths, (str, Path)):
            paths = [paths]
        records = []
        for source in paths:
            path = Path(source).resolve()
            if not path.is_file():
                raise FileNotFoundError(f'Rule deck not found: {path}')
            lines = path.read_text(errors='replace').splitlines(keepends=True)
            name = None
            block = []
            depth = 0
            for line in lines:
                if name is None:
                    match = re.match(r'^([A-Za-z_][\w.:-]*)\s*\{', line)
                    if not match:
                        continue
                    name = match.group(1)
                block.append(line)
                depth += line.count('{') - line.count('}')
                if depth == 0:
                    identity = hashlib.sha256(f'{path}:{name}'.encode()).hexdigest()
                    records.append({'id': f'deck:{identity}', 'kind': 'reference', 'rule': name,
                                    'content': ''.join(block).strip(), 'outcome': 'reference',
                                    'validation_source': 'unknown', 'source': str(path)})
                    name, block = None, []
        self.rule_deck_ids = self.index_records(records)
        return self.rule_deck_ids

