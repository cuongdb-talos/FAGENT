# Input Validation and Tool Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate configured inputs before workflow preparation, consistently resolve layer mappings, and pause DRC execution for user review when a tool fails or cannot determine its result.

**Architecture:** Normalize all layer-map formats through one parser used by GDS and DRC consumers. Preflight configured inputs before any task preparation; separately, route tool exceptions and explicitly indeterminate outcomes to a checkpointed review interrupt, with explicit resume decisions and trace evidence.

**Tech Stack:** Python, PyYAML, JSON Schema, LangGraph SQLite checkpoints, pytest.

**Spec:** Conversation design agreed on 2026-11-25: early GDS/map validation; no guessed mapping; review unknown results and exceptions; continue, skip task, or stop. This plan records the design directly; no separate spec file exists.

## Global Constraints

- Derive paths and PDK details from YAML; do not invent layer numbers or default aliases.
- Preserve existing user changes and run artifacts; do not use `./cmd` while it removes `output`.
- Keep class `run` methods first, avoid nested functions, and preserve CAD/EDA units and provenance.
- Do not declare a DRC pass from a successful tool invocation alone.
- Keep current task and plan history when pausing or resuming.

## Review Focus

- Conflicting mappings between files must identify both file and line and block editing (Task 1).
- GDS drawing layers missing a map must stop preflight; map layers absent in GDS are informational (Task 2).
- Derived deck expressions must not be silently treated as physical drawing layers (Task 3).
- Restart after an interrupt must not rerun a mutating tool or lose its result (Task 4).
- Whitespace-only `work` must not pass validation or trigger uncontrolled replanning (Task 4).

## Inputs and expected outputs

Representative YAML: `layer_map_files: [config/layer_mapping, config/smec55.layermap]`; the two documented formats are `61 0 M1 drawing` and `GT drawing 30 0`. The normalized entries are `M1/drawing -> (61, 0)` and `GT/drawing -> (30, 0)`, each with file and line provenance. A conflicting `GT drawing 31 0` must yield a conflict report rather than a selected layer.

Representative review: `measure_violation(task-3)` returning `status=unknown` and a reason pauses execution; output contains the tool, task, call arguments, result, and checkpoint identity. User decision `skip_task` marks the task unresolved with the supplied reason; `continue` does not imply success or a DRC pass.

For document inputs locate the relevant rule before checking its semantics:

```bash
grep -n -A 4 '^GT_A_13[[:space:]]*{' DRC/1P7M_6Ic_1TKMc_ALPA2.drc
```

## Task 1: Shared layer-map parser

**Files:** Create `FAGENT/capabilities/layout/layer_map.py`, `tests/test_layer_map_preflight.py`; modify `FAGENT/capabilities/layout/process_gds.py`, `FAGENT/tasks/drc/rule_evidence.py`.

**Interfaces:** `LayerMap.run(paths: list[Path]) -> LayerMap` stores normalized entries and diagnostics on the instance. Entries contain name, purpose, layer, datatype, source and line. Both existing consumers use this result.

- [ ] Add failing tests for both four-column formats, comments, duplicate identical entries, whitespace, and malformed non-comment lines; assert exact normalized values and source lines.
- [ ] Add failing tests for conflicting `(name, purpose)` mappings and conflicting names for the same `(layer, datatype, purpose)`; assert both sources are reported and no mapping is silently chosen.
- [ ] Run `pytest -q tests/test_layer_map_preflight.py`; verify the new tests fail because the shared parser does not exist.
- [ ] Implement `LayerMap.run`; reject unknown formats and ambiguous mappings with actionable diagnostics. Do not infer `drawing` from an undocumented format. Integrate the parser in both consumers without changing the existing interpretation of a known purpose.
- [ ] Run `pytest -q tests/test_layer_map_preflight.py tests/test_drc_geometry_evidence.py tests/test_drc_geometry_integration.py`; require all tests to pass.

## Task 2: Validate configured inputs before preparation

**Files:** Create `FAGENT/engine/input_preflight.py`, `tests/test_input_preflight.py`; modify `FAGENT/fagent.py` and existing task preparation interfaces only where necessary.

**Interfaces:** `InputPreflight.run(setup: dict, setup_path: Path, config: dict) -> InputPreflight` stores `report` with errors, warnings and provenance on the instance. It runs after YAML/config parsing and before `workflow.prepare`, RAG initialization or model requests.

- [ ] Add failing tests for missing required YAML fields, missing/unreadable map/deck/GDS inputs, malformed maps, invalid GDS, unmapped GDS geometry pairs, and an unused mapped layer. Assert error path, issue, and no calls to workflow preparation or model on failure. Check duplicate cell/input handling and text elements separately from drawing geometry.
- [ ] Run `pytest -q tests/test_input_preflight.py`; verify failure before implementation.
- [ ] Implement preflight with configuration-relative paths and the shared parser. Read GDS headers, geometry pairs, units and hierarchy using the existing reader or a read-only inspection path; do not create run artifacts or mutate GDS. Validate configured task inputs according to task type; report absent-in-GDS mappings as warnings. Include input source and GDS pair in diagnostics.
- [ ] Test a valid representative GDS/map and all negative fixtures: `pytest -q tests/test_input_preflight.py tests/test_flow_boundaries_trace.py`. Assert a failed CLI execution has nonzero status and no RAG/model invocation.

## Task 3: Resolve rule and layer evidence without guessing

**Files:** Modify `FAGENT/tasks/drc/rule_evidence.py`, `FAGENT/capabilities/layout/violation_geometry.py`; test in `tests/test_drc_geometry_evidence.py` and `tests/test_layer_map_preflight.py`.

**Interfaces:** `RuleEvidence.run(task, deck_paths, layer_maps, references)` continues returning its existing result plus explicit physical/derived layer provenance and supported/unknown reasons. A derived expression is not replaced with a same-named physical layer.

- [ ] Add failing fixtures for `GT_A_13` using `GT drawing 30 0`, `INST drawing 60 0`, and `EXCLU = EXCLUi OR MARKS`; assert derived EXCLU is not accepted as direct `EXCLU drawing 132 0` solely by name, and missing or unsupported expression evidence yields `supported=false` with reason and source.
- [ ] Run `pytest -q tests/test_drc_geometry_evidence.py`; verify the new fixture fails for the intended reason.
- [ ] Implement evidence classification and provenance from deck and shared mapping; support only expressions that can actually be evaluated and return unknown otherwise. Preserve hierarchy and exception-region semantics; never invent PDK mappings.
- [ ] Run `pytest -q tests/test_drc_geometry_evidence.py tests/test_drc_geometry_integration.py tests/test_drc_geometry_review_fixes.py`; require passing results. Inspect an actual `find_rule` output for exact file, line, units and unresolved expressions.

## Task 4: Checkpointed review of tool failures and unknown outcomes

**Files:** Modify `FAGENT/engine/fagent_state.py`, `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/graph_builder.py`, `FAGENT/engine/graph_runner.py`, `FAGENT/tasks/drc/tools.py`, `FAGENT/tasks/drc/prompts/repair.md`; extend `tests/test_drc_review_followup.py` and `tests/test_drc_replanning.py`.

**Interfaces:** Store `tool_review` with tool name, call id, task id when present, arguments, error or result, candidate identity and plan position. Review decision object has `action` in `continue`, `skip_task`, `stop`, plus `reason` for `skip_task`; `continue` returns control to the agent with failure evidence and does not repeat the failed call automatically.

- [ ] Add failing tests for exception, explicit `status=unknown`, empty proposal with unsupported reason, and valid non-error tool result. Assert first three interrupt before another model/tool call, preserve plan/history, and include original arguments and reason. Include `update_checklist(work='')` and whitespace-only input; correct schema/description must prevent blank values or report validation failure through review.
- [ ] Add failing restart/resume tests for each action, invalid decision, and a successful mutating call followed by interruption: preserve checkpoint and candidate identity, do not replay the mutation, `skip_task` records unresolved with reason, and `stop` ends incomplete. Ensure no task is marked pass merely because a call finished.
- [ ] Run `pytest -q tests/test_drc_review_followup.py tests/test_drc_replanning.py`; confirm new tests fail before implementation.
- [ ] Route classified tool outcomes to a dedicated interrupt after tool execution has been checkpointed. Resume through the existing graph runner/checkpointer, validate feedback, retain original trace and append review/resume events. Do not turn all empty arrays into failures; classify documented unsupported/unknown proposal results explicitly.
- [ ] Align checklist tool schema and repair prompt: optional `work`, nonempty when provided; supply a clear error for invalid input. Preserve tool results and tool-call IDs across interrupt/resume.
- [ ] Run `pytest -q tests/test_drc_review_followup.py tests/test_drc_replanning.py tests/test_drc_report_checklist.py tests/test_drc_plan_progress.py`; require all tests to pass.

## Task 5: End-to-end boundary and artifact verification

**Files:** Extend `tests/test_flow_boundaries_trace.py`, `tests/test_drc_geometry_integration.py`; update `FAGENT/tasks/README.md` with input formats and review behavior.

**Interfaces:** CLI accepts the existing `--setup` and `--config`; use the existing review provider and checkpoint resume entry points, extending CLI only if needed for resuming an existing run. Preflight and review diagnostics must be present in the appropriate console/trace artifacts without overwriting previous runs.

- [ ] Add an integration test using temporary YAML, GDS, deck and both map formats: assert preflight precedes preparation; an unknown measurement pauses, a skip decision is recorded, and resume retains previous plan and checklist. Separately assert unsupported format stops before any workflow/model call.
- [ ] Run `pytest -q tests/test_layer_map_preflight.py tests/test_input_preflight.py tests/test_drc_geometry_evidence.py tests/test_drc_geometry_integration.py tests/test_drc_review_followup.py tests/test_drc_replanning.py tests/test_flow_boundaries_trace.py`; require all passing.
- [ ] Run `pytest -q`; record exit status and any unrelated failures. If configured models and DRC inputs are available, run a representative flow using a temporary setup/output directory and the real CLI, never `./cmd`; inspect exit status, validation report, `trace.log`, `conversation.json`, `plan.md`, checkpoint and any GDS artifact. Report separately whether real DRC verification was run; do not claim DRC clean without its evidence.
- [ ] Review changed files and user-owned modifications with `git diff`; do not commit or stage pre-existing changes without authorization.
