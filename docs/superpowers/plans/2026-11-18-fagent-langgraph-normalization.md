# FAGENT LangGraph Normalization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox syntax for tracking.

**Goal:** Normalize FAGENT's modules and implement a checkpointed LangGraph workflow while preserving its CAD and knowledge contracts.

**Architecture:** Isolate layout, DRC, model and retrieval services from graph orchestration; expose the existing operations as typed tools. The graph carries serializable state and delegates side effects to injected services; the CLI resumes interrupted reviews.

**Tech Stack:** Python, LangGraph, LangChain Core, llama-cpp-python, gdstk, SQLite, pytest.

**Spec:** `docs/superpowers/specs/2026-11-18-fagent-langgraph-normalization-design.md`

## Global Constraints

- Preserve local in-process llama.cpp, existing JSON tool-call compatibility, CLI, artifact names, exact-rule-first hybrid retrieval, authorization and provenance.
- Preserve knowledge records and SQLite data; never recreate the removed legacy directories. Keep existing imports through compatibility shims.
- Simulated or absent DRC results never qualify as verified. Derive site-specific paths and values from input/configuration.
- Put `run()` first in classes; avoid nested function definitions. Store class outputs on `self` where applicable.
- Preserve the user's existing uncommitted edits. Do not commit or overwrite their changes without explicit authorization.
- Source document locator: `grep -nE '^## (Architecture|RAG and data|Validation and compatibility|Error handling and testing)' docs/superpowers/specs/2026-11-18-fagent-langgraph-normalization-design.md`.

## Review Focus

1. Empty or missing optional reference sources: context assembly succeeds and does not recreate directories (Task 2).
2. Embedding failure or changed model identity: lexical fallback works; stale vectors are not scored (Task 3).
3. Malformed or multiple model tool calls: preserve IDs, emit errors and continue safely (Tasks 4 and 5).
4. Review resume after interruption: no duplicate mutation or knowledge write (Task 6).
5. Missing, simulated or stale validation: never report a real DRC pass (Tasks 1 and 5).

## File Map

- `FAGENT/domain/`: `layout.py`, `gds_parser.py`, `drc.py` own CAD logic; `FAGENT/io/loaders.py` owns input preparation.
- `FAGENT/models/`: `chat_model.py`, `embeddings.py` own llama.cpp adapters.
- `FAGENT/rag/`: `indexing.py`, `storage.py`, `retriever.py`, `learning.py` own existing RAG behavior.
- `FAGENT/tools/`: `layout_tools.py`, `validation_tools.py` expose typed tool wrappers.
- `FAGENT/agent/`: `state.py`, `nodes.py`, `graph.py`, `prompts.py`, `review.py` own orchestration and review.
- `FAGENT/cli.py`, `FAGENT/__main__.py`, `FAGENT/config.json`, `requirements.txt`, `README.md` own entry, configuration and documentation.
- Existing `FAGENT/AGENT`, `FAGENT/RAG`, `FAGENT/TOOLS` modules remain compatibility shims; tests live under `tests/`.

### Task 1: Preserve CAD behavior and normalize domain imports

**Files:** Create `FAGENT/domain/{__init__,layout,gds_parser,drc}.py`, `FAGENT/io/{__init__,loaders}.py`; modify existing `FAGENT/TOOLS/*.py`; test `tests/test_agent.py`, `tests/test_structure_setup.py`.

**Interfaces:** `LayoutService.run(rundir=None, verbose=None)` preserves `Agent_functions` behavior; `InputLoader.run(gds_path=None, drc_path=None, output_dir=None, layer_map_files=None, verbose=None)` preserves loader behavior. Existing class names remain importable aliases.

- [ ] Add regression tests for source-overwrite rejection, non-finite polygon coordinates, and validation after a later polygon edit; assert simulated validation cannot become verified.
- [ ] Run `python -m pytest -q tests/test_agent.py tests/test_structure_setup.py`; establish the baseline and record pre-existing failures before moving code.
- [ ] Move parser, loader and domain logic with imports adjusted; retain wrappers for old import paths and preserve units (GDS user units and DRC database units).
- [ ] Run the same tests and `python -m pytest -q tests/test_review.py`; compare against baseline and fix regressions.

### Task 2: Normalize model configuration and prompts

**Files:** Create `FAGENT/models/{__init__,chat_model,embeddings}.py`, `FAGENT/agent/{__init__,prompts}.py`; modify `FAGENT/AGENT/llama_adapter.py`, `FAGENT/config.json`, `README.md`; test `tests/test_llama_adapter.py`, `tests/test_json_flow.py`, `tests/test_caller_context.py`.

**Interfaces:** `LocalChatModel.chat(messages: list, tools: list) -> dict`; `LocalEmbeddings.embed_documents(texts: list[str]) -> list[list[float]]` and `embed_query(text: str) -> list[float]`; `build_system_prompt(config: dict, retrieved: list[dict]) -> str`. Retain `LlamaAdapter` for old callers.

- [ ] Add tests for separate chat/embedding configuration, query/document prefixes, and absent legacy directories without creating them; assert retrieved text is labeled as reference data.
- [ ] Run `python -m pytest -q tests/test_llama_adapter.py tests/test_json_flow.py tests/test_caller_context.py` and record baseline.
- [ ] Extract adapters while preserving llama.cpp response validation and JSON call normalization; define explicit configurable prompt input and portable configuration defaults.
- [ ] Run the same tests; verify old `LlamaAdapter` imports and output shape remain valid.

### Task 3: Separate retrieval and storage

**Files:** Create `FAGENT/rag/{__init__,indexing,storage,retriever,learning}.py`; modify `FAGENT/RAG/{rag,learn}.py`; test `tests/test_rag.py` and `tests/test_review.py`.

**Interfaces:** `KnowledgeStore.records() -> list[dict]` and `index_records(records: list[dict]) -> list[str]`; `HybridRetriever.retrieve(query: str, rule_names: list[str], limit: int) -> list[dict]`; `CaseLearner.run(..., review=None)` retains existing inputs and outputs.

- [ ] Add regression tests asserting exact rule ranks before a higher-scoring non-exact match, embedding failure falls back to lexical retrieval, changed identity does not reuse stale vectors, and unauthorized records are excluded.
- [ ] Run `python -m pytest -q tests/test_rag.py tests/test_review.py` and record baseline.
- [ ] Extract storage, ingestion, retrieval and learning without changing IDs, existing file locations, metadata or transactional behavior; expose old classes through compatibility shims. Do not migrate or rewrite the user's existing knowledge data during development.
- [ ] Run the same tests using temporary knowledge directories and compare results with baseline.

### Task 4: Define typed tools and model boundary

**Files:** Create `FAGENT/tools/{__init__,layout_tools,validation_tools}.py`; modify `requirements.txt` and `FAGENT/models/chat_model.py`; test `tests/test_json_flow.py` and new `tests/test_fagent_tools.py`.

**Interfaces:** `build_layout_tools(service: LayoutService) -> list` returns `read_gds`, `replace_polygon`, `save_gds`; `build_validation_tools(service: LayoutService) -> list` returns `validate`. Tools use LangChain schemas and the existing operation names and input fields.

- [ ] Add tests for tool schema names and required fields, invalid geometry, unknown tool calls and stable tool call IDs; use fake local model responses.
- [ ] Run `python -m pytest -q tests/test_fagent_tools.py tests/test_json_flow.py`; confirm new tests fail for the missing wrappers.
- [ ] Add compatible LangChain Core and LangGraph dependency constraints after checking installed/version-compatible APIs; implement typed wrappers and model message conversion without requiring native GGUF tool calling.
- [ ] Re-run the test command and confirm success. Check dependency resolution in the actual execution environment and document any installation blocker.

### Task 5: Implement graph and deterministic state transitions

**Files:** Create `FAGENT/agent/{state,nodes,graph}.py`; modify `FAGENT/agent/prompts.py`; test new `tests/test_fagent_graph.py`.

**Interfaces:** `FagentState` is a serializable typed mapping with `messages`, `rundir`, `candidate_path`, `iteration`, `validation`, `result`, and `review`; `build_graph(services, checkpointer)` returns a compiled graph. Services include chat, retrieval, tool execution and persistence dependencies.

- [ ] Add fake-model tests for two tool calls in order with matching IDs, malformed arguments returned as tool errors, iteration limit, model failure, no validation, simulated validation, and real validation. Assert a polygon edit clears previous validation and review.
- [ ] Run `python -m pytest -q tests/test_fagent_graph.py`; confirm missing graph fails.
- [ ] Implement preparation, context, model, tool and result nodes with conditional routing; use raw serializable state and inject services outside it. Preserve conversation and result schemas. Distinguish actual DRC output and exit status from simulated feedback.
- [ ] Re-run graph and tool tests. Ensure calls that write candidate files are not repeated on graph replay; keep mutation nodes separate from interrupt nodes and test duplicate invocation explicitly.

### Task 6: Add review pause, resume and controlled learning

**Files:** Create `FAGENT/agent/review.py`; modify `FAGENT/agent/{nodes,graph}.py`, `FAGENT/rag/learning.py`; test `tests/test_review.py`, `tests/test_fagent_graph.py`.

**Interfaces:** Review node uses `interrupt()` and accepts resume feedback; checkpointer plus a configurable `thread_id` identifies a run. `ReviewState.run(feedback)` remains the authority for save eligibility.

- [ ] Add tests for EOF, rejection, explicit completion plus save request, review invalidation after edit or validation, resume with same thread, and repeated resume without duplicate case or mutation.
- [ ] Run `python -m pytest -q tests/test_review.py tests/test_fagent_graph.py`; confirm new cases fail before implementation.
- [ ] Implement review and conditional storage nodes. Authorize storage only when review evidence and current structured analysis are both valid; preserve idempotent record IDs and avoid replaying operations before interrupts.
- [ ] Re-run the same tests and verify unauthorized or incomplete cases never reach the store.

### Task 7: Integrate CLI, artifacts and documentation

**Files:** Create `FAGENT/cli.py`; modify `FAGENT/__main__.py`, `FAGENT/AGENT/caller.py`, `README.md`, `tests/test_structure_setup.py`, `tests/test_agent.py`.

**Interfaces:** `main()` retains existing CLI options and `python -m FAGENT`; CLI invokes `build_graph`, renders review prompts and resumes with `Command(resume=feedback)` using the same thread ID. Preserve `--load-only`, `--quiet`, output directories, and compatibility caller imports.

- [ ] Add subprocess tests for load-only and a fake-model run: verify exit status, `candidate.gds`, `conversation.json`, `result.json`, source file unchanged, and simulated validation marked incomplete. Verify missing optional references are accepted.
- [ ] Run `python -m pytest -q tests/test_structure_setup.py tests/test_agent.py` and record failures.
- [ ] Switch CLI to graph execution and update README with configuration, runtime storage, removed legacy folders, migration shims and simulated validation behavior.
- [ ] Re-run tests; inspect produced artifacts and log errors, and verify the CLI does not claim a real Calibre run without actual execution and collected output.

### Task 8: Regression and environment verification

**Files:** All above; no production changes unless failures justify them.

- [ ] Run `python -m pytest -q tests/test_agent.py tests/test_structure_setup.py tests/test_caller_context.py tests/test_json_flow.py tests/test_llama_adapter.py tests/test_rag.py tests/test_review.py tests/test_fagent_tools.py tests/test_fagent_graph.py` and compare with the recorded baseline.
- [ ] Run `python -m compileall -q FAGENT` and `git diff --check`; inspect `git status --short` for unintended knowledge or user-file changes.
- [ ] If a compatible local GGUF and dependencies are available, run a configured smoke test and record model path, tool-call format, exit status and artifacts. Otherwise explicitly mark real-model validation unperformed.
- [ ] If Calibre and a configured rule deck are available, run a separately authorized/configured validation and inspect command exit status, log and DRC result artifacts; otherwise report that only simulated validation was exercised.

## Representative Example

Input: `python -m FAGENT --gds <cell.gds> --drc <cell.drc.results> --output <run-dir> --config <config.json>`. Geometry returned by `read_gds` is in GDS user units; DRC geometry retains the source scale and database units. A fake model requests `replace_polygon`, then `validate`; simulated Calibre feedback results in `result.json` with `status: incomplete` and `validation.simulated: true`. After explicit completion and save feedback, the approved case may be indexed once. A real DRC pass requires actual tool execution and collected results.
