# DRC Planner Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the DRC planner actionable task and tool context and reject invalid plans.

**Architecture:** Classify goal-excluded rules using configured deck evidence while building the checklist. Associate active tasks with report geometry, derive tool descriptions from the next workflow agent, and validate each response against a per-request schema.

**Tech Stack:** Python 3.10+, JSON Schema, pytest, LangGraph, RuleEvidence and ToolSpec.

**Spec:** The user's requirements and discussion immediately preceding this plan. No separate specification file exists.

## Global Constraints

- Derive rule names, layers, paths, geometry, thresholds and tools from configuration and inputs.
- Preserve DBU coordinates, marker ordering and occurrence identity.
- Keep existing user changes. Apply TDD and the repository's CAD/EDA coding rules.
- New classes put `run()` first, store results on `self`, and contain no nested functions.

## Review Focus

- Missing or ambiguous deck evidence keeps a task active and identifies the uncertainty.
- Duplicate rule names and violation IDs preserve distinct occurrences.
- Missing or non-agent next nodes fail clearly before a model request.
- Invalid tool names, task IDs and empty active plans yield incomplete results.
- A clean or fully excluded checklist makes no planner model call.

## File Structure

- Modify `FAGENT/tasks/drc/steps.py` and `FAGENT/tasks/drc/checklist.py` for evidence-based exclusions.
- Reuse exact block extraction and provenance in `FAGENT/tasks/drc/rule_evidence.py`.
- Modify `FAGENT/engine/graph_nodes.py` for planner context and response validation.
- Modify `FAGENT/tasks/drc/prompts/plan.md` for planning instructions.
- Extend `tests/test_drc_replanning.py` and the existing checklist tests.

Representative input contains a pending task `task-4`, rule `CT_EN_7a_7b`, violation ID 1, occurrence 0, and coordinates in DBU. An expected response is `{"summary":"Inspect rule","steps":[{"tool":"find_rule","purpose":"Read the rule and layers","task_id":"task-4"}]}`. Production identifiers come from the inputs and configured workflow.

---

### Task 1: Resolve excluded rules from evidence

**Files:** Modify `FAGENT/tasks/drc/steps.py` and `FAGENT/tasks/drc/checklist.py`. Extend the existing checklist tests and `tests/test_drc_replanning.py`.

**Interfaces:** `Steps.read_drc(context)` consumes the report, goal and `context.services.input_sources['decks']`. Extend `Checklist.run(report, proposed_tasks=None, goal='', rule_evidence=None)` with an optional mapping of exact rule name to extracted evidence. Calls without evidence retain their current behavior.

- [x] **Step 1: Write failing tests.** Use a temporary deck containing an arbitrarily named `DENSITY` rule and a geometry rule. An explicit density exclusion skips only the density task and records deck path and line range. Missing blocks, unavailable decks, ambiguous duplicate definitions and the word density appearing only in a comment keep tasks active. A goal without density exclusion also keeps both active. Preserve existing exact-name exclusion behavior.
- [x] **Step 2: Run the affected pytest nodes and confirm failure on the new evidence assertions.**
- [x] **Step 3: Implement evidence collection and exclusion.** Use configured deck paths and exact rule identities. Identify executable `DENSITY` operations after removing comments. Require unambiguous evidence before skipping, record uncertainty in the task reason, and preserve IDs and occurrences. Persist the checklist and its digest through the existing path.
- [x] **Step 4: Run the affected pytest nodes and confirm they pass.**

### Task 2: Build planner context and validate plans

**Files:** Modify `FAGENT/engine/graph_nodes.py` and `FAGENT/tasks/drc/prompts/plan.md`. Test `tests/test_drc_replanning.py` and `tests/test_drc_node_prompt_isolation.py`.

**Interfaces:** `GraphNodes.execute_plan(context, step)` consumes `services.workflow.steps`, `step.next`, `step.schema`, checklist, report and references. It produces a plan or existing incomplete result, and records the actual request using `services.chat(messages, [], response_schema=...)`.

- [x] **Step 1: Write failing tests.** Capture the actual graph request. Assert `active_tasks` associates each active task ID with its rule, violation ID, occurrence, marker type and DBU points, including two occurrences sharing a rule and violation ID. Assert `allowed_tools` contains the next agent's configured tool names and descriptions, and the chat tools argument remains empty. Assert skipped tasks are absent and an entirely skipped checklist avoids the planner call. Test missing and ambiguous marker associations without borrowing another marker.
- [x] **Step 2: Run the targeted tests and confirm failure on missing context.**
- [x] **Step 3: Implement the payload.** Resolve `step.next` against loaded steps and require an agent with tools. Match markers by exact rule, violation ID and occurrence in report order. Preserve source, units, references, replan context, candidate digest and prior plan. Include a concise example in the prompt and instruct the model to use supplied task IDs and tools, investigate before proposing edits, and return empty steps only when no active task remains.
- [x] **Step 4: Write failing validation tests.** Assert the request schema restricts tool names and task IDs to the next agent and active checklist, and requires a step when active tasks exist. Test fabricated tools, stale task IDs and empty steps with a stub client that bypasses adapter validation. Confirm no-active tasks produce an empty plan without an empty enum.
- [x] **Step 5: Run validation tests and confirm failure.**
- [x] **Step 6: Deep-copy the configured schema per request and add restrictions.** Validate the response against that same schema and return an informative incomplete result for invalid plans. Keep the shared schema and repair prompt unchanged.
- [x] **Step 7: Run `pytest -q tests/test_drc_replanning.py tests/test_drc_node_prompt_isolation.py` and confirm all pass.**

### Task 3: Verify the planner boundary

**Files:** Use a temporary directory for generated requests and traces.

**Interfaces:** Replay the newly recorded `model_request` through `replay_model.py --file REQUEST`, using the configured backend. The event contains messages, tools, response schema and request ID.

- [x] **Step 1: Inspect the deck and prompt.** With a configured deck path in `$DRC_DECK`, run `grep -n -i 'density' "$DRC_DECK"` and compare executable rule blocks and source lines with exclusion evidence. Run `grep -n 'allowed_tools\|active_tasks' FAGENT/tasks/drc/prompts/plan.md`.
- [x] **Step 2: Run regression tests.** Run `pytest -q tests/test_drc_replanning.py tests/test_drc_node_prompt_isolation.py tests/test_conversation_snapshots.py` and the checklist tests used in Task 1. Record exit status and failures.
- [x] **Step 3: Exercise real inference.** Generate a new plan request through the updated graph using configured inputs, export its trace event, and run `python replay_model.py --file REQUEST`. Record exit status and response. Check task and tool membership and at least one step for active tasks. Distinguish a valid plan from verified DRC repair.
- [x] **Step 4: Verify coding rules and artifacts.** Inspect modified Python code for nested definitions, class entry points and stored outputs. Run `git diff --check`. Compare trace request, checklist statuses and deck source lines. Run an applicable deck validator if available and record its command and result. Report unavailable validators or tools, and distinguish static tests from an actual DRC run.

## Execution results

- The focused planner, checklist, context and trace suite passed all 48 tests. The full suite passed 84 tests and reproduced the same 9 geometry and transaction failures found in the pre-change baseline.
- The recorded DRC report produced 8 active tasks and 2 density exclusions with exact deck line ranges. The first local replay returned whitespace for the summary and purpose. A schema regression test now rejects this response.
- A subsequent replay of the updated request stopped at 192 generated tokens and the adapter reported a truncated response. This run exercised inference and validation. Calibre was unavailable in this environment.
- A diagnostic replay with context 8192, default KV precision, temperature 0, and a 256-token budget produced one valid step. It repeated the prompt example and covered only one of the eight active tasks.
