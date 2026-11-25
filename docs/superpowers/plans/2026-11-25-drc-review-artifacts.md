# DRC Review Fixes and Plan Artifact Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve remaining DRC review findings and persist the model's current plan as readable `plan.md` while preserving checkpoint recovery.

**Architecture:** Keep `state["plan"]` and `state["checklist"]` as the authoritative machine state. Render `plan.md` from validated plan data after planning; use graph checkpoints for state recovery. Address geometry and candidate persistence defects at their existing shared service boundaries, retaining existing user work.

**Tech Stack:** Python, LangGraph, gdstk, Shapely, pytest.

**Spec:** `docs/superpowers/specs/2026-11-23-drc-agent-geometry-design.md` and the 2026-11-25 inline code review.

## Global Constraints

- Preserve the user's uncommitted edits; inspect `git diff` before each edit.
- No arbitrary rule, cell, layer, DBU, or voltage defaults; derive from source data.
- Do not claim full DRC or connectivity verification from a local geometry check.
- `plan.md` is a derived artifact, never a source of truth for state or model execution.
- Do not create `checklist.json` for new runs; keep the checklist and digest in graph checkpoint state and preserve resume integrity.
- Use TDD, real geometry fixtures, no nested functions, and put `run` first in modified classes.

## Review Focus

- Fractional DBU thresholds must round upward, never reduce a minimum (Task 1).
- Rule-deck expressions with derived layers, exclusions and directional semantics must not be promoted to supported solely by a regex (Task 1).
- An interrupted candidate/map commit must be recoverable even if the process dies between file replacements (Task 2).
- A replan or failed/empty plan must not leave an old `plan.md` appearing current (Task 3).
- Resume with a checkpoint and a missing or altered checklist artifact must behave according to the confirmed storage policy (Task 4).

## Input and output examples

Input: plan `{'summary':'Inspect coverage','steps':[{'task_id':'task-1','tool':'get_violation','purpose':'Read marker'}]}`. Output `plan.md` includes the summary and an ordered item with the task, tool and purpose; text is derived from the validated plan, never invented. On an empty plan for active violations, result remains incomplete and the artifact identifies the empty or failed plan.

For source rule inspection: `grep -n -A 16 '^CT_EN_7a_7b\|^CT_R_11' DRC/1P7M_6Ic_1TKMc_ALPA2.drc`. The actual deck and input paths come from run configuration; `CT_R_11` cannot be declared passing by M1-only coverage.

## Task 1: Finish geometry and rule safety review

**Files:** `FAGENT/tasks/drc/rule_evidence.py`, `FAGENT/capabilities/layout/repair_proposals.py`, `FAGENT/capabilities/layout/violation_geometry.py`, `tests/test_drc_geometry_review_fixes.py`.

**Interfaces:** Preserve `RuleEvidence.run(...)`, `ViolationGeometry.run(...)`, and `RepairProposals.run(...)`; emit `unknown`/`unsupported` when rule semantics, scale, connectivity or competing constraints are not proven.

- [ ] Add independent geometry tests showing a 25.4 DBU minimum requires at least 26 DBU; a rule with `OPPOSITE`, exclusions or unknown derived layers is not treated as unconditional enclosure; overlapping neighbouring metal or an unverified shared-cell edit produces no applicable proposal.
- [ ] Run `python -m pytest -q tests/test_drc_geometry_review_fixes.py` and observe each new test fail for its targeted behavior.
- [ ] Replace threshold truncation/rounding with conservative validated conversion; recognize supported semantics explicitly; reject proposals whose connectivity, adjacency, hierarchy or exclusions are unproven.
- [ ] Run targeted geometry tests and inspect actual rule evidence and the resulting status on the configured CT/M1 example.

## Task 2: Recoverable candidate and serializable tool results

**Files:** `FAGENT/engine/graph_services.py`, `FAGENT/capabilities/layout/layout_service.py`, `FAGENT/tasks/drc/tools.py`, `tests/test_drc_transactional_apply.py`, `tests/test_drc_geometry_review_fixes.py`.

**Interfaces:** Preserve `GraphServices.checkpoint_candidate() -> str` and JSON-compatible `ToolResult`; a candidate and its polygon ID mapping must remain recoverable as a digest-matched pair.

- [ ] Add a real layout/tool-dispatch test proving `replace_polygon` sends a JSON-serializable result and the graph state reflects the saved candidate.
- [ ] Add failure-injection and restart tests for failures during both file replacements and recovery, including first save and replacing an existing candidate; verify original GDS and prior candidate/map digest remain intact.
- [ ] Run targeted tests and identify any failing cases in current uncommitted changes.
- [ ] Where failures remain, stage versioned immutable pairs and atomically commit a small pointer/manifest or equivalent recoverable protocol; ensure rollback restores memory and disk after exceptions or restart.
- [ ] Run `python -m pytest -q tests/test_drc_transactional_apply.py tests/test_drc_geometry_review_fixes.py` and inspect saved artifacts.

## Task 3: Write current plan to Markdown

**Files:** `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/graph_runner.py`, `tests/test_drc_replanning.py`, new `tests/test_drc_plan_artifact.py`.

**Interfaces:** `write_plan_markdown(rundir, plan, status, check_output_path) -> Path` persists `rundir/plan.md` atomically from the validated plan; existing state remains authoritative.

- [ ] Add tests covering ordered step rendering, UTF-8 and Markdown content, empty plan, failed model response, successive replan replacement, protected output paths, and resume without reusing stale content.
- [ ] Run `python -m pytest -q tests/test_drc_plan_artifact.py` and confirm expected failure before implementation.
- [ ] Implement deterministic rendering and atomic output-path-checked writing after each plan outcome; record status/revision and failure when no valid current plan exists, including no-active-task handling.
- [ ] Verify replan updates the file, trace reports the artifact, and execution still reads `state['plan']`; run targeted tests.

## Task 4: Checklist artifact policy

**Files:** `FAGENT/tasks/drc/steps.py`, `FAGENT/tasks/drc/tools.py`, `FAGENT/engine/graph_runner.py`, `FAGENT/engine/fagent_state.py`, associated checklist and resume tests.

- [ ] Add tests proving new runs create no checklist file, all updates remain in state, resume verifies the checkpoint's checklist digest, and old checkpoints that reference an existing checklist remain readable; never delete historic files.
- [ ] Run the new tests and confirm they fail because the current implementation writes `checklist.json`.
- [ ] Remove new checklist file writes while preserving checkpoint state, integrity validation, and legacy resume; check update, invalidation, finalize and review/resume call sites.
- [ ] Run checklist, graph and resume tests.

## Task 5: Final verification

- [ ] Run `python -m pytest -q` and `git diff --check`; report all failures and warnings.
- [ ] Inspect generated `plan.md`, checkpoint state, candidate GDS and polygon mapping from a representative isolated run.
- [ ] Check modified code against no-nested-function and main-method-first rules; report any remaining gaps.
- [ ] State explicitly whether real Calibre and connectivity verification were available; do not equate mocked/static checks with signoff.
