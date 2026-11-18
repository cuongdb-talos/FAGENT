# FAGENT Hybrid RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace bulk learned-knowledge prompt loading with locally indexed hybrid retrieval and retain labeled repair outcomes.

**Architecture:** `Agent_rag` owns persistent records, local Ollama embeddings, lexical and semantic retrieval, and migration; `Agent_learn` creates evidence-based cases and `Agent_caller` supplies selected cases with mandatory instructions. All runtime knowledge lives under `FAGENT/knowledge`; simulated validation remains accepted and labeled.

**Tech Stack:** Python, SQLite, NumPy, OpenAI-compatible Ollama embeddings, pytest.

**Spec:** `docs/superpowers/specs/2026-11-12-fagent-hybrid-rag-design.md`.

## Global Constraints

- Preserve pre-existing user changes and data; migrate legacy records without deleting original information.
- Define `run()` first in every class; store produced outputs on `self`.
- All knowledge and index paths resolve inside `FAGENT/knowledge`; reject external paths and aliases.
- Do not call a simulated result a real Calibre run. No real Calibre integration is in scope.
- Model, endpoint, retrieval limit and context limit come from configuration; do not embed project-specific rule names or data.
- `executing-plans` mandates Liberty comparison for every task; this workflow produces no Liberty. Resolve this conflicting completion requirement before marking implementation tasks complete.

## Review Focus

- Embedding outage: persist pending records and return labeled lexical fallback.
- Model change: reject or rebuild incompatible vectors; never compare mismatched dimensions.
- Multiple rules: do not present run-wide edits as a confirmed fix for each rule.
- Repeat ingestion: no duplicated run cases or migrated records.
- Simulated validation: preserve its provenance independently from outcome.

## Representative input and output

Input: `drc.json` containing `rules: [{"name": "M1.S.1", "description": ["metal spacing"]}]`, a run with `status: verified`, and validation `{ "status": "good", "simulated": true }`.

Expected stored case: `rule: M1.S.1`, `outcome: pass`, `validation_source: simulated`, a stable source identifier and unambiguous evidence. Querying `M1.S.1` returns it ahead of unrelated rules; an incomplete run is stored with `outcome: incomplete`.

Document sections to update: `grep -nE '^## |^### ' .agent/AI_Workflow.md`.

## Task 1: Isolate work and capture baseline

**Files:** Existing workspace and `docs/superpowers/specs/2026-11-12-fagent-hybrid-rag-design.md`.

- [ ] Create or verify an isolated worktree using `using-git-worktrees`; preserve the current uncommitted database, source changes, and spec by explicitly copying the required snapshot without overwriting unrelated work.
- [ ] Record baseline `git status --short`, run `python -m pytest -q tests/test_agent.py`, and record any pre-existing failures separately.
- [ ] Read the TDD and verification skills. Record the Liberty comparison gate's applicability before implementing.

## Task 2: Implement local storage and migration

**Files:** Create `FAGENT/rag.py`, `tests/test_rag.py`; modify configuration documentation as needed.

**Interfaces:** `Agent_rag.run() -> Agent_rag` initializes storage; `index_records(records: list[dict]) -> list[str]` stores normalized records by stable ID; `migrate_legacy(path: Path) -> int` imports legacy records idempotently with `validation_source: unknown`.

- [ ] Write failing tests for persistent round-trip, duplicate IDs, distinct runs sharing a rule, external/symlinked knowledge paths, empty legacy data, and migration retaining all legacy content.
- [ ] Run `python -m pytest -q tests/test_rag.py`; verify the new tests fail for missing implementation.
- [ ] Implement local SQLite records, reproducible IDs, ownership/path checks, and legacy migration. Keep the source database intact and avoid marking legacy outcomes as verified real Calibre.
- [ ] Re-run `python -m pytest -q tests/test_rag.py`; require all task tests to pass, and record verification and any Liberty comparison constraint.

## Task 3: Implement hybrid retrieval

**Files:** Modify `FAGENT/rag.py`, `tests/test_rag.py`, `config.json`, `config.agy.json`.

**Interfaces:** `retrieve(query: str, rule_names: list[str], limit: int) -> list[dict]` returns records with source, outcome, validation source, score and retrieval mode; model and endpoint are independently configured.

- [ ] Write failing tests with a fake embeddings client for exact-rule priority, semantic relevance, lexical fallback on outage, absent data, model/dimension mismatch, pending indexing retry and bounded results.
- [ ] Run the tests and record the expected failures.
- [ ] Implement configured local embeddings, lexical matching, vector cosine scoring and deterministic ranking. Store model identity; do not compare incompatible vectors. Persist records before requesting embeddings and preserve pending work on failures.
- [ ] Re-run `python -m pytest -q tests/test_rag.py`; require all tests to pass and verify fallback results identify their mode.

## Task 4: Learn from labeled outcomes

**Files:** Modify `FAGENT/learn.py`, `FAGENT/caller.py`, `tests/test_agent.py`, `tests/test_rag.py`.

**Interfaces:** `Agent_learn.run()` reads run artifacts, stores extracted cases via `Agent_rag.index_records`, and exposes stored IDs on the instance; the caller persists validation evidence in `result.json` and invokes learning for all terminal outcomes.

- [ ] Write failing tests for verified simulated pass, explicit fail, incomplete/no validation, both DRC representations, repeat ingestion, and multi-rule conversations without unsupported rule-to-edit attribution.
- [ ] Run the targeted tests and record the expected failures.
- [ ] Extract stable run/case identities and evidence; keep run-level tool history separate from rule-specific evidence unless linkage is supported. Preserve LLM refinement only when its output does not invent verification or associations.
- [ ] Update `result.json` to carry `last_validation` including `simulated`, and invoke learner after writing artifacts for both verified and incomplete runs without changing accepted simulated pass behavior.
- [ ] Re-run the targeted tests; ensure all pass and that rerunning the same artifacts is idempotent.

## Task 5: Integrate prompt retrieval and relocate knowledge

**Files:** Move `.agent/rules/*.md`, `.agent/contexts/*.md`, `.agent/skills/*.md` under appropriate directories in `FAGENT/knowledge`; modify `FAGENT/caller.py`, `tests/test_agent.py`, `tests/test_rag.py`.

- [ ] Write failing tests confirming mandatory instructions are always present, only bounded retrieved records appear, outcomes and provenance are visible, both DRC formats work, and empty/offline retrieval leaves the caller functional.
- [ ] Run targeted tests and verify failure before implementation.
- [ ] Relocate mandatory instructions while preserving contents; update the caller to index available reference knowledge, query from current violations, and inject retrieved material as labeled data separate from mandatory instructions.
- [ ] Re-run the targeted tests and `python -m pytest -q tests/test_agent.py tests/test_rag.py`; resolve regressions without reverting unrelated workspace changes.

## Task 6: Document and verify the complete flow

**Files:** Modify `.agent/AI_Workflow.md`, `README.md` where existing guidance conflicts.

- [ ] Use the document grep command above; update architecture, dataflow, configuration, storage, migration, failed-run learning, retrieval fallback and simulated Calibre sections to match implementation.
- [ ] Run `python -m pytest -q tests/test_agent.py tests/test_rag.py`, inspect exit status, and run applicable CAD/EDA checks from `verification-before-completion`.
- [ ] Check whether a configured local Ollama embedding model is actually available; report a real-service check separately from mocked tests. Do not claim real Calibre or Liberty output when none was produced.
- [ ] Review the full diff against the spec, including the five review-focus cases and pre-existing uncommitted data. Record limitations and all verification evidence.
