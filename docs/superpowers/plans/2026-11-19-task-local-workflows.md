# Task-local Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run task-local workflows and agent tools through a common graph, with an opt-in live execution trace.

**Architecture:** The setup selects a task package; its workflow references local Python functions directly. A shared loader validates definitions, a shared graph executes workflow and agent steps, and a trace observer reports runtime events. Shared environment configuration, checkpoints, review, and DRC validation safeguards remain in place.

**Tech Stack:** Python >=3.10, PyYAML, jsonschema, LangGraph, SQLite checkpointer, pytest, existing llama.cpp adapter.

**Spec:** `docs/superpowers/specs/2026-11-19-task-local-workflows-design.md`

## Global Constraints

- Work on `main` as requested; preserve all existing user changes, including staged changes. Do not reset, clean, or stage unrelated files.
- Keep shared ENV in `FAGENT/config.json` (or the existing `--config` override); do not copy environment settings per task.
- `env_setup.yaml` remains the source of selected task and per-run inputs; retain current DRC setup compatibility.
- No central task list, action allowlist, task-specific branch in the generic graph, or hard-coded project paths.
- Keep candidate integrity checks, validation provenance, explicit storage consent, review interrupts, and checkpoint workflow identity. A simulated pass must remain labeled simulated.
- Functions are trusted project Python code; containment checks constrain references, not Python execution.
- Main `run()` methods appear first in classes; avoid nested function definitions. Keep computed outputs on instances where existing project conventions require it.
- No production GDS migration implementation or new real EDA backend in this scope.
- Do not commit or stage pre-existing user changes. Any optional commits require a clean, explicitly selected path set and must not capture unrelated work.

## Review Focus

1. Malicious task or module paths and symlink escapes must fail before import (Task 1).
2. A tool name collision, invalid schema, or malformed call must fail without invoking the callable (Tasks 1 and 3).
3. A changed candidate or checkpoint identity must not be resumed (Task 4).
4. Repeated agent loops, conditional routes, and interrupts must display the actual route in order without payloads (Task 5).
5. Installed packages must contain task assets and expose both CLI entry points (Task 6).

## Target Tree

```text
FAGENT/
├── __main__.py                    # python -m FAGENT entry point
├── fagent.py                      # shared CLI and setup dispatch
├── config.json                    # shared ENV; user-local values retained
├── config.example.json            # shared ENV example
├── engine/
│   ├── fagent_state.py            # serializable graph state
│   ├── workflow_loader.py         # task discovery, YAML and reference validation
│   ├── task_contracts.py          # context, workflow definitions, tool metadata
│   ├── graph_builder.py           # generic graph compilation and routing
│   ├── graph_nodes.py             # shared step, agent, tool and review execution
│   ├── graph_runner.py            # invoke, checkpoint, review and artifacts
│   ├── graph_services.py          # shared model, RAG, layout and EDA services
│   └── trace.py                   # event formatter and live observer
├── tasks/
│   ├── __init__.py
│   ├── drc/
│   │   ├── __init__.py
│   │   ├── workflow.yaml
│   │   ├── steps.py               # DRC read / repair-related validation
│   │   ├── tools.py               # read_gds / replace_polygon and metadata
│   │   ├── prompts/drc.md
│   │   └── schemas/drc.json
│   └── lvs/
│       ├── __init__.py
│       ├── workflow.yaml
│       ├── steps.py               # existing LVS placeholder behavior
│       ├── tools.py               # task-owned tool location; initially empty
│       ├── prompts/lvs.md
│       └── schemas/lvs.json
├── knowledge/templates/
│   ├── prompts/{protocol,review,drc_site}.md  # common templates
│   ├── schemas/{workflow,review}.json           # shared definition schemas
│   └── reports/drc_summary.txt
├── capabilities/                  # existing GDS parsing and layout operations
├── integrations/                  # existing model and EDA adapters
└── rag/                           # existing retrieval and knowledge storage
tests/
├── test_task_loader.py
├── test_task_execution.py
├── test_task_regression.py
├── test_trace.py
├── test_package.py
└── fixtures/tasks/migrate_gds/   # test-only task; not a production migration
    ├── __init__.py
    ├── workflow.yaml
    ├── steps.py
    ├── tools.py
    ├── prompts/agent.md
    └── schemas/response.json
```

`FAGENT/workflows/*.yaml`, the DRC tool JSON and task-specific prompt/schema files migrate to their task directories; remove the old files only after their references and package data have been updated. Shared protocol/review assets remain shared. Do not overwrite or delete uncommitted user content during migration. `migrate_gds` is a test fixture, not an advertised production feature.

## Interfaces and Examples

Setup remains compatible:

```yaml
tasks:
  - drc:
      drc_folder: DRC
      drc_files: DRC/rules.drc
      prompt: Fix DRC errors.
```

Representative test task setup and workflow:

```yaml
# setup.yaml
tasks:
  - migrate_gds:
      input_gds: input/cell.gds
      output_gds: output/cell.gds
```

```yaml
# tasks/migrate_gds/workflow.yaml
task: migrate_gds
version: 1
inputs: [input_gds, output_gds]
prompts: [prompts/agent.md]
schema: schemas/response.json
steps:
  - {id: read, action: 'steps:read_gds', next: migrate}
  - id: migrate
    action: agent
    tools: ['tools:inspect_layer']
    next: validate
  - {id: validate, action: 'steps:validate_gds', next: review}
  - {id: review, action: review, next: END, continue: migrate}
```

`TaskContext` is an immutable dataclass containing `task: str`, `inputs: dict`, `state: dict`, and `services: GraphServices`. A normal step has signature `function(context: TaskContext) -> dict` and returns serializable state updates. A tool has signature `function(context: TaskContext, **arguments) -> dict`; a `@tool(description: str, parameters: dict)` decorator stores metadata on that function, with an object JSON Schema for `arguments`. `context` and services are never sent to the model. A validated workflow contains resolved callables, tool specifications, task-local prompt/schema contents and a digest of workflow, callable source and referenced assets; no callable or service is serialized into state. For unchanged DRC/LVS behavior, preserve existing configuration inputs and review settings in the workflow definition.

`agent` and `review` are shared framework node kinds, not task registrations. `next` is the success route; `continue` is required for review and points to the retry step. Tool errors are returned to the model as tool error messages; a step exception or exhausted agent iteration limit results in an incomplete/failed run, with no invented validation pass. A review `unclear` repeats review; `stop` follows `next`. The graph retains a bounded recursion limit.

```text
# [task] migrate_gds
## [node] read -> pending:
##### read: running
##### read: completed
## [node] read -> migrate:
## [node] migrate -> pending:
##### migrate: running
#### [tools] inspect_layer:
##### migrate: completed
## [node] migrate -> validate:
```

The second node line reports the real next node after routing; it does not expose payloads. Errors are reported independently of `--verbose` and must not dump model messages or input data.

## Task 1: Task contracts and validated task discovery

**Files:** Create `FAGENT/engine/task_contracts.py`, `FAGENT/tasks/__init__.py`, `tests/test_task_loader.py`, and test fixture files in `tests/fixtures/tasks/migrate_gds/`; modify `FAGENT/engine/workflow_loader.py` and `FAGENT/knowledge/templates/schemas/workflow.json`.

**Interfaces:** `TaskContext(task, inputs, state, services)`; `tool(description: str, parameters: dict)`; `WorkflowLoader(path: Path, task_root: Path | None = None).run()` returns a workflow object with `task`, `version`, `inputs`, `steps`, `digest`, prompt/schema text and resolved functions/tool specs. `task_root` defaults to `FAGENT/tasks` and is injectable for tests.

- [ ] Add tests `test_loads_task_local_step_and_tool`, `test_invalid_task_or_module_path_rejected_before_import`, `test_symlink_escape_rejected`, `test_duplicate_ids_and_tool_names_rejected`, `test_missing_function_and_bad_tool_schema_rejected`, `test_invalid_route_and_signature_rejected`, and `test_digest_changes_when_callable_source_changes`. Assertions inspect resolved callables, required schema and task/step/tool error context; use a marker file to prove rejected modules were not imported.
- [ ] Run `python -m pytest -q tests/test_task_loader.py`; expect failure because the new interface is absent.
- [ ] Implement contracts, metadata decorator and loader. Accept task names matching `^[a-z][a-z0-9_]*$`; resolve paths and reject absolute paths, `..`, cross-task imports, and symlinks escaping task root. Validate JSON schemas with `Draft202012Validator`, prohibit external references, require unique ids and valid `next`/`continue` targets, check signatures and tool argument schemas. Hash workflow text, referenced assets and Python source bytes in deterministic order.
- [ ] Run `python -m pytest -q tests/test_task_loader.py`; expect all tests to pass.

## Task 2: Generic step graph and task dispatch

**Files:** Modify `FAGENT/fagent.py`, `FAGENT/engine/graph_builder.py`, `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/fagent_state.py`; create `tests/test_task_execution.py`.

**Interfaces:** `GraphBuilder(services, checkpointer).run()` returns a compiled graph; `GraphNodes.execute_step(context: TaskContext, step) -> dict` invokes a resolved normal-step callable. `Fagent.load_task_workflow(task_name, task_cfg, base)` resolves the task directory and loads the workflow; task inputs flow to `TaskContext.inputs`.

- [ ] Add `test_fixture_task_runs_in_declared_order`, `test_step_state_updates_reach_next_step`, `test_new_task_needs_no_engine_registration`, `test_failed_step_never_claims_success`, and `test_unknown_task_and_missing_input_report_context`. Fixture runs `read -> validate -> END` without a model and asserts returned state and order.
- [ ] Run `python -m pytest -q tests/test_task_execution.py`; expect failure for the old hard-coded dispatch/graph.
- [ ] Replace fixed task and action dispatch with task discovery and workflow-driven graph construction. Pass task input to initial state; validate required inputs. Wrap step exceptions into an incomplete result with task/step identity. Keep `StateGraph` nodes and state updates serializable, and preserve SQLite checkpointer integration. Do not require DRC-specific cell lists or layer maps for an unrelated task; retain their existing DRC behavior via task-local input preparation.
- [ ] Run `python -m pytest -q tests/test_task_loader.py tests/test_task_execution.py`; expect pass.

## Task 3: Shared agent loop and task-local tools

**Files:** Modify `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/graph_builder.py`, `FAGENT/engine/graph_services.py`, `FAGENT/integrations/models/llama_adapter.py` only if required; extend `tests/test_task_execution.py` and fixture tools.

**Interfaces:** `GraphNodes.execute_agent(context, step) -> dict`; `GraphNodes.execute_tools(context, step) -> dict`; the adapter continues `chat(messages, tools, response_schema)`. Each agent step uses only tools it declares; tool metadata produces the existing model tool spec shape.

- [ ] Add `test_agent_calls_task_tool_and_receives_result`, `test_tool_arguments_validated_before_invocation`, `test_undeclared_tool_is_rejected`, `test_tool_failure_is_returned_to_agent`, `test_iteration_limit_is_incomplete`, and `test_multiple_agent_steps_use_their_own_tools`. Use a fake model and assert call order, messages, and no callable invocation for invalid arguments.
- [ ] Run `python -m pytest -q tests/test_task_execution.py`; expect the new agent cases to fail.
- [ ] Implement generic agent/tool cycle and route to the declared next step after final structured output. Preserve tool IDs, schema validation, conversation updates and max iteration handling. No task-name condition in graph construction or generic tool dispatch.
- [ ] Run `python -m pytest -q tests/test_task_execution.py tests/test_task_loader.py`; expect pass.

## Task 4: Migrate DRC/LVS and preserve review and evidence

**Files:** Create the `FAGENT/tasks/drc/` and `FAGENT/tasks/lvs/` files in the target tree; modify `FAGENT/engine/graph_services.py`, `FAGENT/engine/graph_runner.py`, `FAGENT/fagent.py`, `FAGENT/config.example.json`; create `tests/test_task_regression.py`. Move existing task assets only after verifying their contents and all references.

**Interfaces:** DRC task tools use `TaskContext` to access layout services. `replace_polygon` preserves inspect-before-edit and saves a candidate plus digest through task-specific logic. `GraphRunner.resume(feedback)` retains identity and candidate checks. Review `continue` routes to the workflow's configured target.

- [ ] Add `test_drc_inspection_before_edit_and_candidate_digest`, `test_drc_validation_preserves_simulated_provenance`, `test_lvs_placeholder_and_mock_are_labeled`, `test_review_continue_stop_unclear_and_consent`, `test_resume_rejects_changed_identity_or_candidate`, and `test_current_drc_setup_per_cell`. Assert no pass is manufactured on missing/failed evidence and no knowledge storage occurs without consent.
- [ ] Run `python -m pytest -q tests/test_task_regression.py`; expect migration tests to fail before implementation.
- [ ] Move DRC/LVS step handlers, tool metadata, prompts, response schemas and workflow definitions into their task packages; preserve common review/protocol assets and existing prompt configuration. Replace old task-specific loader lists and runner branches with the shared contracts. Keep checkpoint outputs, candidate file, output-path safety, RAG and JSON artifact behavior.
- [ ] Run `python -m pytest -q tests/test_task_loader.py tests/test_task_execution.py tests/test_task_regression.py`; expect pass. Compare old and new simulated DRC/LVS results and review states using controlled fixtures; record any intentional difference.

## Task 5: Live verbose trace

**Files:** Create `FAGENT/engine/trace.py`, `tests/test_trace.py`; modify `FAGENT/fagent.py`, `FAGENT/engine/graph_runner.py`, and graph node/routing code.

**Interfaces:** `Trace(enabled: bool, stream: TextIO)` exposes `task(name)`, `node_start(node, step)`, `tool(name)`, `node_end(node, step, status, next_node=None)`. Events are emitted at execution points, including before tool invocation and after route selection; `waiting` is emitted at an interrupt.

- [ ] Add `test_verbose_event_order_and_actual_next`, `test_loop_and_multiple_tools_emit_each_visit`, `test_waiting_and_failed_status`, `test_no_trace_without_flag`, and `test_trace_excludes_payloads`. Capture stdout/stderr and assert pending then actual next, exact node/step/tool format, no secret sentinel, and error visibility even without verbose.
- [ ] Run `python -m pytest -q tests/test_trace.py`; expect failure until CLI and trace exist.
- [ ] Add `--verbose` (default false), propagate its value rather than fixed `verbose=True`, and implement live event reporting in the agreed format. Ensure existing progress/think prints do not leak payloads or appear in quiet mode; preserve interactive review prompt separately.
- [ ] Run `python -m pytest -q tests/test_trace.py tests/test_task_execution.py tests/test_task_regression.py`; expect pass.

## Task 6: Packaging, documentation and final verification

**Files:** Modify `pyproject.toml`, `README.md`, `env_setup.yaml` only if a compatibility example is needed; create `tests/test_package.py`. Remove superseded task assets only after checking all references.

**Interfaces:** `python -m FAGENT` and installed `fagent` use `FAGENT.fagent:Fagent.main`; installed distributions contain task YAML, prompts and schemas as package data.

- [ ] Add `test_installed_package_loads_task_assets` and `test_both_cli_entry_points_show_verbose_help`; install a locally built wheel into an isolated temporary environment with available dependencies, and verify both entry points and DRC/LVS task asset resolution. The existing `pyproject.toml` points at missing `FAGENT.fagent_app`, so update that entry point.
- [ ] Run `python -m pytest -q tests/test_package.py`; expect failure before package data and entry point changes.
- [ ] Update package data, CLI entry point and README with task tree, function metadata, workflow and setup examples, shared ENV, trace format, error handling and simulator limitations. Retain real EDA paths in user configuration, not code.
- [ ] Run `python -m pytest -q tests`; expect pass. Run `python -m FAGENT --help` and installed `fagent --help`; both must list `--verbose`. Run `rg -n 'workflows/|knowledge/templates/(tools/drc|prompts/(drc|lvs)|schemas/(drc|lvs))|fagent_app' FAGENT README.md pyproject.toml` and inspect every remaining reference for an intentional compatibility use.
- [ ] Run `git diff --check` and inspect `git status --short`; preserve pre-existing changes. Record commands and outcomes, including any unavailable dependencies.
- [ ] If a configured real Calibre environment is available and the user has supplied suitable decks, run the existing supported validation path and inspect process exit status and generated reports. Otherwise explicitly report that real EDA execution was not verified; simulated tests are not evidence of a real pass.

## Spec Navigation

```bash
grep -nE '^## (Organization|Execution|Verbose|Verification)' docs/superpowers/specs/2026-11-19-task-local-workflows-design.md
```
