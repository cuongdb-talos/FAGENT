# DRC Plan Progress Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do not dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the repair model explicit plan progress and maintain a human-readable `plan.md` containing the status and result of every planned tool step across replans.

**Architecture:** LangGraph state is the source of truth for the current plan and its execution history. Tool dispatch records progress and output summaries without enforcing tool order; `plan.md` is regenerated atomically from state, including previous plan versions. Repair receives the current plan and progress in its message. The DRC violation checklist remains separate in state.

**Tech Stack:** Python, LangGraph, JSON Schema, pytest, Markdown.

**Spec:** User-approved decisions in the conversation: tool execution success is separate from DRC outcome; preserve prior plans in the same file; match the first unfinished step with the same tool and, when supplied, matching `task_id`; model chooses tools without code enforcing plan order.

## Global Constraints

- Answer user questions in Vietnamese; write the plan in English. No project-specific cell, rule, file path, or layer constants in production code.
- Do not modify the existing DRC checklist meaning, tool interfaces, schema returned by the planning model, or existing routing except where specified.
- No nested functions; place `run()` first in new classes and store class outputs on the instance.
- Preserve existing work in the dirty working tree. Do not commit without the user's instruction.
- A completed tool step does not imply a DRC pass; unsupported and violation results must remain visible.

## Review Focus

- Several identical tools in one plan or a call with no task identifier: preserve order and apply the matching rule (Task 1).
- `ToolResult` without an exception reports unsupported geometry or an active violation: record `done` and its separate outcome (Task 1).
- A raised exception or schema validation error and a subsequent successful replan: keep the failed step and old plan (Tasks 1 and 3).
- Multiple tool calls in one assistant message, including unplanned calls: update only matched steps and preserve results (Task 1).
- An interrupted or failed replan and resumed state: preserve the last known plan artifact, do not manufacture completion, and regenerate from checkpoint state (Tasks 3 and 4).

## Data Contract and Example

The planner returns the existing `state['plan'] = {'summary': str, 'steps': [{'tool': str, 'purpose': str, 'task_id': str}]}`. Add `state['plan_history']`, an ordered list of version records. Each version contains `version` (one-based integer), `summary`, `status` (`active` or `superseded`), and `steps`; each step copies the planner fields and adds `step_id` (one-based within its version), `status` (`pending`, `done`, `failed`), `call_id` (string or null), and `outcome` (JSON object or null). `state['plan']` remains the latest planner output for compatibility. Completed means the call returned successfully and its result was serializable; `outcome` records any returned `status`, `reason`, `supported`, or `found` fields, or a concise success indication when none exist. Never claim deck-level DRC success from a step status.

Matching uses only the active version: select the first `pending` step with the same tool name. If the call has a `task_id`, require the step's `task_id` to match; if there is no `task_id`, do not filter by task. A call with no matching step changes no progress. Process a batch sequentially; a call that raises becomes `failed` and retains its error. A later call cannot consume a failed step. Record unplanned calls in the existing conversation trace, not as fabricated plan steps. Matching does not prevent model tool calls, reorder them, or infer that a task passed.

Example input: a plan has `read_checklist(task-3)` then `find_rule(task-3)`. A successful `read_checklist({})` marks only the first step `done`; `find_rule({'task_id': 'task-3'})` returning `{'supported': false, 'reason': 'No rule deck'}` marks the second step `done` with that outcome. The DRC checklist remains unchanged unless the tool updates it.

Representative artifact:

```markdown
# DRC plan

## Plan 1 (superseded)
1. [x] **read_checklist** — Review current task statuses. (task: task-3; call: call-1)
   - Outcome: completed
2. [!] **find_rule** — Find rule. (task: task-3; call: call-2)
   - Error: rule lookup failed

## Plan 2 (active)
1. [x] **find_rule** — Find rule. (task: task-3; call: call-3)
   - Outcome: supported=false; reason=No rule deck
2. [ ] **update_checklist** — Record unresolved result. (task: task-3)
```

Use `[x]` for `done`, `[!]` for `failed`, and `[ ]` for `pending`. Version sections are chronological. Escape or safely render newlines in model-supplied text without changing the recorded JSON state.

Relevant code and prompt sections:

```bash
grep -nE 'write_plan_markdown|execute_plan|execute_agent|execute_tools|route_task_tools' FAGENT/engine/graph_nodes.py
grep -nE 'read_checklist|update_checklist|example|plan' FAGENT/tasks/drc/prompts/{plan,repair}.md
```

## Task 1: Track individual tool calls in state

**Files:** Modify `FAGENT/engine/fagent_state.py`, `FAGENT/engine/graph_nodes.py`; create `tests/test_drc_plan_progress.py`.

**Interfaces:** `plan_history: list[dict[str, Any]]` is serializable graph state. `execute_plan()` creates version records; `execute_tools()` returns the updated history with its existing state updates and messages. Add small module-level helpers if needed; no nested definitions.

- [ ] Write focused tests for first matching pending step, task ID discrimination, no-task `read_checklist({})`, duplicate tool steps, out-of-plan calls, a multi-call batch, and a failed call. Verify serializable state and no mutation of the incoming state.
- [ ] Add tests showing `{'status': 'violation'}` and `{'supported': False, 'reason': 'unsupported'}` both mark the tool step `done` while preserving the respective outcome, not a DRC `pass`.
- [ ] Run `python -m pytest tests/test_drc_plan_progress.py -q` and confirm the new tests fail for missing progress tracking rather than setup errors.
- [ ] Implement the state contract and matching algorithm above. Capture the call ID, tool result fields and error text without placing full GDS geometry in plan state. Preserve existing `ToolResult.updates`, tool messages, trace events, and failure routing.
- [ ] Run `python -m pytest tests/test_drc_plan_progress.py tests/test_drc_replanning.py -q` and confirm the progress and existing replan tests pass.

## Task 2: Provide progress to repair and improve planner prompts

**Files:** Modify `FAGENT/engine/graph_nodes.py`, `FAGENT/tasks/drc/prompts/plan.md`, `FAGENT/tasks/drc/prompts/repair.md`; extend `tests/test_drc_plan_progress.py` and `tests/test_drc_node_prompt_isolation.py`.

**Interfaces:** The repair request retains `plan` and adds `plan_progress`, containing the active history version and its steps. The planner's output schema remains unchanged.

- [ ] Add a graph-level test that a successful `read_checklist` is followed by a repair request containing `plan_progress` with step 1 `done` and step 2 `pending`, plus the original plan and tool result. Test an unplanned call leaves the next planned step pending.
- [ ] Run the new tests and confirm failure because progress is not in the model request.
- [ ] Pass active progress on every repair request. The prompt directs the model to inspect current progress and choose the next applicable step, to treat `read_checklist` as a global call, and to use the latest plan after replanning. The model still chooses tools; code does not force it to follow the plan.
- [ ] Remove the four-step planner example that was copied in the observed run. Ask the planner to cover each active task, include checklist updates and supported diagnostic/measurement steps, and explicitly account for missing references. Do not invent geometry edits or DRC evidence.
- [ ] Run `python -m pytest tests/test_drc_plan_progress.py tests/test_drc_node_prompt_isolation.py tests/test_drc_context_flow.py -q`.

## Task 3: Persist versioned `plan.md` without losing previous plans

**Files:** Modify `FAGENT/engine/graph_nodes.py`; extend `tests/test_drc_plan_progress.py` and `tests/test_drc_review_followup.py`.

**Interfaces:** `write_plan_markdown(rundir, plan_history, check_output_path)` renders all version records and uses the existing checked output path and temporary-file replacement pattern. Update its callers and tests together.

- [ ] Add tests that actual `plan.md` has `[ ]`, `[x]`, `[!]`, task IDs, tool result/error, and two chronological versions after failure and successful replan. Verify unsupported output is not displayed as a DRC pass.
- [ ] Add a test that replan model failure does not replace the prior version with an empty “planning in progress” document. Keep the old active version until a new plan has been successfully parsed and validated.
- [ ] Run these tests and confirm failures reflect missing versioned progress rendering.
- [ ] Render the current state after plan creation, after each tool batch, and after successful replan. Mark the old version `superseded` only when the new version is valid; write the whole history atomically. On plan failure, retain the existing file and failure trace. Ensure the no-active-tasks path is represented consistently.
- [ ] Run `python -m pytest tests/test_drc_plan_progress.py tests/test_drc_review_followup.py tests/test_drc_replanning.py -q`.

## Task 4: Verify end-to-end behavior and recovery

**Files:** Extend `tests/test_drc_plan_progress.py`; modify `FAGENT/engine/graph_runner.py` only if necessary to regenerate the derived artifact when resuming a saved checkpoint.

**Interfaces:** Checkpoint state, not Markdown, is authoritative. Existing `conversation.json` and `trace.log` remain the detailed audit of tool input/output; `plan.md` summarizes progress.

- [ ] Add an integration test with a scripted client: plan, successful `read_checklist`, next request with progress, `update_checklist`, failed `find_rule`, replan, and successful unsupported result. Assert both model input and on-disk Markdown versions, checklist state, tool-call correlation, and failure routing.
- [ ] Test resume or reconstruction from serialized checkpoint state, including missing or stale `plan.md`; the regenerated document must match the checkpoint history. Do not claim that a tool ran merely because an artifact exists.
- [ ] Run `python -m pytest tests/test_drc_plan_progress.py tests/test_drc_replanning.py tests/test_drc_node_prompt_isolation.py tests/test_drc_context_flow.py tests/test_drc_review_followup.py -q`, then run `python -m pytest -q`. Report all existing or new failures by name.
- [ ] If the configured model and DRC inputs are available, run a representative real flow in a new run directory and inspect `conversation.json`, `trace.log`, and `plan.md`: verify first `read_checklist` is reflected as done before the next request and record any model repetition. Do not claim the prompt guarantees model compliance. No SPICE deck validation is implied by this DRC-only test.
- [ ] Check the diff for unintended files, hard-coded project values, nested functions, and changed user work. Report test and live-run limitations explicitly.
