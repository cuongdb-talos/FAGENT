# Clean Task Model Requests Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each opted-in task a clean, task-owned model request while preserving the existing graph topology and execution flow.

**Architecture:** Add an optional task-local request builder binding to the existing workflow loader and call it at the existing agent request assembly boundary. The builder constructs task-specific system text and user data; the existing engine owns tool history, tool specifications, schema, trace, queue and routing. Existing workflows without a builder retain their current behavior.

**Tech Stack:** Python, YAML, JSON Schema, pytest, existing LangGraph workflow and model adapters.

**Spec:** User-approved discussion in this conversation: one focused DRC payload per marker; clean requests for other task types; no graph structure changes. The example is representative, not evidence that every inferred field is available.

## Global Constraints

- Do not change graph nodes, edges, routing, checkpoint state structure, or queue semantics.
- Preserve existing uncommitted workspace changes; do not commit unrelated files.
- Derive paths, layer mappings, units, grid, goal and constraints from actual inputs and configuration; never hard-code the example cell or rule.
- Keep full trace and current-task assistant/tool pairs; never send orphan tool messages.
- Treat markers as evidence, not automatically as source polygons; preserve uncertainty, hierarchy and geometry provenance.
- A local assessment is not a fresh Calibre DRC pass; do not claim verified without supported validation.
- Keep existing public workflow behavior unless the optional request builder is configured.

## Review Focus

- Invalid builder reference or signature: loader rejects it before execution (Task 1).
- A non-opted-in task: existing request and schema remain unchanged (Task 1).
- Two markers with identical rule and ID: use the exact occurrence and current candidate (Task 2).
- Missing or ambiguous rule, layer, grid, or hierarchy: report unavailable/incomplete; do not invent values (Task 2).
- Multiple tool calls and transition to a new marker: preserve paired results only for the active marker (Task 3).

## File Map

- Modify `FAGENT/knowledge/templates/schemas/workflow.json` and `FAGENT/engine/workflow_loader.py`: validate, bind and hash an optional task-local `request_builder: module:function` using the existing local callable rules.
- Modify `FAGENT/engine/graph_nodes.py`: invoke the optional builder only at the existing request construction boundary, retaining the legacy path when absent.
- Create `FAGENT/tasks/drc/model_payload.py`: construct the DRC system text and user payload from focused evidence.
- Modify `FAGENT/tasks/drc/workflow.yaml` and `FAGENT/tasks/drc/prompts/repair.md`: opt DRC in and align its instructions with the new payload.
- Modify focused context, workflow loader and queue integration tests; update `FAGENT/tasks/README.md` with the extension contract.

### Task 1: Optional task-local request builder

**Files:** Modify `FAGENT/knowledge/templates/schemas/workflow.json`, `FAGENT/engine/workflow_loader.py`, `FAGENT/engine/graph_nodes.py`; test existing loader tests and add `tests/test_task_request_builder.py`.

**Interfaces:** `request_builder: model_payload:build_request` is an optional workflow-level reference. Bound callable `build_request(state: dict, node_prompt: str, services: object) -> dict` returns exactly `{'system': str, 'user': dict}`. The engine serializes `user` with `json.dumps`, constructs the initial system/user messages, and appends paired current-item assistant/tool messages in their original order. Builder errors fail the request explicitly; no silent legacy fallback. A workflow without the binding uses the existing path unchanged.

- [ ] Add tests loading a temporary task with a valid builder, missing module, invalid signature and changed builder source; verify binding, rejection and workflow digest changes.
- [ ] Add tests proving an opted-in request contains one system message, one JSON-encoded initial user message, and configured tools/schema; prove an unconfigured task retains its pre-change request.
- [ ] Run `pytest tests/test_task_request_builder.py -q` and confirm the new tests fail for the missing contract.
- [ ] Implement schema validation and task-local binding with the loader's existing path, source hashing and signature checks; call the builder within `build_model_request` without changing graph construction or state.
- [ ] Run `pytest tests/test_task_request_builder.py -q` and the repository's existing workflow loader tests; require all to pass.

### Task 2: DRC request payload

**Files:** Create `FAGENT/tasks/drc/model_payload.py`; modify `FAGENT/tasks/drc/workflow.yaml`, `FAGENT/tasks/drc/prompts/repair.md`; test `tests/test_drc_focused_context.py` and `tests/test_task_request_builder.py`.

**Interfaces:** The DRC builder uses the existing `GraphNodes.build_active_task_context(state, task_id)` result via the existing request assembly boundary; do not reconstruct marker association independently. If needed, pass this prepared focused context as an explicit argument by updating the builder contract and tests consistently. It returns user keys `task_id`, `cell`, `unit`, `grid`, `violation`, `layer_map`, `layout`, `task_history`. Preserve evidence status, candidate digest, hierarchy/path incompleteness and exact polygon points where available; do not drop information needed for safe edits. Use null or explicit unavailable diagnostics where evidence cannot establish a requested value. Do not invent rule type, requirement, exceptions, relations, or polygon identity from a bounding box.

**Representative input:** task `task-3`, cell `ROM_MCELL0`, rule `GT_A_13`, marker `[-0.09, 0.21, 0.31, 0.27]`, goal limited to M1 and CT.

**Expected output:** one system message with the configured goal and execution instructions; one user JSON string containing `task_id: task-3`, the matching marker and supported rule/layout evidence; no plan, full report, unrelated checklist, or previous marker conversation. If GT is reliably identified as the target layer, the existing checklist flow can record `skipped`; otherwise represent uncertainty and do not guess the scope.

- [ ] Add tests for exact marker occurrence, actual GDS grid and units, unsupported or ambiguous layer/rule evidence, nonrectangular polygons, incomplete hierarchy, empty history and stale candidate. Assert the sample's metrics only when supported by source geometry.
- [ ] Run `pytest tests/test_drc_focused_context.py tests/test_task_request_builder.py -q` and confirm payload assertions fail before implementation.
- [ ] Implement the DRC builder, opt in via workflow, and align system prompt with direct user fields. Preserve necessary source provenance and use configuration for goal/constraints. Avoid duplicating the prompt in user content.
- [ ] Run the same tests and require all to pass. Inspect the source rule with `grep -n -A 12 'GT_A_13' DRC/1P7M_6Ic_1TKMc_ALPA2.drc` when that deck is available; the configured deck remains authoritative.

### Task 3: Multi-turn isolation and verification

**Files:** Modify `tests/test_drc_queue_context.py`, `tests/test_drc_queue_integration.py`, `FAGENT/tasks/README.md`; add targeted tests as needed.

**Interfaces:** First request for each marker contains only its system and user messages. Subsequent requests contain that same focused context followed by complete assistant/tool pairs for this marker. Tool outputs and trace retain their existing schema and correlation IDs. A new marker starts with refreshed candidate evidence and no prior marker tool messages.

- [ ] Add a scripted integration test with two marker tasks and at least two tool calls: capture actual arguments to the chat client and matching `model_request` trace events; assert paired tool IDs, no cross-task leakage, new candidate digest and configured tool/schema fidelity.
- [ ] Test an unsupported or missing builder and a non-DRC workflow without an agent; do not assume the current LVS workflow invokes a model.
- [ ] Run `pytest tests/test_task_request_builder.py tests/test_drc_focused_context.py tests/test_drc_queue_context.py tests/test_drc_queue_integration.py -q`; require success and inspect any existing failures without deleting user changes.
- [ ] Document how another task opts in, its builder contract, JSON-string content, unavailable evidence, conversation history and backward-compatible default.
- [ ] Run applicable existing DRC geometry, tool and workflow tests. Record command, exit status and results. If Calibre is available and configured, run a representative real flow and inspect conversation and DRC artifacts; otherwise explicitly report that no real DRC validation was performed.
- [ ] Inspect `git diff` for only intended edits and preserve unrelated workspace changes. Do not commit unless explicitly requested.
