"""Local llama.cpp integration for chat and embeddings."""

# ==============================================================================
# SECTION 1: Model Integration Exports
# ==============================================================================
from .llama_adapter import LlamaAdapter

__all__ = ["LlamaAdapter"]
