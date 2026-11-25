# DRC Five Tool Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair supported cover and area violations with deterministic geometry proposals selected by the model, then save the verified candidate GDS.

**Architecture:** Read the existing DRC report and GDS, queue one marker per task, and supply rule evidence and candidate geometry to the repair agent. Five tools generate and apply proposals, check geometry, and save; workflow code owns progress, rollback, and status. The agreed conversation design is recorded under Global Constraints below; no separate spec exists.

**Tech Stack:** Python, existing GDS layout service and geometry library, YAML workflow, pytest.

**Spec:** Approved conversation design in Global Constraints; user requested this plan directly.

## Global Constraints

- Skip density; do not invoke a DRC engine or claim a deck-level pass.
- Each queue item is one marker; retain ordered task history and preload layout in `read`.
- The model selects polygon IDs and chooses proposals using rule and GDS evidence; geometry code computes coordinates.
- Cover requires every selected group to cover the inner polygon; polygons within a group act as a union. Represent M1 AND (AA OR GT) as one M1 group and one group containing the chosen AA and/or GT polygons; reject unsupported interpretations.
- Area and enclosure constraints derive from rule evidence; coordinates use GDS user units, area uses squared user units, precision comes from the GDS grid.
- Preserve previously verified edits; rollback the current task on failed check. Unsupported or ambiguous tasks become unresolved and leave the queue; attempts are bounded by configuration.
- Save once after queue completion, including when unresolved tasks remain. Report local geometry verification separately from full DRC validation.
- Expose only five model tools; remove obsolete DRC tool registrations and handlers after checking references, preserving shared internals still in use.

## Review Focus

- Multiple bound polygons jointly cover target but no individual polygon does: task 1 tests union.
- Missing or ambiguous rule, layer, polygon, exception, or incomplete geometry: task 2 tests unresolved without edit.
- Stale proposal or no-op edit: task 2 tests rejection and unchanged candidate.
- A later edit or save fails after multiple polygons were modified: task 2 tests atomic rollback and retains prior verified edits.
- Empty queue and unresolved tasks: task 3 tests one save and accurate output status.

## File Structure and Data Flow

- Modify `FAGENT/capabilities/layout/repair_proposals.py` and `cover_check.py`: deterministic proposal generation and geometry checks.
- Modify `FAGENT/tasks/drc/tools.py`: register the five model tools and expose typed interfaces.
- Modify `FAGENT/tasks/drc/steps.py`, `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/graph_runner.py`, and `FAGENT/engine/fagent_state.py` as needed: task lifecycle, transactions, queue, save routing.
- Modify `FAGENT/tasks/drc/prompts/repair.md` and `FAGENT/tasks/drc/workflow.yaml`: consistent instructions and tool availability.
- Tests: `tests/test_drc_five_tool_geometry.py`, `tests/test_drc_five_tool_transactions.py`, `tests/test_drc_five_tool_flow.py`.
- Source data: configured `.drc.results`, rule decks, layer maps, GDS and goal. Generated artifacts: run-scoped checklist, trace, proposals, candidate and final GDS. Resolve paths via existing configuration; do not embed project-specific paths.
- Locate representative rule text with `grep -n -A 4 'GT_A_13\|CT_R_11' DRC/1P7M_6Ic_1TKMc_ALPA2.drc`; deck is evidence only, not executed.

## Interfaces and Examples

`fix_cover(task_id: str, action: str, inner_id: str | None = None, groups: list[list[str]] | None = None, proposal_id: str | None = None) -> ToolResult` and `fix_area(task_id: str, action: str, polygon_id: str | None = None, minimum_area: float | None = None, proposal_id: str | None = None) -> ToolResult`: `action` is `propose` or `apply`; proposals contain ID, candidate digest, affected IDs, before/after points, rule assumptions and local check. Apply uses only a staged ID for the current task and candidate digest.

`check_cover(task_id: str) -> ToolResult` and `check_area(task_id: str) -> ToolResult` check the staged transaction against its bound inputs and return `pass`, `fail`, or `unknown`, measurements and candidate digest. `save_gds() -> ToolResult` returns output path, digest, and task summary; only available after queue completion.

Example: `groups=[["M1:1", "M1:2"], ["AA:1"]]` means `(M1:1 union M1:2) covers CT AND AA:1 covers CT`. For `GT_A_13`, `minimum_area=0.038` square user units; a rectangular `0.4 × 0.06` polygon has area `0.024` and must not pass unchanged. Proposal output example: `{"proposal_id":"p1","candidate_digest":"...","changes":[{"polygon_id":"GT:1","after_points_user":[...]}]}`. A successful local check returns `{"status":"pass","deck_clean":false}`.

### Task 1: Geometry proposals and checks

**Files:** Modify `FAGENT/capabilities/layout/repair_proposals.py`, `FAGENT/capabilities/layout/cover_check.py`; Test `tests/test_drc_five_tool_geometry.py`.

**Interfaces:** Geometry helpers consume resolved direct polygons, groups, rule limits and GDS grid; return deterministic candidate point lists and measurements in user units.

- [ ] Write failing tests: group union covers target; each group must cover; holes and concave shapes are respected; minimum enclosure comes from rule; area `0.024 < 0.038` fails, while grid-aligned candidate `>= 0.038` passes; unsupported geometry returns unknown.
- [ ] Run `pytest -q tests/test_drc_five_tool_geometry.py`; expect failure for missing interfaces.
- [ ] Implement deterministic candidate generation for supported geometry, grid snapping, actual polygon boolean coverage and area; return unknown when an exact safe operation cannot be supported. Do not invent an exception interpretation.
- [ ] Run `pytest -q tests/test_drc_five_tool_geometry.py`; expect all tests pass.

### Task 2: Five tool interfaces and transactional edits

**Files:** Modify `FAGENT/tasks/drc/tools.py`, `FAGENT/capabilities/layout/layout_service.py`, `FAGENT/engine/fagent_state.py`; Test `tests/test_drc_five_tool_transactions.py`.

**Interfaces:** Implement the five signatures above using `ToolSpec` and `ToolResult`; proposal store and transaction snapshot are state-owned and task-scoped.

- [ ] Write failing tests for model-selected IDs, unknown IDs, missing mapping, unsupported exceptions, stale digest, same-geometry no-op, apply of a multi-polygon proposal, failed check rollback of all edited polygons, and candidate save failure preserving earlier verified edits.
- [ ] Run `pytest -q tests/test_drc_five_tool_transactions.py`; expect failure for missing tools.
- [ ] Implement tool schemas and handlers, staged proposal registry, candidate digest validation, atomic multi-polygon transaction and rollback, and local evidence. Reuse existing checkpoint and polygon identity services. Remove obsolete DRC tool registrations and handlers only after checking all references; preserve internal helpers required elsewhere.
- [ ] Run `pytest -q tests/test_drc_five_tool_transactions.py`; expect all tests pass.

### Task 3: Queue, save, prompt and workflow integration

**Files:** Modify `FAGENT/tasks/drc/steps.py`, `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/graph_runner.py`, `FAGENT/tasks/drc/prompts/repair.md`, `FAGENT/tasks/drc/workflow.yaml`; Test `tests/test_drc_five_tool_flow.py`.

**Interfaces:** Existing `read_drc` and `read_gds` initialize one-marker queue and candidate context. The repair agent receives the five named tool schemas; queue control records result and evidence; save is invoked once when queue is empty.

- [ ] Write failing integration tests for density skip, supported cover and area task, unresolved ambiguous task, bounded retries, no-op rejection, failed-check rollback, empty queue, and unresolved-plus-successful tasks saved exactly once. Assert no DRC backend invocation and no unverified `pass`.
- [ ] Run `pytest -q tests/test_drc_five_tool_flow.py`; expect failure with current workflow.
- [ ] Update queue routing and runner so an agent completion alone cannot mark a task passed; only a successful check tied to the applied candidate can do so. Route queue completion to save even with unresolved tasks, and make final save independent of existing review authorization. Record failures and preserve original GDS.
- [ ] Update `repair.md` to describe evidence, five tool signatures, proposal selection, uncertainty and local-check limits. Update `workflow.yaml` to expose only the five tools and route queue completion to save; keep `read_drc` and `read_gds` in read. Ensure save is available when no active task remains.
- [ ] Run `pytest -q tests/test_drc_five_tool_flow.py tests/test_drc_marker_queue.py tests/test_queue_routing.py tests/test_drc_queue_integration.py`; expect pass.
- [ ] Run an end-to-end flow with configured input paths and a deterministic model stub; inspect exit code, run trace, checklist and saved GDS. Reopen GDS and verify polygon geometry against local checks, unresolved status and digest. This is a real GDS round trip, not a DRC deck run.

### Completion

- [ ] Run `pytest -q tests/test_drc_five_tool_geometry.py tests/test_drc_five_tool_transactions.py tests/test_drc_five_tool_flow.py` and relevant existing queue tests; record commands and outcomes.
- [ ] Check git diff, preserve unrelated workspace changes, and commit only task files if requested.
