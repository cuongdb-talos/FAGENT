"""Module FAGENT.rag.retriever: Bộ truy xuất tri thức kết hợp (Hybrid Knowledge Retriever).

Trách nhiệm & Chức năng:
- Cung cấp lớp `HybridRetriever` kết hợp đa tầng phương thức tìm kiếm tri thức sửa lỗi DRC:
  1. Khớp chính xác tên DRC rule (Exact rule match).
  2. Tìm kiếm theo từ khóa lexical (Keyword/token frequency ranking).
  3. Tìm kiếm ngữ nghĩa vector (Semantic cosine similarity ranking) qua mô hình embedding cục bộ.
- Lọc bảo mật: Chỉ cho phép truy xuất các tài liệu tham khảo gốc hoặc các case sửa lỗi đã có bằng chứng Human Review hợp lệ.
"""
import json
import math
import re
import sqlite3
from pathlib import Path
from .storage import KnowledgeStore
from .indexing import KnowledgeIndexer
from ..integrations.models.llama_adapter import LlamaAdapter


KNOWLEDGE_ROOT = Path(__file__).resolve().parent.parent / 'knowledge'


class HybridRetriever(KnowledgeStore, KnowledgeIndexer):
    def run(self):
        if KNOWLEDGE_ROOT.is_symlink():
            raise ValueError('Knowledge root must not be a symlink')
        root = KNOWLEDGE_ROOT.resolve()
        self.knowledge_dir = Path(self.knowledge_dir or root).resolve()
        if not self.knowledge_dir.is_relative_to(root):
            raise ValueError('Knowledge directory must be inside FAGENT/knowledge')
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.knowledge_dir / 'rag.sqlite3'
        if self.db_path.is_symlink() or (self.db_path.exists() and self.db_path.stat().st_nlink > 1) or not self.db_path.resolve().is_relative_to(root):
            raise ValueError('Knowledge database must be inside FAGENT/knowledge')
        self.records_path = self.knowledge_dir / 'records.json'
        if self.records_path.is_symlink() or (self.records_path.exists() and self.records_path.stat().st_nlink > 1) or not self.records_path.resolve().is_relative_to(root):
            raise ValueError('Knowledge records must be inside FAGENT/knowledge')
        new_index = not self.db_path.exists()
        with sqlite3.connect(self.db_path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
            if new_index and self.records_path.exists():
                records = json.loads(self.records_path.read_text())
                db.executemany('INSERT INTO records (id, data) VALUES (?, ?)', [(r['id'], json.dumps(r)) for r in records])
        return self

    def __init__(self, knowledge_dir=None, config=None, embedding_client=None):
        self.knowledge_dir = knowledge_dir
        self.config = config or {}
        self.embedding_client = embedding_client
        self.db_path = None
        self.indexed_ids = []
        self.results = []






    def embed(self, text):
        return self.embed_batch([text], 'query')[0]

    def embed_batch(self, texts, purpose):
        prefix = self.config.get(f'embedding_{purpose}_prefix', '')
        inputs = [prefix + text for text in texts]
        if self.embedding_client is None:
            
            self.embedding_client = LlamaAdapter(self.config)
        if hasattr(self.embedding_client, 'chat'):
            vectors = self.embedding_client.embed_batch(inputs)
        elif hasattr(self.embedding_client, 'embed_batch'):
            vectors = self.embedding_client.embed_batch(inputs, self.config.get('embedding_model', ''))
        else:
            vectors = [self.embedding_client.embed(text, self.config.get('embedding_model', '')) for text in inputs]
        if len(vectors) != len(inputs) or not vectors:
            raise ValueError('Embedding response count does not match input count')
        dimension = len(vectors[0])
        if not dimension or any(len(v) != dimension or any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in v) for v in vectors):
            raise ValueError('Embedding response contains invalid vectors')
        return vectors

    def retrieve(self, query, rule_names, limit):
        records = [record for record in self.records() if self.eligible(record)]
        terms = set(re.findall(r'[\w.]+', query.lower()))
        exact = set(rule_names)
        model = self.config.get('embedding_model_path') or self.config.get('embedding_model')
        if not model:
            raise ValueError('embedding_model_path is required in config')
        identity = self.embedding_identity(model)
        query_vector = self.embed(query)
        missing = [r for r in records if r.get('_embedding_identity') != identity or not isinstance(r.get('_vector'), list) or len(r['_vector']) != len(query_vector)]
        batch_size = max(1, int(self.config.get('embedding_batch_size', 16)))
        for start in range(0, len(missing), batch_size):
            batch = missing[start:start + batch_size]
            vectors = self.embed_batch([str(r.get('content', '')) for r in batch], 'document')
            if any(len(v) != len(query_vector) for v in vectors):
                raise ValueError('Embedding dimensions do not match query')
            for record, vector in zip(batch, vectors):
                record.update(_vector=vector, _embedding_model=model, _embedding_identity=identity)
            with sqlite3.connect(self.db_path) as db:
                db.executemany('UPDATE records SET data = ? WHERE id = ?', [(json.dumps(r), r['id']) for r in batch])
        ranked = []
        for record in records:
            content = str(record.get('content', ''))
            words = set(re.findall(r'[\w.]+', content.lower()))
            lexical = len(terms & words) / max(len(terms), 1)
            vector = record.get('_vector')
            if record.get('_embedding_identity') != identity or not isinstance(vector, list) or len(vector) != len(query_vector):
                raise ValueError(f"Record {record.get('id')} is missing a valid embedding vector")
            norm = math.sqrt(sum(x * x for x in vector) * sum(x * x for x in query_vector))
            semantic = sum(x * y for x, y in zip(vector, query_vector)) / norm if norm else 0.0
            match = record.get('rule') in exact
            score = lexical + max(semantic, 0.0)
            if match or score > 0:
                item = {k: v for k, v in record.items() if not k.startswith('_')}
                item.update(score=score, retrieval_mode='hybrid')
                ranked.append((match, score, item))
        ranked.sort(key=lambda row: (-int(row[0]), -row[1], row[2]['id']))
        self.results = [row[2] for row in ranked[:max(0, limit)]]
        return self.results

    def eligible(self, record):
        if record.get('kind') == 'reference':
            return True
        review = record.get('review')
        if not isinstance(review, dict):
            return False
        if review.get('consent_source') == 'human_review':
            return review.get('storage_consent') is True and review.get('authorized') is True
        return (review.get('completed') is True and review.get('save_requested') is True
                and bool(review.get('feedback')))

    def embedding_identity(self, model):
        if self.config.get('embedding_model_path'):
            path = Path(model).expanduser().resolve(strict=True)
            stat = path.stat()
            revision = [str(path), stat.st_size, stat.st_mtime_ns]
        elif self.embedding_client:
            revision = ['injected-client', model]
        else:
            raise ValueError('embedding_model_path is required')
        return json.dumps(['llama-cpp-v1', revision,
                           self.config.get('embedding_query_prefix', ''),
                           self.config.get('embedding_document_prefix', '')])
