# FAGENT llama.cpp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run agent inference and RAG embeddings locally via `llama_cpp.Llama` without HTTP.

**Architecture:** A shared adapter lazily owns one agent model and one embedding model per FAGENT run. Caller retains its tool loop; RAG retains retrieval and cache ownership. Configurable GGUF paths and runtime options replace Ollama settings.

**Tech Stack:** Python, `llama-cpp-python`, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-fagent-llama-cpp-design.md`

## Global Constraints

- Preserve DRC validation, human review, and lexical/exact-rule fallback; never label simulated validation verified.
- No HTTP for inference, embeddings, or embedding model identity; remove `FAGENT/AGENT/agy_adapter.py`.
- Main `run` method comes first in each class; avoid nested functions and hard-coded project-specific values except the user's initial configurable paths.
- Keep existing unrelated workspace changes intact; stage only explicitly selected task files after checking their diffs, and do not commit unrelated pre-existing edits.
- Locate the approved design section with `grep -n '^## ' docs/superpowers/specs/2026-11-16-fagent-llama-cpp-design.md`.

## Review Focus

- An absent or unreadable agent GGUF must enter the existing incomplete/review flow, not abort before result artifacts are written (Task 2).
- Empty, malformed, non-finite, or dimension-inconsistent embedding batches must never be cached (Tasks 1 and 3).
- Replacing a GGUF at the same path or changing a prefix must invalidate vectors (Task 3).
- A malformed tool call must be rejected without executing any tool (Tasks 1 and 2).
- Real GGUF files or compatible tool-call chat format may be unavailable; report smoke-test status honestly (Task 4).

## File structure

- Create `FAGENT/AGENT/llama_adapter.py`: lazy model ownership, chat and embedding API normalization.
- Modify `FAGENT/AGENT/caller.py`: invoke adapter, preserve messages, review and tool processing.
- Modify `FAGENT/RAG/rag.py`: use adapter embeddings and local model cache identity.
- Modify `FAGENT/config.json`, `requirements.txt`, `README.md`, `.agent/AI_Workflow.md`, and `scripts/benchmark_fagent_embedding.py`: configure and describe the local flow.
- Delete `FAGENT/AGENT/agy_adapter.py`; update `tests/test_agent.py`, `tests/test_rag.py`, `tests/test_review.py`; create `tests/test_llama_adapter.py`.

### Task 1: Local model adapter

**Files:** Create `FAGENT/AGENT/llama_adapter.py`, `tests/test_llama_adapter.py`; modify `requirements.txt`.

**Interfaces:** `LlamaAdapter(config: dict)` provides `chat(messages: list, tools: list) -> dict` and `embed_batch(texts: list[str]) -> list[list[float]]`. Both lazily initialize and reuse their separate `Llama` objects. Import the binding lazily so tests and `--load-only` do not require a loaded GGUF. Use an injectable or monkeypatchable model factory.

- [ ] Write adapter tests using a fake `Llama`: construct twice via repeated calls and assert one constructor per model; check configured `model_path`, `n_ctx`, `n_threads`, `n_gpu_layers`, agent `chat_format`, and `embedding=True` only for embeddings. Assert a normal message and tool-call `id`, `function.name`, and JSON argument string survive unchanged. Example input `{'role': 'user', 'content': 'repair'}` produces `{'role': 'assistant', 'content': 'done'}`; example tool call `{'id': 'c1', 'type': 'function', 'function': {'name': 'check', 'arguments': '{}'}}` retains those fields.
- [ ] Add tests asserting missing choices/message, invalid tool call ID/name/arguments, mismatched embedding count/order, empty vectors, non-numeric or non-finite values, and differing dimensions raise descriptive errors. Fake indexed embedding data returned out of order must produce vectors in input order; duplicate or missing indices must fail.
- [ ] Run `python -m pytest -q tests/test_llama_adapter.py`; expect failure because adapter is absent.
- [ ] Implement the interface using `create_chat_completion(messages=..., tools=...)` and `create_embedding(input=...)`; validate full responses before returning. Pass resource settings from the per-model configuration without inventing machine-specific settings. Add `llama-cpp-python` dependency and remove OpenAI only after confirming no remaining FAGENT imports need it.
- [ ] Run `python -m pytest -q tests/test_llama_adapter.py`; expect all tests pass.

### Task 2: Agent integration and configuration

**Files:** Modify `FAGENT/AGENT/caller.py`, `FAGENT/config.json`, `tests/test_agent.py`, `tests/test_review.py`; delete `FAGENT/AGENT/agy_adapter.py`.

**Interfaces:** Consume `LlamaAdapter.chat(messages, tools)` from Task 1. Construct one adapter per `Agent_caller.run`, pass the same instance to RAG, and leave tool results as `{'role': 'tool', 'tool_call_id': id, 'content': JSON text}`.

- [ ] Replace HTTP mock round-trip tests with a fake adapter response sequence: assistant tool call, tool response, final assistant text. Assert tool ID and JSON arguments are preserved, the expected tool runs exactly once, and conversation/result artifacts retain existing behavior. Add tests for malformed calls and an agent model load failure, asserting incomplete result and artifacts with no tool execution or verified claim. Remove tests exclusively exercising the deleted AGY adapter.
- [ ] Run `python -m pytest -q tests/test_agent.py tests/test_review.py`; expect new integration tests to fail before implementation.
- [ ] Replace the OpenAI client with the adapter; move lazy model errors inside the existing request exception/review path. Keep review and iteration semantics intact. Remove the AGY adapter and obsolete provider/HTTP code. Set `agent_model_path` to `/mnt/d74b9a13-9fbd-4687-8385-e72dd5e811b1/AI_BUNDLE/models/main_agent/qwen2.5-coder:7b.gguf` and `embedding_model_path` to `/mnt/d74b9a13-9fbd-4687-8385-e72dd5e811b1/AI_BUNDLE/models/embeddings/bge-m3-q8_0.gguf`; expose separate `agent_n_ctx`, `agent_n_threads`, `agent_n_gpu_layers`, `agent_chat_format`, `embedding_n_ctx`, `embedding_n_threads`, and `embedding_n_gpu_layers`. Use `null` for thread count to permit binding defaults and zero GPU layers by default; select and document the chat format supported by the installed binding for Qwen2.5 tool calling. Retain existing batch and retrieval settings.
- [ ] Run `python -m pytest -q tests/test_agent.py tests/test_review.py`; resolve regressions, preserving existing checks unrelated to provider behavior.

### Task 3: Local embeddings and cache identity

**Files:** Modify `FAGENT/RAG/rag.py`, `tests/test_rag.py`, `tests/test_review.py`.

**Interfaces:** Consume `LlamaAdapter.embed_batch(texts)` from Task 1; `Agent_rag(..., embedding_client=adapter)` reuses caller's adapter. Standalone RAG constructs one adapter lazily when no client is injected. Keep `embed`, `embed_batch`, and `retrieve` public behavior and query/document prefixes.

- [ ] Add tests with a fake adapter and temporary GGUF: query `metal spacing` and document `metal spacing rule` generate vectors in input order, repeated retrieval embeds only the query, and cache records use the new identity. Replace the model at the same path with changed size or `mtime_ns`, and separately change each prefix; assert document vectors are regenerated. A missing GGUF, mixed dimensions, partial batch, NaN, and adapter exception each leave invalid vectors uncached and return lexical/exact matches. Seed an old Ollama identity and assert it is never reused.
- [ ] Run `python -m pytest -q tests/test_rag.py tests/test_review.py`; expect the new identity and adapter tests to fail before implementation.
- [ ] Replace Ollama client and `/api/tags` lookup with adapter calls. Build a deterministic cache identity from resolved model path, file size, `mtime_ns`, and both prefixes, with a llama.cpp version namespace; explain that metadata avoids hashing large models but cannot detect replacement preserving both metadata values. If `stat` fails, perform retrieval through lexical fallback rather than trusting prior vectors. Preserve transactional batch writes and reject dimension mismatches.
- [ ] Run `python -m pytest -q tests/test_llama_adapter.py tests/test_agent.py tests/test_rag.py tests/test_review.py`; expect all pass.

### Task 4: Documentation, benchmark, and end-to-end verification

**Files:** Modify `README.md`, `.agent/AI_Workflow.md`, `scripts/benchmark_fagent_embedding.py`, and any directly affected test fixtures.

**Interfaces:** Benchmark takes local GGUF model paths and runtime settings rather than an endpoint; output identifies model, batch size, elapsed time, recall, and peak RSS including the local model process. Existing corpus JSON stays unchanged.

- [ ] Add or update benchmark tests to assert local model paths and batch size reach RAG, no endpoint is required, and unavailable model is reported as an error rather than a successful benchmark. Run the targeted test and observe failure before changing the script.
- [ ] Update benchmark and user documentation for installing the binding, selecting compatible Qwen tool-call format, configuring CPU/GPU resources and GGUF paths, local cache identity limitations, failure fallback, and the existing `python -m FAGENT` entry point. Remove obsolete Ollama/AGY guidance and HTTP tests only where superseded.
- [ ] Run `python -m pytest -q tests/test_llama_adapter.py tests/test_agent.py tests/test_rag.py tests/test_review.py tests/test_structure_setup.py` and relevant benchmark tests; expect exit 0. Run `rg -n -i 'openai|ollama|https?://|agy_adapter' FAGENT scripts/benchmark_fagent_embedding.py requirements.txt` and investigate any hits; no inference, embedding, or model identity HTTP path may remain.
- [ ] If both configured files and `llama_cpp` are available, run a bounded smoke test that instantiates each configured model, produces a finite BGE-M3 vector, and performs a Qwen tool-call round trip with a harmless fake tool; record commands, output and status. Otherwise record that real-model verification remains outstanding, including the missing prerequisite. Do not represent mock tests as real-model validation.
- [ ] Review the final diff and run applicable CAD/EDA verification skill checks; commit only task-owned changes that are separable from pre-existing user modifications, or report any files that cannot safely be committed separately.
