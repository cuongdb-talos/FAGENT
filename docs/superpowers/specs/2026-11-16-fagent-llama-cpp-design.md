# FAGENT llama.cpp integration design

## Goal and scope

Run both FAGENT agent inference and RAG embeddings locally through the `llama-cpp-python` `Llama` API, without HTTP or Ollama. Preserve the existing tool-calling loop, DRC validation, human review, and RAG retrieval behavior. Remove the unused `FAGENT/AGENT/agy_adapter.py` and update relevant configuration, dependencies, documentation, and tests.

## Architecture

Add a small llama.cpp adapter that owns model initialization and translates between the existing caller and RAG interfaces and `Llama` operations. The caller retains responsibility for conversation state and executing tools; RAG retains responsibility for indexing, retrieval, caching, and fallback. The adapter creates separate inference and embedding model instances on demand and reuses each instance for the session.

The agent uses `Llama.create_chat_completion` with the conversation and existing tool schemas. The adapter normalizes the returned assistant message to the dictionary shape consumed by the current loop, preserving tool-call IDs, names, and arguments. Configure a chat format appropriate to the model, and validate that the selected model and installed binding support tool calls; report unsupported or malformed responses clearly rather than silently discarding calls.

The embedding adapter uses the binding's embedding API with embedding mode enabled, processes inputs in configured batches, and returns vectors in input order. It verifies the result count and vector shape before RAG caches or uses the vectors.

## Configuration

Replace Ollama endpoint, API key, and model-name settings with configurable GGUF paths and llama.cpp runtime options. Initial paths are:

- Agent: `/mnt/d74b9a13-9fbd-4687-8385-e72dd5e811b1/AI_BUNDLE/models/main_agent/qwen2.5-coder:7b.gguf`
- Embedding: `/mnt/d74b9a13-9fbd-4687-8385-e72dd5e811b1/AI_BUNDLE/models/embeddings/bge-m3-q8_0.gguf`

Expose context size, thread count, GPU layers, and agent chat format as appropriate for each model, plus the existing embedding batch size. Paths and resource settings remain configurable for other machines. Document installation of `llama-cpp-python` and any model-specific runtime requirements. No server or network endpoint is required.

## Cache and failure behavior

Namespace embedding cache entries by the local model's identity and the existing query/document prefix settings so Ollama vectors are not reused with BGE-M3. Changes to the model at a configured path must invalidate its cached vectors; use file identity metadata or a content fingerprint and document the chosen trade-off. Never mix vectors with incompatible dimensions.

Failure to load the agent model or obtain a valid agent response produces a clear failure through the existing incomplete/review flow; it must not fall back to HTTP. Embedding load or generation failure preserves the existing lexical and exact-rule retrieval fallback and must not leave partial or invalid vectors in cache. Model paths may be unavailable in development environments; tests must not depend on their presence.

## Verification

Use mocked `Llama` instances to test configuration, initialization and reuse, ordinary replies, tool-call round trips, malformed responses, batch ordering, cache identity and invalidation, and embedding failure fallback. Run relevant existing tests for the agent, RAG, DRC, and human review, updating HTTP-specific tests for the new interface. Verify that the application no longer makes HTTP requests for inference or embeddings and that obsolete AGY integration is removed. If the configured GGUF files and compatible runtime are accessible, perform a real-model smoke test covering agent tool calls and embedding generation; otherwise report this verification as outstanding.

## Constraints and assumptions

The supplied GGUF files are assumed to be available on the target machine, but were not accessible in the design environment. Compatibility of Qwen tool calling, BGE-M3 embedding output, context sizes, and available compute must be validated on that machine. This change does not alter the review authorization or claim simulated DRC results are verified.
