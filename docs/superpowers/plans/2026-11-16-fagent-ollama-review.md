# FAGENT Ollama and Human Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents for implementation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use Ollama for reasoning and fast CPU embedding while allowing only explicitly approved completed runs into learned RAG knowledge.

**Architecture:** Preserve the current working-tree implementation as the starting point. Separate review authorization from model decisions and apply it both at learning and retrieval boundaries. Batch eligible embeddings through a reusable Ollama client, retaining the existing hybrid ranking and cache while distinguishing validation provenance from review.

**Tech Stack:** Python, OpenAI-compatible Ollama API, SQLite, pytest, JSON.

**Spec:** `docs/superpowers/specs/2026-11-16-fagent-ollama-review-design.md`

## Global Constraints

- Ollama only for reasoning and embeddings; reasoning model is `gemma4`.
- Target AMD EPYC 7H12, approximately 60 GB available RAM, CPU only; choose model and batch size using target-server measurements.
- A user must explicitly confirm task completion and request knowledge storage; model output is not authorization. Incomplete and unreviewed learned cases cannot be retrieved.
- Keep existing unreviewed learned data, mandatory instructions, reference documents and validation provenance. Never claim simulated Calibre validation is real.
- Preserve the user's existing tracked and untracked changes; set up an isolated workspace from their actual working-tree state without committing or discarding those changes. Record how the isolated baseline differs from HEAD.
- Follow repository class conventions: `run` first, avoid nested functions, store outputs on `self`, derive configurable values.
- The invoked executing-plans skill requires output-versus-target Liberty comparison before marking any task complete. Check whether this GDS/DRC flow provides the required output and `ref_lib`; if either is unavailable, report the unmet verification gate without fabricating artifacts or claiming task completion.

## Review Focus

- Negated or quoted save instructions do not authorize persistence: test in Task 1.
- A new modification after completion review invalidates earlier authorization: test in Task 2.
- Partial embedding batches, mismatched dimensions or a failed Ollama call never poison the cache: test in Task 4.
- Legacy migrations cannot make unreviewed records eligible: test in Task 3.
- Noninteractive EOF and interrupted input preserve available run artifacts without learning: test in Task 2.

## File map

- `FAGENT/review.py`: review state and explicit terminal authorization protocol.
- `FAGENT/caller.py`, `FAGENT/learn.py`, `FAGENT/functions.py`: orchestration, gated learning and validation provenance.
- `FAGENT/rag.py`: eligible record filtering, batched embeddings and versioned cache.
- `cmd`, `config.json`, `README.md`, `.agent/AI_Workflow.md`: Ollama configuration and user workflow.
- `tests/test_review.py`, `tests/test_rag.py`, `tests/test_agent.py`: isolated unit and integration tests.
- `scripts/benchmark_fagent_embedding.py`: configurable benchmark and retrieval-quality report, with no production knowledge mutation.

### Task 1: Explicit review authorization

**Files:** Create `FAGENT/review.py`, `tests/test_review.py`.

**Interfaces:** Produces `ReviewState.run(feedback: str) -> ReviewState`, `ReviewState.authorized: bool`, `ReviewState.reset() -> None`, and `ReviewState.evidence: dict`. The caller must supply raw user feedback and never set authorization from model output.

- [ ] **Step 1:** Write tests for explicit complete-and-save, incomplete-and-save, completion without saving, ambiguous, negated and quoted instructions. Use representative English and Vietnamese input; unknown forms fail closed and ask for clarification.
- [ ] **Step 2:** Run `pytest -q tests/test_review.py`; expected: FAIL because `FAGENT.review` does not exist.
- [ ] **Step 3:** Implement conservative explicit matching for affirmative completion and save in the user's own input; ignore quoted statements and reject negative or ambiguous forms. If intent cannot be safely identified, leave unauthorized and request clarification. Store raw evidence and avoid inferring intent from agent-generated text.
- [ ] **Step 4:** Run `pytest -q tests/test_review.py`; expected: all tests PASS.
- [ ] **Step 5:** Commit only Task 1 files on the isolated branch.

### Task 2: Interactive review and learning gate

**Files:** Modify `FAGENT/caller.py`, `FAGENT/learn.py`, `FAGENT/functions.py`; test `tests/test_review.py`, `tests/test_agent.py`.

**Interfaces:** Consumes Task 1 `ReviewState`. Produces `Agent_learn.run(..., review: dict = None)` with a fail-closed authorization check and reviewed provenance on every stored record; `Agent_caller.run(...)` persists review evidence and resumes chat with user messages.

- [ ] **Step 1:** Write mocked conversation tests for feedback leading to another function call then review, explicit completed/save leading to learning, completion alone, incomplete/save, negation, EOF, interruption, tool failure and iteration exhaustion. Assert a later layout modification resets previous authorization. Assert simulated validation remains simulated.
- [ ] **Step 2:** Run `pytest -q tests/test_review.py tests/test_agent.py`; expected: FAIL on missing interactive review and ungated learning; note unrelated baseline failures separately.
- [ ] **Step 3:** Implement the terminal review loop and model tool dispatch. Feed review messages to `gemma4`, accept model save proposals only after an independent user authorization, persist run artifacts, and fail closed on missing input. Limit iterations per interaction using configuration and reset the approval when work changes. Enforce the same gate in `Agent_learn` so direct callers cannot bypass it. Preserve status and simulated validation separately.
- [ ] **Step 4:** Update existing tests whose old behavior expected automatic learning, then run `pytest -q tests/test_review.py tests/test_agent.py`; expected: all applicable tests PASS.
- [ ] **Step 5:** Commit only Task 2 files on the isolated branch.

### Task 3: Restrict retrieval to reviewed cases

**Files:** Modify `FAGENT/rag.py`, `FAGENT/learn.py`; test `tests/test_rag.py`.

**Interfaces:** Consumes Task 2 reviewed provenance. Produces `Agent_rag.eligible(record: dict) -> bool`; `retrieve(...)` filters before ranking and before embedding.

- [ ] **Step 1:** Write tests showing unreviewed legacy, old case and old run-context records are retained but absent from retrieval and embedding requests; authorized reviewed cases and `kind=reference` remain available; repeated legacy migration never grants review.
- [ ] **Step 2:** Run `pytest -q tests/test_rag.py`; expected: FAIL because old records are currently retrieved.
- [ ] **Step 3:** Implement fail-closed eligibility based on explicit persisted review evidence, preserving existing record contents and mandatory/reference paths. Include review provenance in prompt results alongside independent validation provenance.
- [ ] **Step 4:** Update old tests to supply explicit reviewed provenance where retrieval is expected; run `pytest -q tests/test_rag.py`; expected: PASS.
- [ ] **Step 5:** Commit only Task 3 files on the isolated branch.

### Task 4: Batched Ollama embedding and cache

**Files:** Modify `FAGENT/rag.py`, `config.json`, `cmd`, `FAGENT/caller.py`; test `tests/test_rag.py`, `tests/test_agent.py`.

**Interfaces:** Consumes Task 3 `eligible`. Produces reusable Ollama embedding client and `Agent_rag.embed_batch(texts: list[str], purpose: str) -> list[list[float]]`; retain `retrieve(query, rule_names, limit)`.

- [ ] **Step 1:** Add fake-client tests asserting batches of configurable size, reuse on warm retrieval, cache invalidation after model or formatting changes, stable ranking and no requests for ineligible records. Test offline fallback, partial batch, incorrect dimensions and retry without invalid cached data. Verify configured `gemma4` and Ollama-only provider path.
- [ ] **Step 2:** Run `pytest -q tests/test_rag.py tests/test_agent.py`; expected: FAIL because batching and cache identity are absent.
- [ ] **Step 3:** Use one Ollama-compatible client per RAG instance and the embedding API's multiple-input request. Batch eligible missing vectors, validate complete finite dimension-consistent results before cache writes, bind cache identity to model and query/document formatting, and log fallback. Configure a bounded batch size and set the normal command to `config.json`; reject AGY provider in supported flow.
- [ ] **Step 4:** Run `pytest -q tests/test_rag.py tests/test_agent.py`; expected: PASS.
- [ ] **Step 5:** Commit only Task 4 files on the isolated branch.

### Task 5: Benchmark and workflow documentation

**Files:** Create `scripts/benchmark_fagent_embedding.py`, `tests/test_embedding_benchmark.py`; modify `README.md`, `.agent/AI_Workflow.md`.

**Interfaces:** Consumes Task 4 embedding client and retrieval. Benchmark accepts a labeled DRC query corpus, Ollama model names, batch sizes and output path; produces JSON with settings, cold indexing time, warm retrieval and query latency, peak memory and recall at configured k. Use a temporary knowledge store; do not mutate production records.

- [ ] **Step 1:** Add tests for a tiny labeled corpus, multiple batch configurations, metric calculations, unavailable service reporting and unchanged production files.
- [ ] **Step 2:** Run `pytest -q tests/test_embedding_benchmark.py`; expected: FAIL because benchmark is absent.
- [ ] **Step 3:** Implement benchmark and document CPU server setup, review input examples, EOF behavior, legacy exclusion, simulated validation and offline fallback. Include representative corpus input `{"query":"CT enclosure","rules":["CT_EN"],"expected_ids":["case-1"]}` and output fields for cold seconds, warm latency and recall. Locate existing documentation with `grep -n 'Hybrid RAG\|Calibre và validation\|AGY' README.md`.
- [ ] **Step 4:** Run `pytest -q tests/test_embedding_benchmark.py tests/test_review.py tests/test_rag.py tests/test_agent.py`; expected: PASS. Run a real Ollama integration and benchmark on the target server if available; otherwise explicitly report them unverified and do not select a claimed fastest model.
- [ ] **Step 5:** Commit only Task 5 files on the isolated branch.
