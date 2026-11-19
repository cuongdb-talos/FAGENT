"""Module FAGENT.models.chat_model: Giao diện Chat Model cục bộ cho FAGENT.

Trách nhiệm & Chức năng:
- Cung cấp lớp `LocalChatModel` đóng gói giao diện gửi/nhận tin nhắn và gọi công cụ (tool calling)
  dựa trên LlamaAdapter, tạo lớp trừu tượng hóa cho việc tương tác LLM.
"""
from .llama_adapter import LlamaAdapter


class LocalChatModel:
    def run(self):
        return self

    def __init__(self, config, client=None):
        self.client = client or LlamaAdapter(config)
        self.message = None

    def chat(self, messages, tools):
        self.message = self.client.chat(messages, tools)
        return self.message
