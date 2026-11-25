# DRC Repair Gaps Implementation Plan

> **For agentic workers:** Use executing-plans inline. Preserve existing work in the checkout and run regression tests before reporting completion.

**Goal:** Supply rule-specific planning evidence, validate task coverage, resolve layer mappings, generate verified repair proposals, and show actionable outcomes in `plan.md`.

**Architecture:** Keep checklist and plan progress in LangGraph state. Retrieve and validate evidence per active rule, derive physical layer identities from configured authoritative inputs, and use deterministic geometry checks before staging edits. Render plan status from recorded tool outcomes.

**Tech Stack:** Python, LangGraph, gdstk, pytest, configured DRC deck and layer maps.

## Constraints and examples

- Preserve model choice of tool order. Validate plan coverage and task association, not the execution sequence.
- Never invent layer numbers or assume derived Calibre layers are physical layers. Missing evidence remains unresolved with a reason.
- Preserve connectivity, neighboring-rule constraints, hierarchy and shared-cell behavior. Unknown verification results must prevent proposal application.
- Example planner input `task-3 → GT_A_13`, `task-7 → BD_R_1_GT` must not use a `BD_R_1_GT` reference as evidence for task-3. A plan containing only task-3 must report missing task-7 and request a corrected plan with bounded retries.
- Example tool output `proposals: []` must display zero proposals and any available reason. A conditional edit omitted for that reason must have an explicit skipped state and reason.
- Locate deck definitions with `grep -nE '^GT_A_13[[:space:]]*\u007b|^LAYER[[:space:]]+GT|^LAYER MAP' DRC/1P7M_6Ic_1TKMc_ALPA2.drc`.

## Task 1: Retrieve relevant evidence and validate plan coverage

**Files:** `FAGENT/engine/graph_nodes.py`, RAG implementation and `tests/test_drc_replanning.py`.

- [ ] Add tests with partially populated references, duplicate sources, absent rules, retrieval failures, multiple tasks sharing a rule, and a model response omitting an active task. Verify exact rule matching, deduplication, missing-evidence reporting and bounded retry or incomplete result.
- [ ] Run the tests to observe the existing failure, then retrieve missing references per distinct active rule using the configured RAG. Preserve provenance and explicitly report unavailable evidence in the planner payload.
- [ ] Validate that every active task appears in the returned plan. Return actionable validation feedback to the planner with a bounded retry and record failure when retries are exhausted. Preserve previous plan history during replanning.
- [ ] Verify rule-to-task associations with structured identifiers. Do not infer a rule from arbitrary natural-language purpose text as an authoritative validation. Test misleading references and preserve the existing model-selected tool order.
- [ ] Run `python -m pytest tests/test_drc_replanning.py tests/test_drc_plan_progress.py -q`.

## Task 2: Resolve physical layer mappings and rule support

**Files:** `FAGENT/tasks/drc/rule_evidence.py`, configured layer-map loader, `tests/test_drc_geometry_review_fixes.py` and focused fixtures.

- [ ] Test configured mapping formats, duplicate drawing/text entries, missing GT/CT/AA/BORDER mappings, unsupported derived layers, and mismatched sources. Never silently substitute a text datatype for drawing.
- [ ] Trace physical GDS layer/datatype evidence to authoritative configured PDK input. Add verified mappings only when their provenance is established. Emit explicit missing or ambiguous diagnostics otherwise.
- [ ] Define an explicit support result for each rule expression. Evaluate GT area and exception-region semantics against the deck and available geometry. Implement a deterministic checker only where all required regions and hierarchy are available. Otherwise mark the task unresolved with a precise reason.
- [ ] Run focused rule and mapping tests, then compare the test output with the configured DRC deck and GDS units.

## Task 3: Generate proposals with actual verification evidence

**Files:** `FAGENT/capabilities/layout/violation_geometry.py`, `FAGENT/capabilities/layout/repair_proposals.py`, `FAGENT/tasks/drc/tools.py`, geometry and transactional tests.

- [ ] Add failing tests for verified rectangular enclosure repair, connectivity impact, neighboring shapes, shared instances, missing hierarchy information and stale candidate digest. Include at least one case where each verification cannot be established and must block editing.
- [ ] Implement the three verification results from real layout and rule evidence. Bind evidence to the candidate digest and relevant cell and polygon identities. When the required connectivity or neighboring rules cannot be established, return an explicit unsupported reason instead of setting a verification flag.
- [ ] Generate a proposal only after all checks pass. Apply via `apply_polygon_fix`, recheck the saved candidate, and retain rollback and stale-proposal behavior. Ensure limitations on supported rules are visible to the agent.
- [ ] Run `python -m pytest tests/test_drc_geometry_review_fixes.py tests/test_drc_transactional_apply.py tests/test_drc_geometry_integration.py -q`.

## Task 4: Record proposal outcomes and conditional steps

**Files:** `FAGENT/engine/graph_nodes.py`, `FAGENT/tasks/drc/tools.py`, `FAGENT/tasks/drc/prompts/repair.md`, `tests/test_drc_plan_progress.py`.

- [ ] Test an empty proposal result, a valid proposal, an unsupported rule, an explicit skip and replanning. Verify `plan.md` retains previous versions and displays proposal count and reason without reporting a DRC pass.
- [ ] Return a reason alongside an empty proposal list when the tool can determine one. Record a compact outcome including proposal count and reason, without embedding polygon geometry in plan history.
- [ ] Provide a way for the agent to report that a conditional step was skipped, with task and reason. Match it to the intended active plan step and persist it in state and `plan.md`. Preserve existing executed/failed semantics and avoid assuming a skip from a missing call.
- [ ] Run `python -m pytest tests/test_drc_plan_progress.py tests/test_drc_replanning.py -q`.

## Task 5: End-to-end verification

- [ ] Add a representative integration test from active tasks and partial RAG references through planning, tool execution, conditional apply, recheck and final artifact. Also test unavailable mapping and unsupported GT area, expecting an unresolved task with no geometry edit.
- [ ] Run the focused suites and `python -m pytest -q`. Report failures by name and distinguish existing failures from regressions.
- [ ] If the configured model, DRC deck and layer mappings are available, run a new flow and inspect `conversation.json`, `trace.log`, `plan.md` and the candidate GDS. Confirm task-to-rule association, measurement and proposal evidence, and whether the candidate changed. Report missing external tools or unsupported rules explicitly.
