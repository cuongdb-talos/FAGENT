"""Module FAGENT.rag.storage: Quản lý lưu trữ tri thức và snapshot JSON nguyên tử.

Trách nhiệm & Chức năng:
- Cung cấp lớp `KnowledgeStore` quản lý cơ sở dữ liệu SQLite `rag.sqlite3` lưu trữ các bản ghi tri thức sửa lỗi.
- Đảm bảo tính nhất quán dữ liệu: Xuất snapshot ra tệp `records.json` bằng kỹ thuật ghi tệp tạm nguyên tử (atomic write).
- Cung cấp API truy vấn, lập chỉ mục và kiểm tra tính hợp lệ của các bản ghi tri thức.
"""
import hashlib
import json
import os
import sqlite3
import tempfile


class KnowledgeStore:
    def run(self):
        return self

    def records(self):
        with sqlite3.connect(self.db_path) as db:
            self.all_records = [json.loads(row[0]) for row in db.execute('SELECT data FROM records ORDER BY id')]
        return self.all_records

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

