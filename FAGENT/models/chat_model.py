"""Local chat boundary retaining the existing JSON-call adapter."""
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
