# DRC Marker Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let workflow code process one DRC marker at a time using a state-backed queue and current GDS context.

**Architecture:** Task-specific read actions populate a common queue and an all-items checklist. The graph processes one item at a time, retains the complete conversation with task attribution, and advances on success or an item-local failure. The candidate GDS is shared across items; the current item's context is refreshed from the live candidate after successful edits.

**Tech Stack:** Python, LangGraph, JSON Schema, gdstk, pytest.

**Spec:** Requirements agreed in the brainstorming conversation; no separate spec file was produced. The detailed decisions below are the implementation contract.

## Global Constraints

- Each DRC marker occurrence is one queue item; preserve report order and duplicate marker occurrences.
- The workflow controls scheduling. Model history remains shared and ordered, with task attribution.
- Read GDS in the read node; the model need not call `read_gds`.
- Preserve the original GDS and write edits to the existing candidate path.
- Apply existing CAD/EDA coding rules and derive cell names, scales and paths from inputs.

## Representative Input and Output

Input: rule `CT_EN` has markers 101 and 102, and rule `GT_A` has marker 201. Queue order is `[task-1, task-2, task-3]`. If task-2 cannot be processed, expected output is task-2 `unresolved` with a reason, followed by execution of task-3. The final checklist retains all three tasks.

To inspect a report section during a real run: `grep -n -A 12 'CT_EN' "$DRC_REPORT"`. Obtain `DRC_REPORT` from the configured run input; marker coordinates in the report are DBU and layout points use GDS user units.

## Review Focus

- Duplicate marker IDs within one rule preserve occurrence identity and ordering.
- A count without geometry produces an unresolved item and the queue advances.
- A changed candidate is visible to the next marker while earlier conversation remains attributable.
- Resume after interruption preserves the active item and does not replay a completed edit.
- An item error or iteration limit records its reason, advances once, and leaves an incomplete final result.

---

### Task 1: Sequential read actions

**Files:** Modify `FAGENT/knowledge/templates/schemas/workflow.json`, `FAGENT/engine/workflow_loader.py`, `FAGENT/engine/graph_nodes.py`, `FAGENT/tasks/drc/workflow.yaml`. Create `tests/test_workflow_action_sequence.py` if the test directory is absent.

**Interfaces:** A node `action` accepts either its existing string form or a nonempty list of task-local action references. Each action receives `TaskContext`; updates from an action are visible to the next action and merged into node state. Agent, plan and review retain their existing string forms.

- [ ] Add tests for scalar compatibility, ordered execution and state propagation, and rejection of an empty list or mixed agent/list actions.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_workflow_action_sequence.py` and confirm the new list case fails.
- [ ] Extend schema, loader and node execution for ordered action lists. Configure DRC read actions as `steps:read_drc` followed by `steps:read_gds`.
- [ ] Run the same command and confirm all tests pass.

### Task 2: Build queue and initial GDS context

**Files:** Modify `FAGENT/tasks/drc/steps.py`, `FAGENT/tasks/drc/checklist.py`, `FAGENT/engine/fagent_state.py`. Create `tests/test_drc_marker_queue.py`.

**Interfaces:** `read_drc(context) -> dict` returns the existing checklist plus `task_queue: list[str]` in report order and `current_task_id: str | None`. `read_gds(context) -> dict` reads the configured target cell and returns `drc_context` containing its GDS data. Each task context is selected by `task_id` and includes its marker, rule, DBU scale, GDS user-unit geometry and candidate identity. Use existing layout APIs and configured top cell.

- [ ] Add tests for two markers, duplicate IDs with distinct occurrences, missing geometry, empty report, and ordered `read_drc`/`read_gds` state propagation.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_drc_marker_queue.py` and confirm missing queue/GDS behavior fails.
- [ ] Implement queue creation and `read_gds`; keep all checklist items including unresolved ones. Do not enqueue items already resolved by read-time rules; record missing geometry as unresolved.
- [ ] Run the same command and confirm all tests pass. Verify persisted checklist uses the format accepted by checkpoint restoration.

### Task 3: Queue routing and task-local failures

**Files:** Modify `FAGENT/engine/graph_builder.py`, `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/fagent_state.py`, `FAGENT/engine/graph_runner.py`. Create `tests/test_queue_routing.py`.

**Interfaces:** State contains `task_queue`, `current_task_id`, and existing checklist. Graph code selects one item, runs configured downstream nodes, records a terminal result, removes that item once, resets its iteration count and advances. Empty queue finishes. Successful processing is distinct from DRC validation; failed or unsupported items become `unresolved` with a reason.

- [ ] Add graph tests for ordered processing, empty queue, failed tool, invalid model response, per-item iteration limit, unresolved reporting, and continuation to the next item. Include restart from an interrupted checkpoint with no replay of an already completed mutation.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_queue_routing.py` and confirm routing cases fail.
- [ ] Implement item selection, completion and failure routing with checkpointed state. Preserve global failures for conditions that prevent the run itself from continuing. Remove reliance on model decisions to advance the queue.
- [ ] Run the same command and confirm all tests pass.

### Task 4: Task-attributed history and candidate refresh

**Files:** Modify `FAGENT/engine/graph_nodes.py`, `FAGENT/tasks/drc/tools.py`, `FAGENT/tasks/drc/prompts/repair.md`, `FAGENT/tasks/drc/workflow.yaml`. Create `tests/test_drc_queue_context.py`.

**Interfaces:** Agent requests receive the active task and ordered `task_history` entries with sequence, task ID, status and reason, together with the existing ordered assistant/tool messages. Tool results update active-task context. After a successful candidate edit, refresh the relevant cell geometry and candidate digest from the live layout before subsequent model requests and queue items. Keep tool-call IDs intact.

- [ ] Add tests showing two tasks retain attributed history, task two receives task one's updated geometry, and an unchanged or failed edit does not falsely refresh a candidate. Include real marker DBU to user-unit conversion using the report scale.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_drc_queue_context.py` and confirm isolation/refresh cases fail.
- [ ] Implement context refresh and history attribution. Remove `read_gds` from DRC agent tools and align the repair prompt with preloaded context. Preserve the existing candidate checkpoint and digest behavior.
- [ ] Run the same command and confirm all tests pass.

### Task 5: End-to-end verification

**Files:** Create `tests/test_drc_queue_integration.py` using temporary inputs and output directories.

- [ ] Exercise a report containing at least two marker occurrences and a small GDS. Script model responses so one item makes a real polygon change and the other fails. Assert queue exhaustion, both checklist results, candidate file existence, correct polygon geometry, no repeated edit on resume, and an incomplete overall outcome for the unresolved item.
- [ ] Run `PYTHONPATH=. pytest -q tests/test_workflow_action_sequence.py tests/test_drc_marker_queue.py tests/test_queue_routing.py tests/test_drc_queue_context.py tests/test_drc_queue_integration.py` and require exit code 0.
- [ ] Run `git diff --check` and inspect the generated `checklist.json`, `candidate.gds`, `conversation.json` and checkpoint in the integration test. Run the configured real DRC backend only when available; distinguish local geometry checks from deck-level verification and report unavailable backend explicitly.
