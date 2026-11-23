# Report-Driven DRC Checklist Implementation Plan

> **For agentic workers:** Use the executing-plans skill to implement this plan inline. Steps use checkbox syntax for tracking.

**Goal:** Build a complete DRC checklist from the report even when the model omits individual violations, marking unplanned items unresolved.

**Architecture:** Derive the canonical list of violation occurrences deterministically from the parsed DRC report. Reconcile valid model suggestions against that list, keeping every report item in report order; save missing suggestions as unresolved checklist entries and preserve existing validation of invalid suggestions. Continue through the existing repair and human-review flow with truthful completion status.

**Tech Stack:** Python, LangGraph, JSON Schema, pytest, existing DRC parser and checklist persistence.

**Spec:** Agreed design in the conversation: the report is the source of checklist entries; model supplies work; omitted entries are unresolved. Existing background: `docs/superpowers/specs/2026-11-23-drc-repair-checklist-design.md`.

## Global Constraints

- Derive rules, counts, geometry IDs, occurrence indexes, paths and goal from inputs; do not hard-code project values.
- Preserve DBU coordinates and report provenance; do not interpret checklist completion as real DRC validation.
- Keep the existing skip authorization, artifact path checks, atomic save and digest-bound resume behavior.
- Class entry points precede other methods; store main execution outputs on the instance.

## Review Focus

- Duplicate IDs within one rule must remain separate occurrences (Task 1).
- Missing geometry or summary-only counts must remain unresolved (Task 1).
- Unmatched, duplicated or unauthorized skipped model tasks must fail rather than fabricate report data (Task 1).
- An empty or partial valid model plan must persist all report items as unresolved where appropriate (Tasks 1 and 2).
- Invalid model output or model failure must not be mistaken for a valid completed plan (Task 2).

## Data and Example

Inputs: the selected setup YAML identifies cell list, input GDS, DRC results, rule deck and output root. The parser produces `drc.json`; `read` passes it to `plan`, which sends report and goal to the model. The resulting `checklist.json` belongs in `<output_folder>/<cell>/runs/<run_id>/rundir/`; state and digest are checkpointed. No geometry coordinates or units are transformed by reconciliation.

Example report: `rules: [{name: R1, total_count: 2, geometry: [{id: 1}, {id: 2}]}]`. Example model tasks: `[{rule: R1, violation_id: 1, occurrence: 0, work: Inspect, status: pending, reason: ''}]`. Expected checklist: R1/1/0 is `pending` with model work; R1/2/0 is `unresolved` with a reason that the model supplied no plan. Both have unique task IDs, empty evidence and `stale: false`.

To inspect the relevant section of a chosen text report or deck, derive the path from setup and run, for example: `grep -n -A 12 'BD_R_1_GT' "$report_path"`. This example rule is diagnostic only and must not appear in implementation logic.

## Files

- Modify `FAGENT/tasks/drc/checklist.py`: construct report-ordered checklist and reconcile model suggestions.
- Modify `FAGENT/tasks/drc/prompts/plan.md`: explain that suggestions enrich report-derived entries; retain requirements to preserve IDs and follow skip policy.
- Modify `tests/test_drc_checklist.py`: exercise reconciliation and existing safety rules.
- Modify `tests/test_drc_plan_flow.py`: exercise persisted checklist and graph continuation for partial model output.
- Modify `FAGENT/tasks/README.md`: document unresolved fallback and clarify that model errors still fail the plan step.

### Task 1: Reconcile report-derived entries with model suggestions

**Interfaces:** `Checklist.run(report: dict, proposed_tasks: list, goal: str) -> Checklist` remains unchanged. `Checklist.read() -> dict` exposes the existing tasks, summary and digest; each task retains its existing schema.

- [x] Add tests in `tests/test_drc_checklist.py` using a two-ID rule with only the first task proposed. Assert exactly two report-ordered tasks, first pending with supplied work, second unresolved with a nonempty omission reason, distinct task IDs and empty evidence. Add tests for an empty plan, duplicate IDs distinguished by occurrence, count-mismatch placeholders, and summary-only input. Verify an unknown or duplicate proposal and an unauthorized skip still raise `ValueError`. Verify a skipped proposal is accepted only with an allowed goal.
- [x] Run `python -m pytest -q tests/test_drc_checklist.py`; confirm the partial and empty plan tests fail on the current missing-violation exception.
- [x] Update `Checklist._reconcile(expected_items: list, proposed_tasks: list, goal: str)` to match suggestions one-to-one, reject unmatched proposals and preserve skip checks. Produce one output per extracted report item in report order. For unmatched geometry, use status `unresolved`, generic inspection work, and an explicit reason that no model plan was supplied. Preserve existing missing-geometry unresolved reasons and existing task fields. Do not generate an automatic skip from the goal.
- [x] Run `python -m pytest -q tests/test_drc_checklist.py`; expect all tests to pass.

### Task 2: Persist and route partial plans

**Interfaces:** `GraphNodes.execute_plan(context, step) -> dict` continues to return `checklist`, `checklist_path`, `checklist_digest` and `messages`; output schema remains `FAGENT/tasks/drc/schemas/plan.json`.

- [x] Add a fake-model graph/plan test in `tests/test_drc_plan_flow.py`: return a schema-valid plan containing one of two report violations, run the plan step, inspect `checklist.json` and digest, and assert both entries exist and the omitted one is unresolved. Assert routing proceeds to repair and a remaining unresolved entry cannot be represented as a fully resolved checklist. Add checks that schema-invalid responses and model exceptions still fail rather than silently synthesize a successful plan.
- [x] Run `python -m pytest -q tests/test_drc_plan_flow.py`; verify the new partial-plan test fails before Task 1 or passes after Task 1, and the negative cases remain enforced.
- [x] Update `FAGENT/tasks/drc/prompts/plan.md` to request actionable suggestions for each supplied violation, preserve identity and occurrence, and state missing suggestions become unresolved. Update `FAGENT/tasks/README.md` to describe the fallback, status and output path; do not imply an actual DRC pass.
- [x] Run `python -m pytest -q tests/test_drc_plan_flow.py tests/test_drc_checklist.py tests/test_drc_runs.py tests/test_drc_end_to_end.py`; expect all tests to pass.

### Task 3: Verify representative flow and artifacts

- [x] Run `python -m pytest -q tests`; record the exit status and failures, if any. Run the project's applicable coding-rule and deck validators if available; report unavailable validators explicitly.
- [x] Using a configured test run and permission to use its model, run the DRC flow through plan. Record the setup, report and rule-deck paths, model invocation outcome and run directory. Inspect `checklist.json`: every geometry and missing-count occurrence must appear exactly once, omission entries must be unresolved, and the stored digest must restore successfully. Check logs and exit status. Do not assume model output will omit a particular item; the fake-model test provides deterministic omission coverage.
- [x] If a real DRC backend is available and validation is in scope, separately run it with the configured deck and inspect its output and exit status. Otherwise state that unit/integration checks and checklist creation are not a real deck validation.
