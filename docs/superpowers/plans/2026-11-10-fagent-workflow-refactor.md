# FAGENT Workflow Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the FAGENT Python scripts around the documented workflow, support summary input, and leave Calibre execution disabled until a site installation is available.

**Architecture:** Preserve the existing loader, GDS reader, caller, provider adapter and geometry tool boundaries. Normalize DRC input at the loader boundary; preserve geometry and output interfaces. Validation saves a changed candidate but explicitly reports that fresh DRC is unavailable.

**Tech Stack:** Python 3.9+, numpy, gdstk, OpenAI SDK, pytest.

**Spec:** `.agent/AI_Workflow.md`, with the user's clarification that Calibre execution is temporarily disabled.

## Global Constraints

- Never overwrite the source GDS or DRC input; check resolved output paths before writing.
- GDS and results marker coordinates are DBU; gdstk coordinates are user units. Use the input scale for conversions.
- Preserve layer and datatype; reject invalid polygons and preserve the existing property guard.
- A change to a referenced cell affects all its instances; expose the existing hierarchy information and do not infer connectivity.
- No fresh Calibre run means no DRC pass; report incomplete validation, even for a clean baseline report.
- Keep paths and site settings configurable; preserve CLI and existing provider compatibility.
- Keep class `run` methods first and instance outputs on `self`; avoid nested functions in production code.

## Review Focus

- Truncated or unrecognized DRC input must fail instead of being treated as clean (Task 1).
- A summary without an explicit total must not silently become zero violations (Task 1).
- Output aliases of input paths must be rejected before writing (Task 2).
- Invalid geometry must leave the loaded polygon and validation state unchanged (Task 3).
- Old or clean baseline reports must never be treated as fresh validation (Task 4).

## File Map

- `FAGENT/loader.py`: input checks, shared DRC format selection and JSON output.
- `FAGENT/functions.py`: layout tools and explicitly disabled validation.
- `FAGENT/process_gds.py`: GDS parsing and DBU data; preserve public behavior.
- `FAGENT/caller.py`, `FAGENT/agy_adapter.py`: bounded tool loop and provider interface.
- `FAGENT/__main__.py`: configuration and orchestration; preserve CLI.
- `tests/test_agent.py`: regression and boundary tests; `README.md`: documented behavior.

Locate the specification sections with `grep -nE '^###|^##|Calibre|Summary|Zero-Assumption' .agent/AI_Workflow.md`.

Representative input: a Calibre summary reporting `TOTAL DRC Results Generated: 10` for `ROM_MCELL0`. Expected `drc.json`: `top_cell` is `ROM_MCELL0`, `total_violations` is `10`, `type` is `summary`, and no marker coordinates or DBU scale are invented. Results input retains its existing `rules`, `geometry`, and `dbu_per_user_unit` fields. A modified candidate with validation disabled yields `status: incomplete` with a reason stating Calibre was not run.

---

### Task 1: Normalize DRC input

**Files:** Modify `FAGENT/loader.py`, `FAGENT/functions.py`, `tests/test_agent.py`.

**Interfaces:** Preserve `Agent_loader.parse_drc(path)` for ASCII results. Add `Agent_loader.read_drc(path)` returning a JSON-serializable dict; use the existing summary schema from `Agent_functions.parse_drc_summary(path)` and retain the latter as a compatibility wrapper.

- [ ] Add tests using the existing sample results and summary: assert the total is 10, summary has rule/cell counts, and results retain DBU markers; assert malformed and truncated inputs raise `ValueError`, including a summary missing its total.
- [ ] Run `python -m pytest -q tests/test_agent.py` and confirm the new tests fail for the missing input handling.
- [ ] Move summary parsing to the loader, require an explicit total, select supported formats by content, and have loader `run()` and `read_drc_log()` use `read_drc`; do not invent summary markers or units.
- [ ] Run `python -m pytest -q tests/test_agent.py` and confirm all tests pass.

### Task 2: Preserve ingestion and entry-point contracts

**Files:** Modify `FAGENT/loader.py`, `FAGENT/process_gds.py`, `FAGENT/__main__.py`, `tests/test_agent.py`.

**Interfaces:** Preserve `Agent_loader.run(...) -> Agent_loader`, `ProcessGDS.run(...) -> ProcessGDS`, `gds_data`, `drc_data`, CLI arguments and output filenames.

- [ ] Add tests for input/output path aliases, results and summary `--load-only`, and GDS DBU geometry using the existing sample. Assert input bytes are unchanged and output JSON has the expected total and geometry.
- [ ] Run `python -m pytest -q tests/test_agent.py` and record failing cases.
- [ ] Refactor ingestion and CLI orchestration without changing existing public signatures; retain configured layer maps and resolve destination paths before comparing them with inputs.
- [ ] Run `python -m pytest -q tests/test_agent.py` and confirm all tests pass.

### Task 3: Refactor geometry operations

**Files:** Modify `FAGENT/functions.py`, `tests/test_agent.py`.

**Interfaces:** Preserve `read_gds(cell_name)`, `replace_polygon(cell_name, polygon_index, points)` and `save_gds()` result shapes and existing gdstk polygon indexing.

- [ ] Add tests for a valid replacement retaining layer/datatype, an invalid or nonfinite polygon leaving original geometry unchanged, and saving/reopening the candidate. Assert the source GDS remains unchanged.
- [ ] Run `python -m pytest -q tests/test_agent.py` and confirm new regression tests exercise the existing contract; fix any demonstrated failures during refactoring.
- [ ] Refactor geometry steps into focused methods while preserving source units, property guard, dirty flag and `last_validation` invalidation.
- [ ] Run `python -m pytest -q tests/test_agent.py` and confirm all tests pass.

### Task 4: Disable Calibre and preserve honest feedback

**Files:** Modify `FAGENT/functions.py`, `FAGENT/caller.py`, `tests/test_agent.py`.

**Interfaces:** `validate()` returns `{'status': 'not_validated', 'reason': ...}`; after a modification it saves the candidate and states Calibre is disabled. `run_calibre()` continues to fail explicitly without invoking a subprocess. Caller result remains `incomplete` without fresh validation.

- [ ] Add tests for unmodified and modified candidates, disabled `run_calibre`, and a clean baseline or stale report: assert no pass is returned and no Calibre subprocess runs. Assert validation feedback reaches the next model tool message.
- [ ] Run `python -m pytest -q tests/test_agent.py` and confirm the new disabled-validation behavior fails before implementation.
- [ ] Implement the disabled response, retain explicit `run_calibre` failure, and ensure caller sends the actual tool response to the model without treating baseline data as fresh validation.
- [ ] Run `python -m pytest -q tests/test_agent.py` and confirm all tests pass.

### Task 5: Refactor provider and end-to-end orchestration

**Files:** Modify `FAGENT/caller.py`, `FAGENT/agy_adapter.py`, `FAGENT/__main__.py`, `tests/test_agent.py`, `README.md`.

**Interfaces:** Preserve `Agent_caller.run(...)` and `AgyCompletions.create(model, messages, tools)`; keep allowed-tool checks, tool messages, iteration limit, `conversation.json`, and `result.json`.

- [ ] Extend mocked Ollama and AGY roundtrip tests to assert iteration limits, unknown tool rejection, tool feedback, output files, and incomplete status when validation is disabled.
- [ ] Run `python -m pytest -q tests/test_agent.py` and record the baseline.
- [ ] Refactor provider and orchestration code around existing contracts; document summary limitations, disabled Calibre, artifacts, DBU conversion, and the need for fresh DRC and separate LVS.
- [ ] Run `python -m pytest -q` and confirm all tests pass; run `python -m FAGENT --help` and confirm the CLI loads.
- [ ] Run the README sample `--load-only` command when its external sample files are available; verify exit status 0 and inspect both output JSON files. Report unavailable samples rather than claiming this check passed.
- [ ] Review the diff against the specification, then commit the approved changes in task-sized commits using exact changed paths.

## Verification limits

Mocked providers and fixture parsing are not real Calibre validation. No Calibre command, deck, fresh DRC artifact, or DRC-clean claim is in scope until the installation and site-specific interface are supplied. No numerical geometry acceptance threshold beyond preservation of the existing values and polygon area greater than zero is specified by the workflow.
