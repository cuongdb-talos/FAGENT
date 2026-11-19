"""SQLite persistence and atomic JSON snapshot writer for RAG records."""

# ==============================================================================
# SECTION 1: Standard Library Imports
# ==============================================================================
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile


# ==============================================================================
# SECTION 2: Knowledge Storage and Record Persistence Class
# ==============================================================================
class KnowledgeStore:
    """Manages SQLite storage and atomic JSON synchronization for RAG knowledge records."""

    # --------------------------------------------------------------------------
    # Main Setup and Table Initialization
    # --------------------------------------------------------------------------
    def run(self):
        if self.knowledge_dir is None:
            raise ValueError('knowledge_dir must be provided')
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.knowledge_dir / 'rag.sqlite3'
        self.records_path = self.knowledge_dir / 'records.json'
        new_index = not self.db_path.exists()
        with sqlite3.connect(self.db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            if new_index and self.records_path.exists():
                records = json.loads(self.records_path.read_text())
                db.executemany('INSERT INTO records (id, data) VALUES (?, ?)', [(r['id'], json.dumps(r)) for r in records])
        self.records()
        return self

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, knowledge_dir=None):
        self.knowledge_dir = Path(knowledge_dir).resolve() if knowledge_dir else None
        self.db_path = self.knowledge_dir / 'rag.sqlite3' if self.knowledge_dir else None
        self.records_path = self.knowledge_dir / 'records.json' if self.knowledge_dir else None
        self.all_records = []
        self.indexed_ids = []

    # --------------------------------------------------------------------------
    # Record Retrieval Operations
    # --------------------------------------------------------------------------
    def records(self):
        with sqlite3.connect(self.db_path) as db:
            self.all_records = [json.loads(row[0]) for row in db.execute('SELECT data FROM records ORDER BY id')]
        return self.all_records

    # --------------------------------------------------------------------------
    # Atomic Record Indexing and Persistence
    # --------------------------------------------------------------------------
    def index_records(self, records):
        self.indexed_ids = []
        existing = {record['id']: record for record in self.records()}
        for item in records:
            record = dict(item)
            record_id = record.get('id') or hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
            record['id'] = record_id
            previous = existing.get(record_id, {})
            if previous.get('content') == record.get('content') and '_vector' in previous:
                record['_vector'] = previous['_vector']
                record['_embedding_model'] = previous.get('_embedding_model')
                record['_embedding_identity'] = previous.get('_embedding_identity')
            existing[record_id] = record
            self.indexed_ids.append(record_id)
        authoritative = [{key: value for key, value in record.items() if not key.startswith('_')} for record in existing.values()]
        fd, name = tempfile.mkstemp(dir=self.knowledge_dir, prefix='.records-', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(authoritative, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.records_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        with sqlite3.connect(self.db_path) as db:
            db.executemany('INSERT OR REPLACE INTO records (id, data) VALUES (?, ?)', [(key, json.dumps(value)) for key, value in existing.items()])
        return self.indexed_ids
