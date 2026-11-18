# Run-directory Data Exchange Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Pass loader output to the caller and geometry tools through JSON files in a run directory.

**Architecture:** Main runs the loader into `<output>/rundir`, then starts the caller using only that path and configuration. The caller reads `gds.json` and `drc.json`, and directly invokes an `Agent_functions` instance initialized with the same path. Functions opens the original GDS named by `gds.json` for editing and reads normalized DRC JSON; Calibre stays disabled.

**Tech Stack:** Python 3.9+, gdstk, OpenAI SDK, pytest.

**Spec:** `docs/superpowers/specs/2026-11-10-rundir-data-exchange-design.md` and `.agent/AI_Workflow.md` (Calibre execution disabled by user request).

## Global Constraints

- Preserve existing user modifications; inspect the working diff before editing.
- `--output` is the parent; run artifacts belong under `<output>/rundir`.
- Do not pass loader instances to caller or functions; functions must not import loader.
- Do not overwrite source GDS or DRC; resolve output paths and protect existing hard links with file identity checks.
- DBU and user-unit conventions remain unchanged; never claim DRC pass without fresh results.
- `run()` is first in each modified class; store class outputs on the instance and avoid nested functions in new code.

## Review Focus

- Missing or malformed JSON must cause a clear failure (Tasks 1 and 2).
- Missing `source` or original GDS must not produce a candidate (Task 1).
- Hard-linked output and input must not overwrite the input (Task 1).
- Each model request after a tool call must include the actual preceding conversation (Task 2).
- Reused run directories must not allow old reports to establish DRC pass (Tasks 1 and 2).

## Files and contracts

- Modify `FAGENT/functions.py`: `Agent_functions(rundir=None, verbose=True)` and `run(rundir=None, verbose=None) -> Agent_functions`; load normalized JSON, use its GDS `source`, write candidate in `rundir`.
- Modify `FAGENT/caller.py`: `Agent_caller(rundir=None, config=None, verbose=True)` and `run(rundir=None, config=None, verbose=None) -> dict`; own functions instance and persisted conversation/result.
- Modify `FAGENT/__main__.py`: create `<output>/rundir` and orchestrate loader then caller.
- Modify `FAGENT/loader.py`: extend existing overwrite guard for hard-linked existing files.
- Modify `tests/test_agent.py` and `README.md`: exercise and document the changed contracts.

Find relevant design sections: `grep -nE '^##|rundir|source|overwrite' docs/superpowers/specs/2026-11-10-rundir-data-exchange-design.md`.

Example input: `gds.json` contains `{"source": "/data/layout.gds", "coordinates": "gds_database_units", "database": {}}`; `drc.json` contains `{"total_violations": 10}`. Caller payload contains those two parsed objects; functions opens `/data/layout.gds`; output goes to `<output>/rundir/candidate.gds` and validation reports `not_validated`.

---

### Task 1: File-based geometry tools and input protection

**Files:** `FAGENT/functions.py`, `FAGENT/loader.py`, `tests/test_agent.py`.

**Interfaces:** `Agent_functions(rundir=None, verbose=True).run(rundir=None, verbose=None)` reads `gds.json` and `drc.json`, stores them as `gds_data` and `drc_data`, and uses `gds_data['source']` for the original GDS. `read_drc_log(path=None)` reads normalized JSON (default `rundir/drc.json`) and stores `drc_log`.

- [ ] Add tests that use real loader JSON without passing the loader to functions, read a cell, save and reopen a modified candidate, and assert original GDS bytes remain unchanged. Assert missing/invalid JSON, missing source, and missing original GDS fail clearly. Assert an existing hard link from each output to an input is rejected before any write. Assert a stale clean DRC JSON does not make validation pass.
- [ ] Run the new tests with `python -m pytest -q tests/test_agent.py -k 'rundir or hardlink'`; confirm failure is due to the missing file-based interface or hard-link guard.
- [ ] Implement the interfaces above, remove the loader import and raw-report parsing wrapper from functions, and check file identity of existing output/input pairs in the loader before writing.
- [ ] Migrate existing geometry and DRC tests to normalized JSON and run `python -m pytest -q tests/test_agent.py -k 'geometry or drc_log or rundir or hardlink'`; expect all selected tests to pass.

### Task 2: File-based caller and provider feedback

**Files:** `FAGENT/caller.py`, `tests/test_agent.py`.

**Interfaces:** `Agent_caller(rundir=None, config=None, verbose=True).run(rundir=None, config=None, verbose=None)` reads both JSON objects, creates functions with `rundir`, and writes `conversation.json` and `result.json` there. Tool execution and schemas remain owned by functions.

- [ ] Add a two-turn provider-boundary test with loader-produced JSON and a real `read_gds` tool call. Assert the initial user payload contains both saved objects, the second provider request includes the assistant call and its tool response, and the output files and incomplete result exist in `rundir`. Add tests for absent and malformed JSON and stale clean DRC input.
- [ ] Run the new caller tests and confirm they fail against the old loader-instance interface.
- [ ] Replace loader input with `rundir`, read saved JSON, initialize functions with that directory, and use the same `self.messages` conversation for all provider requests and persisted output. Preserve bounded iteration and tool allowlist behavior; do not discard unrelated user edits.
- [ ] Migrate existing mocked Ollama and AGY tests to the new interface and run `python -m pytest -q tests/test_agent.py -k 'caller or openai or agy'`; expect all selected tests to pass.

### Task 3: Main orchestration and regression verification

**Files:** `FAGENT/__main__.py`, `tests/test_agent.py`, `README.md`.

**Interfaces:** `python -m FAGENT --gds G --drc D --output O --load-only` writes `O/rundir/gds.json` and `O/rundir/drc.json`. A full run additionally writes conversation and result in the same run directory.

- [ ] Add CLI tests for results and summary `--load-only` and a mocked full run; assert artifact paths, unchanged input bytes, and the incomplete status.
- [ ] Run the CLI tests and confirm they fail before the output path changes.
- [ ] Change main to run loader with `Path(args.output) / 'rundir'`, read load-only results from saved JSON, and pass the path to caller; document migration of output locations and JSON-only tool inputs.
- [ ] Run `python -m pytest -q`, `python -m FAGENT --help`, and `git diff --check`; require zero failures and report exact results. Inspect changes for nested functions and main-method ordering.
- [ ] When external sample files are available, run the README `--load-only` command with a temporary output directory and inspect both JSON artifacts; report explicitly if unavailable. Calibre and live model execution are outside this verification.
