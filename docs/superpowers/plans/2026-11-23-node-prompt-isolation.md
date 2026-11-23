# DRC Node Prompt Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send goal and constraints as the system message, and only the active DRC node's role instructions to the model.

**Architecture:** Construct the DRC system message after context assembly, from the configured goal and constraints. Build node-local request messages without persisting the plan instruction as conversation history; retain DRC context, model plan response, checklist, tool calls and tool results. Supply the existing workflow and custom repair instructions plus protocol only to the repair node; preserve other task workflows' behavior.

**Tech Stack:** Python, LangGraph, JSON Schema, pytest, llama.cpp adapter.

**Scope and sources:** User-approved in-chat design; `FAGENT/tasks/drc/workflow.yaml`, task prompts, `FAGENT/engine/graph_services.py`, `FAGENT/engine/graph_nodes.py`. Locate prompt definitions with `grep -n 'planning agent\|repair agent\|protocol\|prompt' FAGENT/tasks/drc/prompts/*.md FAGENT/tasks/drc/workflow.yaml`.

## Constraints

- The system message contains only goal and constraints; do not place planning, repair, or protocol instructions in it.
- Plan request contains the plan prompt and no repair prompt; repair requests contain the repair prompt and no plan prompt, including subsequent tool cycles.
- Preserve report, references, checklist, assistant plan, tool call/result correlation, schemas, and user-configured repair prompt.
- No hard-coded project, rule, pin or cell values; preserve existing generic and non-DRC workflows.
- No changes to DRC validation, tool safety guards or checklist semantics.
- Replay alone does not establish that prompt isolation fixes model quality.

## Review focus

- Goal absent or constraints empty: system content reflects actual configuration without fabricated values.
- Constraints present and changing per run: plan and repair receive the same current goal and constraints.
- Repair loops and resume: retain history and valid call/result pairing without retaining plan instructions.
- Site prompt and protocol: repair receives them; plan and system do not.
- Non-DRC workflows: their existing prompt configuration remains functional.

## Representative request

Input: goal `Fix geometry`, constraints `["Preserve connectivity"]`, DRC context and distinct `PLAN ROLE`/`REPAIR ROLE` prompts.

Expected plan request: system has goal and constraints; user context has DRC/RAG; user instruction has `PLAN ROLE`, never `REPAIR ROLE`.

Expected repair request: same system and context plus plan result and checklist; node instruction has `REPAIR ROLE`, never `PLAN ROLE`. Subsequent calls preserve tool exchanges and the repair instruction.

## Task 1: Tests for actual node requests

**Files:** `tests/test_drc_context_flow.py` or a new focused test module.

- [ ] Add an integration test using the real DRC workflow and graph with an external model stub, capturing plan and multiple repair requests.
- [ ] Assert system content consists only of the configured goal and constraints; assert mutually exclusive plan/repair prompt text and preserve report, RAG references, plan response, checklist and tool call/result pairing.
- [ ] Add empty constraints, custom repair prompt, resume/history and non-DRC compatibility cases.
- [ ] Run the new tests and record the expected failures against current code.

## Task 2: Compose scoped prompts

**Files:** `FAGENT/engine/graph_services.py`, `FAGENT/engine/graph_nodes.py`, `FAGENT/tasks/drc/steps.py`, and workflow configuration only if needed.

- [ ] Build the DRC system message from the actual goal and constraints after they have been loaded; preserve original behavior for other tasks.
- [ ] Create the plan request with the node-local planning instruction, and persist only the shared conversation plus plan response so its prompt does not enter repair history.
- [ ] Add the configured repair and custom site instructions and protocol to every repair request as a node-local message, without duplicating them in stored history.
- [ ] Keep model/tool schemas, trace recording, checkpoint data and ordering consistent.
- [ ] Run targeted tests and verify they pass.

## Task 3: Regression and real model comparison

**Files:** modified files and existing run artifact; temporary replay files under `/tmp`.

- [ ] Run `python -m pytest -q` and `git diff --check`; inspect changed Python files for class ordering and nested definitions.
- [ ] Replay the same recorded model input through the configured adapter before and after prompt isolation when compute resources permit; retain raw outputs, token usage and finish reason. Report model quality separately from deterministic request-assembly tests.
- [ ] Report that actual Calibre validation was not run unless independently executed; inspect trace messages to demonstrate the requested node isolation.

## Existing diagnostic evidence

An exact replay of request 1 using the configured model and schema returned `{"summary": ": ", "tasks": []}`, with `finish_reason=stop`, 4,964 prompt tokens and 12 completion tokens. The unschematized comparison was still running at plan creation; do not infer its result.
