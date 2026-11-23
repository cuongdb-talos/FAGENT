# DRC Agent Geometry and Replanning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a report-driven checklist and model-guided DRC repair flow backed by traceable internal geometry tools.

**Architecture:** `read` constructs stable checklist entries; `plan` returns ordered tool intents using selected report records and RAG, and `repair` calls task-local tools against candidate-bound state. Internal geometry services return structured evidence, conservatively reject unsupported rule semantics and stage recoverable edits; failures route back to planning with error history and bounded iterations.

**Tech Stack:** Python, LangGraph, gdstk, Shapely, JSON Schema, pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-drc-agent-geometry-design.md`

## Global Constraints

- All geometry operations execute inside FAGENT; no external application is invoked by the tools.
- No hard-coded project rules, cells, layers, thresholds, units, or paths; derive them from inputs and configuration.
- Do not fabricate rule semantics, polygon identity, connectivity, a repair or a pass.
- Coordinates use integer DBU with explicit conversion metadata; never silently round a required dimension downward.
- The model receives `plan` but code does not enforce tool ordering; existing editing guards remain active.
- Local geometry success is not full DRC validation; unsupported or ambiguous cases remain unresolved.
- Preserve input GDS, prior candidate on failed writes, stable polygon identities, digest-bound evidence and bounded replanning.
- Use TDD, no nested functions, `run` first in modified classes, and store class results on `self`.

## Review Focus

- Multiple source polygons or derived layers: return ambiguous/unsupported, never silently select one (Task 3).
- Empty plan with active violations: terminate incomplete while retaining checklist; empty report is separately handled (Task 2).
- Tool failure after successful edits: replan using current candidate and actual failed conversation; terminate on retry limit (Task 2).
- Stale proposal or failed candidate write: reject or recover original candidate and polygon mapping (Task 5).
- CT coverage by M1 without `(GT OR AA)` or with unhandled INST/EXCLU: do not assert full-rule pass (Task 4).

## Inputs and example

Inputs are configured GDS, parsed DRC report, layer maps, rule deck, goal and RAG records. Locate deck definitions with `grep -n -A 16 '^CT_EN_7a_7b\|^CT_R_11' DRC/1P7M_6Ic_1TKMc_ALPA2.drc`. Example fixture: a supported independently defined enclosure check requires 25 DBU and measures 18 DBU; output reports 7 DBU deficit with contributing edges. These values are test fixture inputs, not production defaults. For actual `CT_R_11`, M1-only coverage cannot pass the full rule.

## File map

- `FAGENT/tasks/drc/checklist.py`, `steps.py`: deterministic checklist and per-task report lookup.
- `FAGENT/engine/fagent_state.py`, `graph_nodes.py`, `graph_builder.py`, `FAGENT/tasks/drc/workflow.yaml`, `schemas/plan.json`, `prompts/plan.md`: planning, state, routing and replan budget.
- `FAGENT/tasks/drc/tools.py`: `ToolSpec` adapters and state updates.
- `FAGENT/tasks/drc/rule_evidence.py`: exact source rule retrieval and layer-map provenance.
- `FAGENT/capabilities/layout/violation_geometry.py`: task-to-polygon identification and DBU conversion.
- `FAGENT/capabilities/layout/repair_proposals.py`: supported measurement, proposals and candidate-bound verification.
- `FAGENT/capabilities/layout/layout_service.py`: recoverable staged candidate persistence.
- Focused new tests: `tests/test_drc_report_checklist.py`, `tests/test_drc_replanning.py`, `tests/test_drc_geometry_evidence.py`, `tests/test_drc_repair_proposals.py`.

## Task 1: Report-driven checklist and lookup

**Interfaces:** `Checklist.run(report, proposed_tasks=None, goal='')` builds pending report items when `proposed_tasks is None`; `get_violation(context, task_id)` returns source record, task and candidate identity through `ToolResult`.

- [ ] Add tests asserting two report violations yield two unique pending items with original rule/ID/occurrence, no invented work/evidence; duplicated IDs use distinct occurrence; empty report yields empty checklist; missing task raises a clear error.
- [ ] Run `python -m pytest -q tests/test_drc_report_checklist.py` and confirm failure is missing report-driven behavior.
- [ ] Implement deterministic initialization in `checklist.py`, invoke it from `steps.read_drc`, persist checklist, add `get_violation` tool, and preserve prior snapshot restore behavior.
- [ ] Run the new tests and existing checklist/DRC context tests; verify actual report entries match report identity.

## Task 2: Plan state, repair messages, and replan routing

**Interfaces:** `FagentState.plan` stores `{'summary': str, 'steps': [{'tool': str, 'purpose': str, 'task_id': str}]}`; `FagentState.replan_context` stores actual failed tool call/result and selected relevant violation IDs; `FagentState.replan_count` counts failures within `max_iterations`.

- [ ] Add graph tests with a stub model: plan receives active report violations and RAG but no `drc_context`; repair receives `plan` and checklist; empty plan with active tasks yields `incomplete`; no active tasks do not produce a fabricated plan.
- [ ] Add a test for tool failure after a saved edit: second plan gets matching failure conversation, current candidate digest, relevant violations, RAG, plan and checklist; repeating failures reaches the configured limit and stops incomplete.
- [ ] Run `python -m pytest -q tests/test_drc_replanning.py` and confirm target failures.
- [ ] Update schema/prompt, state and builder routes; construct request payloads from actual state, store plan, route failed tools to plan, retain tool-call IDs and candidate state, and record deviations without enforcing tool order.
- [ ] Run new tests plus `tests/test_drc_context_flow.py` and `tests/test_drc_node_prompt_isolation.py`.

## Task 3: Rule and polygon evidence

**Interfaces:** `RuleEvidence.run(task, deck_paths, layer_maps, references) -> dict` stores `self.result`; `ViolationGeometry.run(task, rule_evidence, layout, candidate_digest) -> dict` stores `self.result`. Adapters expose `find_rule(context, task_id)` and `find_related_polygons(context, task_id)` as `ToolResult`.

- [ ] Add fixture tests: exact rule name and source location retrieved from actual deck text; CT/M1 layer mapping is physical-layer-specific; marker matched to one polygon reports its cell, ID, DBU geometry and evidence; multiple matches remain ambiguous.
- [ ] Add tests that unknown layer mapping, derived layer semantics, missing marker, unsupported hierarchical references and paths produce `unsupported`/`ambiguous` without selecting a polygon.
- [ ] Run `python -m pytest -q tests/test_drc_geometry_evidence.py` and confirm missing functionality fails.
- [ ] Implement evidence services and task-local ToolSpecs, respecting candidate digest and user-specified or rule-derived search region; preserve raw original rule text and RAG references.
- [ ] Run targeted tests, including actual configured report/deck and layer map as read-only fixtures; inspect returned marker/polygon provenance.

## Task 4: Measure and propose supported geometry

**Interfaces:** `RepairProposals.run(task, rule_evidence, geometry, layout, candidate_digest) -> dict` stores `self.result`; tool adapters expose `measure_violation(context, task_id, polygon_ids)` and `propose_polygon_fix(context, task_id, polygon_ids)`.

- [ ] Add independently calculated rectangle tests asserting a 25 DBU requirement and 18 DBU measurement produce a 7 DBU deficit, including units, edges and revision; cover quantization and determinism.
- [ ] Test union coverage, uncovered geometry, empty/ambiguous candidates and actual `CT_R_11` and `CT_EN_7a_7b` conditions: no full pass or proposal when exclusions, directional semantics, derived layers, connectivity or shared-cell effects are unproven.
- [ ] Run `python -m pytest -q tests/test_drc_repair_proposals.py` and confirm expected failures.
- [ ] Implement only explicitly proven geometry subcases and return `unknown`/`unsupported` otherwise; proposals contain before/after vertices, assumptions, revision, nearby constraint checks and a stable ID.
- [ ] Run targeted geometry and existing cover-check tests; compare results against independently derived fixture values.

## Task 5: Transactional apply and local recheck

**Interfaces:** `apply_polygon_fix(context, repair_id)` stages a candidate and ID map, checks proposal digest, returns `ToolResult`; `recheck_violation(context, task_id)` returns local evidence bound to the saved candidate digest and updates checklist only on supported evidence.

- [ ] Add tests for valid edits, stale proposal rejection after another edit, original GDS preservation, ID mapping restoration, evidence invalidation and simulated write failures retaining a recoverable previous candidate.
- [ ] Add tests asserting local recheck `unknown`/`fail` never becomes verified and a local pass never claims deck-level DRC clean.
- [ ] Run tests and confirm the failure reflects unavailable transactional apply/recheck.
- [ ] Implement candidate staging and recoverable replacement in `layout_service.py` and the two adapters in `tools.py`; only apply a proposal whose rule assumptions, connectivity and shared-cell impact are demonstrably supported.
- [ ] Run targeted tests and inspect saved GDS geometry and checksum against expected fixtures.

## Task 6: End-to-end verification

- [ ] Register all tool bindings in `workflow.yaml`; verify schema and loader tests, output provenance and chronological trace.
- [ ] Run a graph integration test with a stub model: report → deterministic checklist → plan → geometry tools → replan on tool error → bounded incomplete result or locally rechecked candidate. Assert that no call-order enforcement is introduced.
- [ ] Run `python -m pytest -q` and `git diff --check`; report exact failures and warnings. Inspect modified Python for nested definitions and class method ordering.
- [ ] Inspect real report/deck mapping using the configured input without altering it; if a real Calibre run or connectivity check is unavailable, explicitly report that no full DRC or connectivity verification occurred.
