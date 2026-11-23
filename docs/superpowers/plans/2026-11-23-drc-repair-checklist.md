# DRC Repair Checklist Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each DRC run an agent-maintained violation checklist, stable polygon IDs, and a geometric enclosure check that reports violating edges and shortages.

**Architecture:** Add task-local planning and checklist tools to the existing graph, with run-scoped checkpointed artifacts. Track direct polygon identities separately from transient gdstk indices and evaluate enclosure through a focused geometry service. Agent decisions remain distinct from actual DRC verification.

**Tech Stack:** Python, LangGraph, JSON Schema, gdstk, Shapely/GEOS, pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-drc-repair-checklist-design.md`.

## Global Constraints

- Apply repository CAD/EDA rules: main/run first in each class, no nested functions, configurable or derived values, execution results on instances.
- User task instructions come from `env_setup.yaml` `prompt`/`prompts`; the current prompt excludes density only. Never hard-code rule names or thresholds.
- All file outputs are contained in a distinct per-cell, per-run directory; never overwrite source GDS, report, layer maps, or decks.
- A checklist pass is an agent assessment, not a real DRC pass; do not claim DRC clean without actual supported DRC evidence.
- Treat GDS coordinates as user units; obtain precision from the GDS library and preserve report DBU conversion and provenance.
- Do not discard or overwrite the user's existing changes in the worktree; stage only task files.
- Before each commit inspect staged and unstaged changes. Several task files already contain user edits; do not use `git commit --only` on a file containing unrelated edits. Stage only implementation hunks or leave the commit for the user if separation is unsafe.

## Review Focus

- Report has more counted violations than geometry records: retain an unresolved task (Task 1).
- Source GDS mixes direct polygons, paths and hierarchy: never use a path/reference ID as a direct editable polygon (Task 2).
- Candidate, mapping or checklist differs from checkpoint after interruption: reject resume without silently reassigning IDs (Tasks 2 and 4).
- Concave, outside, and multiple-edge violations: report all determinable violating boundary portions and shortages, or return unknown if complete evidence cannot be established (Task 3).
- A later geometry edit invalidates an earlier passed task: mark evidence stale and require reconsideration (Tasks 4 and 5).

## Sources, artifacts and inspection

Inputs are configured cell list, per-cell GDS and DRC results, layer maps, rule decks, and the setup goal. Capture source paths and goal with each run; parsed `gds.json` and `drc.json`, the plan/checklist, polygon mapping, `candidate.gds`, SQLite checkpoint, conversation, result and any tool logs are run artifacts. Preserve report rule names, per-rule violation IDs, DBU scale, original ordering and duplicate-count information. For deck syntax inspect the actual selected deck rather than assuming its path, for example `grep -n -A 8 'CT_EN_7a_7b' DRC/1P7M_6Ic_1TKMc_ALPA2.drc`. A real DRC invocation is a separate integration check; mock results and static geometry checks are not real deck validation.

Example input: a report with rule `R_EN`, geometry IDs 1 and 2, and a goal excluding density creates two independent pending tasks; a density violation creates a skipped task only when the goal says so. Example enclosure: inside distance 0.02 µm with required 0.03 µm yields `fail` and shortage 0.01 µm; a point 0.02 µm outside yields shortage 0.05 µm. User-facing results retain coordinate units and evidence source.

---

### Task 1: Run isolation and complete report ingestion

**Files:** Modify `FAGENT/tasks/drc/steps.py`, `FAGENT/capabilities/layout/input_loader.py`, `FAGENT/engine/graph_runner.py`; test `tests/test_drc_runs.py`, `tests/test_drc_input_paths.py`.

**Interfaces:** `Steps.prepare(...)` yields `(run_directory, config, inputs)` per cell, with a fresh unique run directory under `output/<cell>/runs/<run_id>/rundir`; `GraphRunner.resume(...)` continues only the explicitly identified existing run. `drc.json` retains all parsed rules and counts, including rules without geometry.

- [x] Add tests for two fresh runs of the same cell retaining separate source, report and output files; a selected run resume restores its own checkpoint. Assert a rule reporting two violations but containing one or zero geometries survives ingestion unchanged.
- [x] Run `python -m pytest -q tests/test_drc_runs.py tests/test_drc_input_paths.py`; confirm the new assertions fail for the current shared `rundir` and filtered report.
- [x] Implement unique run allocation without hard-coded cell names, preserve report order/counts, and expose run ID/path for resume and human access. Do not silently resume the latest run.
- [x] Rerun the same tests and verify they pass; check existing CLI and output consumers for assumptions about `output/<cell>/rundir` and document the new location.
- [x] Commit only the task's files: `git add -- FAGENT/tasks/drc/steps.py FAGENT/capabilities/layout/input_loader.py FAGENT/engine/graph_runner.py tests/test_drc_runs.py tests/test_drc_input_paths.py` then `git commit --only -m 'feat: isolate DRC runs and retain report counts' -- FAGENT/tasks/drc/steps.py FAGENT/capabilities/layout/input_loader.py FAGENT/engine/graph_runner.py tests/test_drc_runs.py tests/test_drc_input_paths.py`.

### Task 2: Stable polygon identity and checkpoint restoration

**Files:** Create `FAGENT/capabilities/layout/polygon_ids.py`; modify `FAGENT/capabilities/layout/layout_service.py`, `FAGENT/capabilities/layout/process_gds.py`, `FAGENT/tasks/drc/tools.py`, `FAGENT/engine/graph_services.py`, `FAGENT/engine/graph_runner.py`; test `tests/test_polygon_ids.py`.

**Interfaces:** `PolygonIds.run(library) -> PolygonIds`, `id_for(cell_name: str, polygon) -> str`, `resolve(cell_name: str, polygon_id: str) -> polygon`, `save(path: Path, candidate_digest: str) -> None`, and `restore(path: Path, library, candidate_digest: str) -> PolygonIds`. `LayoutService.read_gds(cell_name)` emits `id`, kind, layer, datatype and points; `replace_polygon(cell_name, polygon_id, points)` preserves ID. Parsed initial `poly` entries expose IDs only when deterministically matched to a direct editable polygon; path-derived and referenced entries identify their kind and are not editable.

- [x] Add tests with multiple direct polygons, two cells, a path and a reference. Replace the first polygon, assert every ID remains associated with the intended geometry, candidate save/reload preserves IDs, and cross-cell, unsupported, missing and tampered IDs/maps fail closed. Cover identical polygons and ambiguous source matching explicitly; never guess an identity.
- [x] Run `python -m pytest -q tests/test_polygon_ids.py`; verify failure because stable IDs and restoration do not exist.
- [x] Implement a run-scoped serializable identity registry; use a deterministic one-to-one mapping at checkpoint serialization with cell, geometry/type signatures and occurrence information. Bind it to candidate digest and verify bijection on restore. For ambiguous source-to-runtime matching, label the initial entry unsupported/unmatched rather than fabricating an ID. Update tool schemas and prompt-facing initial GDS data consistently.
- [x] Run `python -m pytest -q tests/test_polygon_ids.py tests/test_drc_input_paths.py`; verify all pass and invalid restoration cannot alter an input.
- [x] Commit only the files listed for this task with message `feat: add stable DRC polygon identities`.

### Task 3: Enclosure geometry and violation evidence

**Files:** Create `FAGENT/capabilities/layout/cover_check.py`; modify `requirements.txt`, `FAGENT/tasks/drc/tools.py`; test `tests/test_cover_check.py`.

**Interfaces:** `CoverCheck.run(inner, outer, minimum: float, precision: float) -> dict` returns `status` (`pass`, `fail`, `unknown`), `minimum`, `measured_distance`, `units`, `violations`, `reason`; `check_cover(context, cell_name: str, inner_id: str, outer_id: str, minimum: float) -> ToolResult` resolves Task 2 IDs and includes them in the result. Each violation contains inner and outer boundary segment coordinates, association information, and shortage in user units; ambiguous outer associations are explicitly null and explained.

- [x] Add geometry tests: full enclosure above/equal/below threshold; touching at zero; the 0.02/0.03 inside and outside examples; partial and wholly outside polygons; multiple edges, concave shapes, holes or invalid geometry; unknown IDs; zero and non-finite inputs; GDS precision boundary. Assert no violation is silently omitted and unsupported/indeterminate cases return `unknown`.
- [x] Run `python -m pytest -q tests/test_cover_check.py`; verify the missing service/tool fails.
- [x] Add a bounded Shapely 2.x dependency and implement full-coverage and minimum boundary distance using GEOS, with tolerance derived from GDS precision. Split inner edges at cover-boundary intersections and at locations needed to identify insufficient-margin portions, associate all determinable closest outer boundary portions, and report per-segment shortage with its measurement location. When completeness or distance variation cannot be represented faithfully, return `unknown` with reason rather than an incomplete pass/fail. Reject invalid geometry and non-finite or negative thresholds. Tool evaluates only the specified direct polygon pair, not entire deck semantics.
- [x] Run `python -m pytest -q tests/test_cover_check.py`; assert all outputs include user units, all outside shortage examples are correct within one GDS precision unit, and no geometry case produces an unsupported pass.
- [x] Commit only the task's files with message `feat: check polygon enclosure with edge evidence`.

### Task 4: Checklist creation, persistence and tools

**Files:** Create `FAGENT/tasks/drc/checklist.py`, `FAGENT/tasks/drc/schemas/plan.json`; modify `FAGENT/tasks/drc/tools.py`, `FAGENT/engine/fagent_state.py`, `FAGENT/engine/graph_runner.py`; test `tests/test_drc_checklist.py`.

**Interfaces:** `Checklist.run(report: dict, proposed_tasks: list[dict]) -> Checklist`, `read() -> dict`, `update(task_id: str, status: str, work: str, reason: str, polygon_ids: list[str], evidence: list[dict]) -> dict`, `invalidate(cell_name: str, polygon_id: str) -> dict`, `save(path: Path) -> None`, and `restore(path: Path, expected_digest: str) -> Checklist`. Tool wrappers `read_checklist(context)` and `update_checklist(context, task_id, status, work, reason, polygon_ids, evidence)` use these operations. States are `pending`, `in_progress`, `pass`, `skipped`, `unresolved`.

- [x] Add tests that two same-rule geometry IDs make distinct tasks, duplicate IDs are disambiguated by occurrence, missing geometry/count mismatch creates unresolved work, and density is skipped only when the agent's plan and goal say so. Assert updates cannot delete tasks, accept only known status/task, preserve evidence, and stale passed evidence after edits. Test atomic writes, corrupt files, mismatched checkpoint digests, and separate fresh runs/resume.
- [x] Run `python -m pytest -q tests/test_drc_checklist.py`; verify checklist interfaces are absent.
- [x] Implement plan schema, report coverage reconciliation, atomic checklist updates, digest-bound checkpoint restoration and the tool wrappers. Agent controls task status; require recorded reason/evidence as appropriate, without treating it as DRC validation. Invalidate all potentially affected passed checks conservatively after any edit when dependency scope cannot be established. Keep authoritative state checkpointed and verify/reconstruct persisted file consistently; never silently accept a modified external file.
- [x] Run `python -m pytest -q tests/test_drc_checklist.py tests/test_polygon_ids.py`; verify pass and inspect an actual serialized checklist against the example in the spec.
- [x] Commit only the task's files with message `feat: track DRC repair checklist`.

### Task 5: Planning action, repair routing and review

**Files:** Modify `FAGENT/tasks/drc/workflow.yaml`, `FAGENT/tasks/drc/steps.py`, `FAGENT/tasks/drc/prompts/drc.md`, `FAGENT/tasks/drc/schemas/drc.json`, `FAGENT/engine/workflow_loader.py`, `FAGENT/engine/graph_builder.py`, `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/graph_services.py`, `FAGENT/engine/graph_runner.py`, `FAGENT/knowledge/templates/schemas/workflow.json`; test `tests/test_drc_plan_flow.py`.

**Interfaces:** Workflow `read → plan → repair → human_review`; `plan` is an agent action using Task 4 plan schema, with its own response schema and task-local prompt. Repair tools include Task 2 ID-based inspection/replacement, Task 3 `check_cover`, and Task 4 checklist tools. Each repair model request includes the latest checklist and rule-deck evidence/goal. Review includes task statuses and evidence.

- [x] Add fake-model graph tests asserting separate plan/repair schemas and calls, report coverage, current checklist at each model call, configured skip intent, task updates and evidence, and resume followed by further edits. Assert all-pass/skipped and unresolved paths reach human review with truthful status; a premature final response with pending tasks cannot claim completion, stale pass cannot end repair, and iteration exhaustion is incomplete. Assert missing/unavailable rule text cannot yield an unsupported tool-backed claim.
- [x] Run `python -m pytest -q tests/test_drc_plan_flow.py`; verify missing plan action and routing fail.
- [x] Extend the loader/builder to support a `plan` agent action with its own task-local schema/prompt, preserving existing agent/review workflows. Validate the plan against every report occurrence before creating the checklist; pass current checklist as context on every repair call. Let agent report unresolved tasks and stop; enforce no completed checklist while pending or stale entries remain. Include checklist in review and `result.json`; preserve incomplete status absent actual DRC evidence. Route human `continue` back to repair without recreating the plan.
- [x] Run `python -m pytest -q tests/test_drc_plan_flow.py tests/test_drc_checklist.py tests/test_chat_backend.py tests/test_openai_adapter.py`; verify pass and inspect recorded model calls for goal, DRC report, deck references, tool schemas and current checklist.
- [x] Commit only the task's files with message `feat: plan and track DRC repairs`.

### Task 6: Integrated verification and operating instructions

**Files:** Modify `FAGENT/tasks/README.md` and DRC-specific documentation as appropriate; test `tests/test_drc_end_to_end.py`.

**Interfaces:** User can locate `output/<cell>/runs/<run_id>/rundir`, inspect checklist and evidence, and resume a selected checkpoint. Document how the configured goal affects skips and the distinction between geometry evidence and real DRC.

- [x] Add an end-to-end fixture with a synthetic deck/report, two violations of one rule, one density violation and geometries with enclosure pass/fail. Use a deterministic fake model and real geometry operations; assert generated artifacts, input provenance, stable IDs, violation edge coordinates, checklist changes and review output. Test an edit after a pass, interruption/resume, and the incomplete outcome of an unsupported rule.
- [x] Run `python -m pytest -q tests/test_drc_end_to_end.py tests/test_drc_plan_flow.py tests/test_cover_check.py tests/test_polygon_ids.py tests/test_drc_checklist.py tests/test_drc_input_paths.py`; confirm all pass.
- [x] Run `python -m pytest -q` and `git diff --check`; report failures accurately. Inspect the configured ROM report and selected deck with the grep command above and run a representative flow only if its configured model and inputs are available; capture command, exit status and artifact paths. Do not run a real external DRC without its executable/deck configuration, and report it as not run if unavailable.
- [x] Run applicable CAD/EDA verification-before-completion checks, including DeckValidator and coding-rule checks where applicable; record unavailable commands and reasons, never equate simulated validation with DRC pass. Document actual CLI/resume usage and the path/units of generated outputs.
- [x] Commit only this task's documentation and test files with message `test: verify DRC checklist workflow`.
