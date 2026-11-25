# DRC Focused Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the model the active DRC marker, its rule evidence, and relevant candidate GDS geometry in a compact, traceable request.

**Architecture:** Keep the full report, layout and conversation in state and trace. Build a task-scoped model request from the current marker and a bounded geometry query, with explicit completeness and provenance; provide a tool to query additional geometry. Rebuild context from the live candidate after each successful edit and retain ordered, task-attributed history.

**Tech Stack:** Python, LangGraph, gdstk, Shapely, JSON Schema, pytest.

**Spec:** The focused DRC/GDS context and processing approach approved in the preceding conversation; no separate spec document exists.

## Global Constraints

- One queue item corresponds to one marker occurrence; preserve rule, violation ID and occurrence identity.
- DRC marker coordinates are DBU; GDS points and query bounds are GDS user units; convert using the report scale.
- Never infer layer mapping, rule thresholds or connectivity. Return unavailable or ambiguous evidence explicitly.
- Preserve full report, full layout, ordered messages and tool-call IDs in checkpoint state and trace.
- Keep original GDS unchanged and bind context to the current candidate digest.
- Follow project CAD/EDA coding rules, including `run()` first in each class and no nested functions.

## Representative Input and Output

Marker `[[1000, 2000], [1100, 2100]]` DBU with `dbu_per_user_unit=1000` becomes `[[1.0, 2.0], [1.1, 2.1]]` user units. Given a configured margin of 0.1 user units, the search bounds are `[0.9, 1.9, 1.2, 2.2]`. Output includes both intersecting CT and M1 polygons and every other polygon in that region, with stable IDs and explicit layer/datatype. If two CT polygons intersect, include both and mark the match ambiguous.

To find source rule evidence in a configured rule deck: `grep -n -A 12 'CT_EN_7a_7b' "$DRC_DECK"`. The deck path and scale come from run inputs; this is an example rule, not a hard-coded project value.

## Review Focus

- Missing or invalid marker geometry or DBU scale produces an explicit unavailable context, without invented bounds.
- Missing or conflicting layer mapping preserves physical layer/datatype and marks semantic mapping unknown.
- Multiple polygon candidates and shapes on other physical layers remain visible within the region.
- Overlapping or unbounded references and paths are flagged as incomplete geometry evidence.
- After candidate replacement or checkpoint resume, all geometry and digests refer to the current live layout.

## File Structure

- `FAGENT/capabilities/layout/violation_geometry.py`: reusable marker region and candidate geometry query.
- `FAGENT/tasks/drc/tools.py`: model-callable bounded geometry query and existing rule evidence integration.
- `FAGENT/engine/graph_nodes.py`: build per-item request and compact model-facing history while preserving stored messages.
- `FAGENT/tasks/drc/steps.py`: retain full report and GDS state without injecting a full report into model history.
- `FAGENT/tasks/drc/workflow.yaml`, `FAGENT/tasks/drc/prompts/repair.md`: enable and document the query tool.
- `tests/`: regression tests for region selection, context assembly, candidate refresh and end-to-end model requests.

---

### Task 1: Bounded geometry query

**Files:** Modify `FAGENT/capabilities/layout/violation_geometry.py`; create `tests/test_drc_context_region.py`.

**Interfaces:** `ViolationGeometry.run(...)` retains its existing matching contract. Add `query_region(self, layout, cell_name: str, bounds_user: list[float], candidate_digest: str | None, layer_specs: list[dict] | None = None) -> dict`. Return `cell`, `bounds_user`, `candidate_digest`, `coordinate_units`, `polygons`, `references_in_region`, `paths_in_region`, `complete`, and `reason`. Each polygon includes stable `id`, `layer`, `datatype`, `points`, and `editable`. No layer filter means all direct physical layers.

- [ ] Write tests for all overlapping polygons including two on one layer and an unrelated layer, optional physical-layer filters, empty region, invalid bounds, overlapping references, unknown reference bounds and paths. Assert stable IDs and explicit incompleteness for unsupported hierarchy.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_drc_context_region.py` and verify the new query tests fail.
- [ ] Implement `query_region` using live layout geometry and conservative hierarchy detection. Keep all candidate polygons; do not reuse the single-match rejection as the query result.
- [ ] Run the same command and require exit code 0.

### Task 2: Active marker context

**Files:** Modify `FAGENT/engine/graph_nodes.py`, `FAGENT/tasks/drc/steps.py`; create `tests/test_drc_focused_context.py`.

**Interfaces:** `GraphNodes.build_active_task_context(state, task_id: str) -> dict` returns `task`, `drc` and `layout` matching the approved example, plus explicit evidence status/reason. `layout.search_region` contains `bounds_user` and `margin_user`. Derive the initial nonnegative margin from `drc_context_margin_dbu` in service configuration, defaulting to zero, then convert with the report scale. Resolve `rule + violation_id + occurrence` exactly. Include rule text and mapping only with their source or explicit unavailable status.

- [ ] Add tests for exact duplicate occurrence selection, DBU conversion, marker of one/two/multiple points, configured margin, missing marker/scale/rule/deck/mapping and ambiguous mappings. Assert all candidates in region appear and no other task's marker is presented as active.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_drc_focused_context.py` and confirm focused-context assertions fail.
- [ ] Build the focused context using Task 1 and the existing rule evidence service. Retain complete report and layout in state, rather than appending the full report as an initial model message. Include references and paths conservatively when their region cannot be established.
- [ ] Run the same command and require exit code 0.

### Task 3: Model requests and history

**Files:** Modify `FAGENT/engine/graph_nodes.py`, `FAGENT/tasks/drc/prompts/repair.md`; extend `tests/test_drc_focused_context.py`.

**Interfaces:** Each model request includes `active_task`, goal, constraints and ordered `task_history` with sequence, task ID, terminal status and reason. Preserve all messages in state/trace. For a model request, include the full assistant/tool pairs belonging to the current item and the task summaries for completed items. Never leave an assistant tool call without its corresponding tool result. Keep shared system instructions and current task context.

- [ ] Add tests with two queue items asserting no full DRC report or checklist is sent, current-item tool pairs remain ordered, prior item summaries remain attributed, and full prior messages remain in state and trace. Include an interrupted tool call so correlation is preserved.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_drc_focused_context.py` and confirm request assertions fail.
- [ ] Implement model-facing message selection without mutating stored history. Update repair instructions to use active marker context and request additional geometry when needed.
- [ ] Run the same command and require exit code 0.

### Task 4: Query additional GDS geometry

**Files:** Modify `FAGENT/tasks/drc/tools.py`, `FAGENT/tasks/drc/workflow.yaml`, `FAGENT/tasks/drc/prompts/repair.md`; create `tests/test_drc_query_region_tool.py`.

**Interfaces:** `query_layout_region(context, cell_name: str, bounds_user: list[float], layers: list[dict] | None = None) -> ToolResult` delegates to Task 1. Each layer filter contains integer `layer` and `datatype`. The tool reports the current candidate digest and completeness, and does not edit state or geometry. `bounds_user` is `[min_x, min_y, max_x, max_y]`.

- [ ] Test a larger requested region, physical layer filters, malformed bounds, unknown cell, overlapping hierarchy and candidate digest binding.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_drc_query_region_tool.py` and confirm missing-tool tests fail.
- [ ] Register the tool with a strict parameter schema, enable it in DRC workflow, and document that it expands evidence for the current task. Do not change the existing `find_related_polygons` contract.
- [ ] Run the same command and require exit code 0.

### Task 5: Candidate refresh and end-to-end verification

**Files:** Modify `FAGENT/engine/graph_nodes.py` as needed; extend `tests/test_drc_queue_integration.py`.

- [ ] Add a two-marker integration test with a real temporary GDS: model sees marker one region, edits a polygon, then marker two receives geometry and digest from the saved candidate. Assert full conversation remains in trace, tool pairs correlate, no unrelated full report is sent, ambiguous candidates survive filtering, and queue failure advances.
- [ ] Add a checkpoint resume case and a failed write case; assert the model never receives a stale digest or claims an edit was saved when checkpointing fails.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_drc_context_region.py tests/test_drc_focused_context.py tests/test_drc_query_region_tool.py tests/test_drc_queue_integration.py` and require exit code 0.
- [ ] Run `git diff --check` and inspect generated candidate GDS, checklist, conversation trace and checkpoint. Run the configured DRC backend if available and distinguish deck-level verification from local geometry checks.
