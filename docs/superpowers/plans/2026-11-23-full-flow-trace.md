# Full Workflow Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Capture the complete observable workflow in structured `conversation.json` events and an easy-to-read `trace.log`.

**Architecture:** Initialize the existing run-scoped trace immediately after allocating a run directory. Record paired operation events at input-loading, service, graph, RAG, validation and finalization boundaries; retain existing model/tool events and version 2 compatibility. Render compact human-readable summaries from the same structured events.

**Tech Stack:** Python, JSON, pytest, LangGraph, existing DRC and RAG services.

**Spec:** `docs/superpowers/specs/2026-11-23-full-flow-trace-design.md`

## Global Constraints

- Preserve version 2 event fields and case learner behavior; do not change repair or validation decisions.
- Record actual available operation inputs and outputs; structured full payloads in JSON and concise chronological summaries in the log.
- Trace events must have timestamps, status and identifiers, record failures, survive resume and use atomic output writes.
- Derive paths, cell names, query and rule data from actual input and configuration.
- Use no nested functions and place `run` first in modified classes.

## Review Focus

- Interrupted or failed preparation: a started operation remains visible and failure details are persisted.
- Resume: new operation identifiers do not collide and previous events remain intact.
- Empty DRC or RAG retrieval: results are recorded as empty without inventing data.
- Existing version 2 events: case learner extraction and readable output still work.
- Tool or validation failure: error and final incomplete status are observable without claiming a pass.

## Representative input and output

Input: a DRC report with two violations and rules `RULE_X`, `RULE_Y`; retrieval query and limit come from the actual report and configuration. Output: read event contains the full report, retrieval event contains the actual query and returned records, and `trace.log` lists the corresponding read and retrieval in order before plan and repair. Inspect the source spec with `grep -n 'Scope and event contract\|Readable output' docs/superpowers/specs/2026-11-23-full-flow-trace-design.md`.

### Task 1: Trace lifecycle and compatibility

**Files:** Modify `FAGENT/engine/conversation_trace.py`; test `tests/test_readable_trace.py` and `tests/test_conversation_snapshots.py`.

**Interfaces:** Provide `ConversationTrace.start_operation(name, inputs) -> str` and `finish_operation(operation_id, name, status, outputs=None, error=None) -> None`; preserve `append(event)` and version 2 model events.

- [ ] Add tests for full structured payloads, UTC timestamps, paired IDs, errors, existing events, resume IDs, and readable summaries.
- [ ] Run targeted pytest and confirm the new tests fail for absent lifecycle support.
- [ ] Implement lifecycle events and readable formatting, using atomic writes and output path checks.
- [ ] Run targeted tests and verify they pass.

### Task 2: Input preparation and graph boundaries

**Files:** Modify `FAGENT/tasks/drc/steps.py`, `FAGENT/tasks/lvs/steps.py` as applicable, `FAGENT/engine/graph_services.py`, `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/graph_runner.py`; add focused tests under `tests/`.

**Interfaces:** Reuse `ConversationTrace` on the allocated rundir; emit preparation, service setup, graph node, resume, review and finalization operations with actual available data and outcomes.

- [ ] Add integration tests using isolated run directories: input load success/failure, node ordering and routing, tool failure, resume, final result and incomplete status.
- [ ] Run new tests and confirm expected failures.
- [ ] Instrument actual boundaries, including early preparation before `GraphServices` exists; propagate original errors and preserve behavior.
- [ ] Run targeted tests and verify events and readable log ordering.

### Task 3: DRC and RAG provenance

**Files:** Modify `FAGENT/engine/graph_services.py` and, only where necessary for actual indexing details, `FAGENT/rag/hybrid_retriever.py`; test `tests/test_drc_context_flow.py` and focused trace tests.

**Interfaces:** Record DRC read input source and complete report; RAG initialization/index inputs and returned outputs; retrieval query, rule names, limit and full retrieved records; record error paths.

- [ ] Write tests for nonempty and empty reports/results, complete structured record provenance, ordering, and RAG/read errors.
- [ ] Run tests and confirm the expected missing-event failures.
- [ ] Instrument real boundaries with actual values and operation status, without re-running retrieval or altering results.
- [ ] Run targeted tests and verify `conversation.json` and `trace.log` artifacts.

### Task 4: Regression and artifact inspection

**Files:** All files modified by prior tasks.

- [ ] Run `python -m pytest -q` and report all failures and warnings.
- [ ] Inspect an isolated representative run's JSON and log for ordering, complete inputs and outputs, error paths and correlation.
- [ ] Check `git diff --check` and coding rules in modified files; report any unavailable real EDA/model verification.
