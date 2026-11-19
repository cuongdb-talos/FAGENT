# Configurable DRC and LVS Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the duplicate Agent_caller loop with LangGraph orchestration, and configure DRC and LVS tasks, prompts and task-specific definitions in files rather than embedding them in engine code.

**Architecture:** A shared LangGraph runner loads a validated task definition from disk and selects registered capability implementations. Workflow files reference task prompts, tool selection, workflow order, schemas and settings; Python implements reusable operations and adapters. DRC follows read log → repair → rerun DRC tool → review; LVS initially provides only the workflow skeleton and explicitly simulated tool step; real report parsing and tool integration await a concrete contract and sample logs. The engine cannot execute arbitrary Python specified in configuration.

**Tech Stack:** Python >=3.10, LangGraph, LangChain Core, Pydantic, YAML, llama.cpp, SQLite, pytest.

**Spec:** User instructions in this conversation (2026-11-19). Inspect existing DRC behavior with `grep -nE 'Calibre and validation|Interactive Review|Yêu cầu LVS' README.md`.

## LVS Scope and Future Contract

The user requires both existing-report analysis and running an LVS tool, but no LVS log is currently available. Implement the workflow skeleton now: read log → analyze/propose repair → apply repair when a supported capability exists → run LVS tool → review. In local skeleton mode, missing log and unavailable repair capability must be explicit placeholders; they do not imply successful analysis or a layout modification. A configured mock tool returns a clearly labelled simulated pass solely to exercise the flow; overall result remains incomplete. Do not invent a report grammar, layout repair algorithm, tool invocation or actual evidence. Real LVS support requires a tool identity, representative pass/mismatch/failure logs, layout and reference netlist inputs, deck, invocation and pass/fail contract.

## Global Constraints

- Tasks are DRC and LVS only. Do not implement unrelated EDA capabilities.
- Remove `Agent_caller` and the standalone orchestration loop; preserve `python -m FAGENT` and `fagent`, existing DRC input flags, setup mode, load-only, resume, configuration compatibility, knowledge and artifacts.
- Remove current references to obsolete `cli.py`; preserve historical documents.
- Place all system prompts, model-facing task-specific instructions, task-specific tool descriptions, task configuration, workflow sequencing and task-specific response schemas in files. Generic model protocol instructions must also come from a file. Python retains general execution, parsing, validation and tool implementations; these operational algorithms cannot be represented as configuration alone.
- Resolve task assets against the selected task definition, validate paths and references, reject unknown actions and malformed schemas before model/tool invocation. Do not allow arbitrary code or shell commands from task files.
- Interpret user intent to stop through the agent, without regex or fixed feedback patterns. Keep explicit, separately recorded user consent before saving knowledge; stopping or inferred completion does not itself authorize storage. Preserve simulated/real evidence; no DRC or LVS pass without the corresponding validated tool evidence. In local mode the DRC tool step returns a configured simulated pass for workflow testing, explicitly tagged `simulated: true`; it is not a real DRC pass and cannot set overall status `verified`.
- Preserve DBU/user-unit conversion, cell and layer identities, data provenance, configurable tool paths and inputs. Tests must not modify original GDS, netlists, rule decks, knowledge or existing output.
- Do not treat model-inferred intent as permission to save knowledge; preserve human feedback and stop handling without a fixed-pattern review gate. Do not commit unless authorized.

## Review Focus

- Missing task assets, invalid action names and malformed schema fail before execution (Task 1).
- Prompts and tool descriptions come from task files, including after reload (Task 2).
- DRC tool errors, iteration limits and simulated validation cannot produce verified results (Task 3).
- LVS mismatches, missing output and tool failures cannot produce pass (Task 4).
- Resume must bind to the same task definition/version and restore relevant artifacts safely (Task 5).

## Intended Files and Interfaces

```text
FAGENT/
├── __main__.py
├── engine/
│   ├── graph.py          # Build LangGraph from validated workflow
│   ├── runner.py         # Run, checkpoint, resume, artifacts
│   ├── state.py          # Shared state
│   ├── workflow.py       # Load and validate workflow definitions
│   ├── nodes.py          # Execute registered capabilities
│   └── services.py       # Runtime dependencies
├── capabilities/
│   ├── registry.py       # Trusted capability registration
│   ├── layout/           # GDS operations
│   ├── drc/              # DRC parsing and validation
│   └── lvs/              # LVS operations, subject to agreed contract
├── integrations/
│   ├── models/           # llama.cpp and embedding adapters
│   └── eda_tools/        # DRC and LVS tool integrations
├── knowledge/
│   ├── prompts/          # DRC, LVS and shared system/protocol instructions
│   └── ...               # RAG data and references
└── workflows/
    ├── drc_repair.yaml
    ├── lvs.yaml
    ├── tools/            # Task-specific model-facing tool descriptions
    └── schemas/          # Task response schemas
```

`workflows/drc_repair.yaml` and `workflows/lvs.yaml` refer to prompt files in `knowledge/prompts/` and to tool/schema assets in `workflows/`; resolve paths against a configured project asset root and validate them. Do not create `FAGENT/tasks/`. Do not add Liberty or simulation capabilities merely to populate the example structure; preserve the existing DRC simulator integration as needed.

- `FAGENT/engine/workflow.py`: `load_workflow(path: Path) -> WorkflowDefinition`; validate references, registered actions and workflow before running.
- `FAGENT/engine/services.py`: own model, tools, RAG and inputs; consume the workflow without `Agent_caller`.
- `FAGENT/engine/graph.py`, `nodes.py`, `state.py`, `runner.py`: graph building, workflow routing, state and checkpoint lifecycle.
- Move existing `FAGENT/tools/` implementations into corresponding `capabilities/` or `integrations/` locations; update imports. Separate DRC simulation integration from DRC capability. Add an explicit local mock LVS backend; add a real adapter only after agreeing its contract.
- Move `FAGENT/models/` adapters into `integrations/models/`, update imports; retain `FAGENT/rag/` and `knowledge/` initially.
- `FAGENT/__main__.py`: select workflow, default existing invocations to DRC. Delete `FAGENT/agent/caller.py`; migrate necessary remaining `agent/` functionality into `engine/`; remove `agent/prompts.py` and `agent/review.py` after migrating their needed behavior without creating replacement modules.
- `tests/test_task_config.py`, `test_prompt_assets.py`, `test_drc_graph.py`, `test_lvs_graph.py`, `test_main_entry.py`: boundary tests using temporary files and fake models/tools.

Representative DRC input: GDS + Calibre ASCII DRC log/results + layer map. Flow: read existing DRC log → analyze and repair candidate → run configured DRC backend against that candidate → review new results with the user. Local example tool output: `{status: pass, simulated: true}`; output includes candidate GDS, tool log/result, conversation and structured result with overall `incomplete`. Real mode must capture actual invocation, exit status and parsed report; no configured real tool or tool failure is not a pass. LVS skeleton example: no report available → mark analysis/repair unavailable → invoke explicitly configured mock backend → review simulated pass → overall incomplete. Real inputs and expected outputs remain pending an agreed format. Provenance links each artifact to task, source inputs, tool invocation, exit status and report path.

## Task 1: Define and validate file-backed tasks

**Files:** Create `FAGENT/engine/workflow.py`, `FAGENT/workflows/drc_repair.yaml` and `FAGENT/capabilities/registry.py`, `tests/test_task_config.py`.

**Interfaces:** `load_workflow(path: Path) -> WorkflowDefinition`; definition includes task ID, prompt paths under `knowledge/`, selected registered actions, ordered steps, response schema path, input requirements and validation policy.

- [ ] Add tests: valid DRC config loads; absent prompt/schema, duplicate step IDs, unknown actions, traversal outside the allowed task asset root and invalid workflow fail before graph execution.
- [ ] Run `python -m pytest -q tests/test_task_config.py`; confirm the new tests fail before implementation.
- [ ] Implement schema-validated configuration and a fixed trusted action registry. Resolve asset paths against the configured project asset root; ensure prompt files are inside `knowledge/`, and reject cycles and missing dependencies.
- [ ] Run the test again and expect pass.

## Task 2: Externalize prompts and task definitions

**Files:** Modify `FAGENT/engine/services.py`, `FAGENT/integrations/models/llama_adapter.py`, DRC capability tool wrappers, configuration examples; create prompt files in `FAGENT/knowledge/prompts/`, remaining workflow assets, and `tests/test_prompt_assets.py`. Do not create `engine/prompts.py`.

**Interfaces:** `GraphServices.prepare() -> list[dict]` loads prompt text from validated `knowledge/prompts/` paths; model response schema and descriptions come from workflow assets; generic response protocol is a separate file in `knowledge/prompts/`.

- [ ] Add tests with temporary alternative prompt and tool description files: changing either changes the model request; invalid or missing file/schema fails explicitly; RAG is labelled as reference and bounded by existing configuration.
- [ ] Run `python -m pytest -q tests/test_prompt_assets.py` and observe failure before implementation.
- [ ] Move DRC instructions, user-configured system prompt text, tool descriptions, final schema and generic JSON protocol out of Python. Keep RAG selection and schema validation in code. Preserve existing effective prompt and tool-call contract; document migration of `system_prompt` configuration.
- [ ] Run `python -m pytest -q tests/test_prompt_assets.py tests/test_task_config.py`; expect pass. Search `rg -n 'DEFAULT_SYSTEM_PROMPT|Inspect the target cell|DRC repair agent' FAGENT --glob '*.py'`; investigate all remaining executable task text.

## Task 3: Migrate DRC to LangGraph-only orchestration

**Files:** Modify `FAGENT/__main__.py`, `FAGENT/engine/services.py`, `FAGENT/engine/graph.py`, `FAGENT/engine/nodes.py`; delete `FAGENT/agent/caller.py`; create `tests/test_drc_graph.py`.

**Interfaces:** `GraphServices(rundir: Path, config: dict, workflow: WorkflowDefinition, verbose: bool = True)` and `prepare() -> list[dict]`; `GraphRunner.run(messages)` remains supported.

- [ ] Add fake-model tests for file-configured step order: read DRC log → repair → invoke DRC backend on saved candidate → review results, duplicate read handling and tool error/iteration limit. Assert review receives the newly generated DRC report and distinguishes a local simulated pass from a real pass. Assert a simulated result is never verified and no unsafe proposal executes when its schema or prerequisite fails.
- [ ] Run `python -m pytest -q tests/test_drc_graph.py`; confirm missing workflow support fails.
- [ ] Migrate caller's active context filtering, tool protocol and model invocation into services; remove `Agent_caller` and its duplicate loop. Implement DRC step routing from the task definition with deterministic prerequisites: read log, repair, save candidate, invoke configured real or explicit local-simulated DRC backend, parse results and present review. The local backend may return pass only with `simulated: true`; real backend captures executable, candidate, deck, exit status and output paths, and missing configuration, execution errors or missing/malformed output cannot produce pass. If the real tool invocation contract is unavailable, define the interface and report it as blocked rather than inventing a command. Preserve existing DRC input and artifacts.
- [ ] Run task tests and `rg -n 'Agent_caller|agent\.caller|agent/caller' FAGENT`; tests pass and search has no matches.

## Task 4: Create the LVS workflow skeleton

**Files:** Create `FAGENT/workflows/lvs.yaml`, LVS prompt and schema assets under `knowledge/prompts/` and `workflows/`, an explicitly labelled mock backend under `integrations/eda_tools/`, and `tests/test_lvs_graph.py`. Register only the minimal placeholder and mock capabilities needed by the workflow.

**Interfaces:** Workflow steps: read log, analyze/repair, run LVS backend, review. Define the generic backend interface from the engine contract; do not define a real report parser or executable command before the tool contract is available.

- [ ] Write tests for the no-log local flow: mark log analysis and repair unavailable, invoke the mock backend once, present its simulated result for review, and return overall `incomplete` with `simulated: true`. Assert no fabricated candidate or verified LVS pass.
- [ ] Add tests ensuring no report is passed to an invented parser, and missing mock configuration, backend errors or missing output do not produce pass. Verify user stop ends the flow without additional calls.
- [ ] Run `python -m pytest -q tests/test_lvs_graph.py`; confirm failing tests before implementation.
- [ ] Implement file-backed workflow, placeholder steps and explicitly configured mock pass. Keep existing-report and real-execution modes visibly unavailable until the actual LVS input, tool and output contracts are supplied; report this in artifacts and documentation.
- [ ] Run the LVS tests and expect pass. Once representative real logs and tool invocation are supplied, extend this plan and implement/test the real adapter separately.

## Task 5: Checkpoint, compatibility, and documentation

**Files:** Modify `FAGENT/engine/runner.py`, `FAGENT/engine/state.py`, `FAGENT/engine/nodes.py`, `FAGENT/__main__.py`, `README.md`, active operational docs; create `tests/test_main_entry.py`.

**Interfaces:** State persists task identity, task definition version/digest, artifacts and results. Resume rejects mismatched definitions and restores the relevant candidate through a capability-specific restore handler.

- [ ] Add tests for DRC default and explicit task selection, setup, load-only, invalid inputs, resume with matching definition, rejection on task/definition mismatch, and agent-interpreted stop feedback, explicit storage consent, and termination on stop without re-entering the model loop.
- [ ] Run `python -m pytest -q tests/test_main_entry.py`; record baseline and missing behavior.
- [ ] Replace `ReviewState` regex authorization and review routing with agent interpretation of user feedback, a structured stop decision, and separately recorded explicit consent for knowledge storage. Stop must end the graph, preserve result and conversation, and not cause further model/tool calls. Test varied synonymous stop requests with a fake model, continuation feedback, ambiguous feedback, EOF, and a stop request without storage consent. Do not treat agent-inferred completion as consent. Implement compatibility and checkpoint identity checks; update docs and examples to `FAGENT/__main__.py`, DRC/LVS configuration and evidence requirements. Search `rg -n 'cli\.py|FAGENT\.cli|Agent_caller' README.md FAGENT .agent pyproject.toml` and update active references only.
- [ ] Run `python -m pytest -q` and `python -m FAGENT --help`; expect all tests pass and exit 0. Record any unavailable dependencies.
- [ ] Run project CAD/EDA verification and applicable deck validators if available. Distinguish fake/simulator results from real model, DRC and LVS runs; report evidence and any unverified stages without claiming a real pass.
