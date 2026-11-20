# OpenAI-Compatible Chat Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable FAGENT chat through an OpenAI-compatible API while retaining local llama.cpp embeddings and the existing local chat backend.

**Architecture:** Select a chat adapter in GraphServices; inject a separate local LlamaAdapter into HybridRetriever. Reuse the existing schema composition and response validation so both backends produce the same agent messages; never silently fall back to unconstrained results.

**Tech Stack:** Python, openai Python SDK, llama-cpp-python, jsonschema, pytest.

**Spec:** User conversation of 2026-11-20: OpenAI-compatible chat using Token Lab, local embeddings; no separate specification document.

## Global Constraints

- Read the API key from the environment variable named by `api_key_env`; never log or persist it.
- Preserve current local chat behavior when `chat_backend` is absent or `llama`.
- Model names, endpoint, and local embedding paths come from configuration.
- Main/run methods precede other class methods; no nested function definitions.
- Do not assume Token Lab supports JSON Schema merely because basic chat works.

## Review Focus

- Missing or empty API key: fail clearly before making a request (Task 1).
- API authentication, network, and unsupported schema errors: propagate without exposing credentials or falling back (Task 1).
- Truncated, malformed, or schema-invalid model response: reject it (Task 1).
- Tool selection and final JSON: preserve the existing internal message contract (Task 1).
- Local versus API selection and embedding injection, including resume: preserve local behavior (Task 2).

## File Structure

- Modify `requirements.txt`: add OpenAI SDK dependency.
- Modify `FAGENT/integrations/models/llama_adapter.py`: extract reusable schema and response normalization without changing local behavior.
- Create `FAGENT/integrations/models/openai_adapter.py`: API chat adapter.
- Modify `FAGENT/engine/graph_services.py`: backend selection and independent embedding client.
- Modify `FAGENT/config_api.json` and `FAGENT/config.example.json`: document selectable backend and embedding configuration.
- Create `tests/test_openai_adapter.py` and `tests/test_chat_backend.py`: adapter and integration boundaries.

## Example

Input configuration: `{"chat_backend":"openai", "base_url":"https://api.tokenlab.amd.com/v1", "agent_model_path":"DeepSeek-V4-Flash", "api_key_env":"TOKENLAB_API_KEY", "embedding_model_path":"<local-embedding.gguf>"}`. The actual embedding path must exist and be supplied by the environment.

A model response with `{"name":"read_drc","arguments":{}}` becomes an assistant message with a `tool_calls` entry for `read_drc`; a response matching the workflow final schema becomes an assistant message containing serialized JSON. The actual schema and tool names come from the workflow, not this example.

### Task 1: Implement OpenAI-compatible chat with existing response contract

**Files:** Modify `requirements.txt`, `FAGENT/integrations/models/llama_adapter.py`; create `FAGENT/integrations/models/openai_adapter.py`, `tests/test_openai_adapter.py`.

**Interfaces:** `OpenAIAdapter(config, client=None)` provides `chat(messages, tools, response_schema) -> dict`, stores `message` and `last_thinking`, and accepts an injected SDK client for tests. Extract reusable schema construction and response normalization from `LlamaAdapter` while preserving `LlamaAdapter.chat` behavior and signatures.

- [ ] Add tests with a fake SDK client asserting `chat.completions.create` receives the configured model, messages, and `response_format={'type': 'json_object', 'schema': schema}`. Assert valid final JSON and valid tool JSON return the same message shape as local chat, including an allocated tool-call ID and serialized arguments.
- [ ] Add tests for empty/missing environment key, API exceptions, `finish_reason='length'`, malformed JSON, schema-invalid JSON, and absent assistant content; assert errors propagate or raise `ValueError` and do not include the key. Assert optional reasoning is stored on `last_thinking`.
- [ ] Run `python -m pytest -q tests/test_openai_adapter.py`; expect failure because the adapter does not exist.
- [ ] Add the SDK dependency and implement the adapter. Instantiate `OpenAI(base_url=config['base_url'], api_key=os.environ[config['api_key_env']])` only when no client is injected. Convert SDK completion to a dictionary before using the shared response validator. Do not retry without schema if the provider rejects the request.
- [ ] Run `python -m pytest -q tests/test_openai_adapter.py` and the existing llama adapter tests discovered with `rg -l 'LlamaAdapter|llama_adapter' tests`; expect all selected tests to pass. If no existing adapter tests are found, add a local-adapter regression test for unchanged schema and message conversion.

### Task 2: Route chat and embedding independently

**Files:** Modify `FAGENT/engine/graph_services.py`, `FAGENT/config_api.json`, `FAGENT/config.example.json`; create `tests/test_chat_backend.py`.

**Interfaces:** `GraphServices.client` remains the chat client. Add `GraphServices.embedding_client`, a local `LlamaAdapter`; pass it as `embedding_client` to `HybridRetriever`. Continue passing the chat client to `CaseLearner`. Select `openai` only for `chat_backend='openai'`, `LlamaAdapter` for missing or `llama`, and reject unknown values.

- [ ] Write tests using injected or patched adapter constructors: absent and `llama` select local chat; `openai` selects API chat and a distinct local embedding adapter; an unknown backend raises `ValueError`. Cover both layout and non-layout workflows and resume. Verify retrieval receives the embedding client, while `chat` and feedback use the chat client.
- [ ] Run `python -m pytest -q tests/test_chat_backend.py`; expect failure on API selection or embedding injection.
- [ ] Implement the routing in `GraphServices.prepare(resume=False)`. Keep API model ID in `agent_model_path` and local embedding model in `embedding_model_path`. Do not instantiate an API client for embedding.
- [ ] Update the example configuration with `chat_backend`; update API config with `chat_backend: openai` and a clearly replaceable local embedding path, without any secret. Preserve existing local config compatibility.
- [ ] Run `python -m pytest -q tests/test_chat_backend.py tests/test_openai_adapter.py`; expect all tests to pass. Run the relevant existing graph, RAG and agent tests identified by `rg -l 'GraphServices|HybridRetriever|CaseLearner' tests`; expect all selected tests to pass.

### Task 3: Verify the real boundary

**Files:** No additional code required unless verification exposes a defect.

**Interfaces:** CLI configuration is supplied with `--config FAGENT/config_api.json`; actual task and run arguments must follow `python -m FAGENT.fagent --help` and the chosen workflow's existing requirements.

- [ ] Run `python -m pytest -q tests/test_openai_adapter.py tests/test_chat_backend.py` and `git diff --check`; expect exit status 0 for both. Run any project-required CAD/EDA checks applicable to the changed flow.
- [ ] With authorized credentials and an existing configured embedding model, run the chosen workflow through the CLI. Capture exit status and logs without recording the key; verify an actual API response passes schema validation, produces a valid agent message, and RAG uses local embeddings when the workflow invokes it. Report the command, workflow, observed artifacts and outcome. Do not claim a real API or EDA run passed if credentials, network, or tool setup are unavailable.
- [ ] If the provider rejects the schema format, report its actual error and stop: basic chat support alone does not establish structured-output support. Decide any alternative protocol separately before implementation.

## Self-review

The tasks cover backend selection, shared response contract, local embeddings, configuration, error handling and actual-provider verification. The conversation is the source specification; schema support remains a real-provider acceptance condition.
