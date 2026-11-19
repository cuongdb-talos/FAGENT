"""Module FAGENT.models.embeddings: Giao diện Embeddings tương thích chuẩn LangChain.

Trách nhiệm & Chức năng:
- Cung cấp lớp `LocalEmbeddings` kế thừa `langchain_core.embeddings.Embeddings`.
- Triển khai hai phương thức cốt lõi: `embed_documents()` (nhúng danh sách tài liệu) và `embed_query()` (nhúng câu truy vấn)
  sử dụng mô hình embedding chạy cục bộ trên llama.cpp.
"""
from langchain_core.embeddings import Embeddings
from .llama_adapter import LlamaAdapter


class LocalEmbeddings(Embeddings):
    def run(self):
        return self

    def __init__(self, config, client=None):
        self.config = config
        self.client = client or LlamaAdapter(config)
        self.vectors = []

    def embed_documents(self, texts):
        prefix = self.config.get('embedding_document_prefix', '')
        self.vectors = self.client.embed_batch([prefix + text for text in texts])
        return self.vectors

    def embed_query(self, text):
        prefix = self.config.get('embedding_query_prefix', '')
        self.vectors = self.client.embed_batch([prefix + text])
        return self.vectors[0]
