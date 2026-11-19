# FAGENT DRC Goal & Agent Tool Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a default CAD/EDA system prompt, update agent-facing DRC repair tool contracts and validation feedback, and load `prompts` from `env_setup.yaml` as the user goal passed into the LangGraph agent workflow.

**Architecture:** `env_setup.yaml` defines `prompts` as the high-level user goal, which `FAGENT/__main__.py` normalizes into `config['goal']`. `FAGENT/agent/prompts.py` provides a default system prompt (merged with any configured `system_prompt` and bounded RAG context), while `GraphServices.prepare()` injects `goal` into the initial user message alongside `gds` and `drc`. `FAGENT/tools/` and `FAGENT/domain/layout.py` expose explicit DRC-repair tool descriptions and actionable validation feedback (including running `CalibreSimulator` on `candidate.gds` when a DRC deck is configured).

**Tech Stack:** Python 3, LangGraph, LangChain `StructuredTool`, Pydantic, PyYAML, `gdstk`, `pytest`.

**Spec:** `.agent/AI_Workflow.md`

## Global Constraints

- No nested `def` inside functions or methods; `run()` must be the first method of every class and store main outputs on `self`.
- No hard-coded cell names, rule names, or fixed paths in logic; all paths and goals must come from config/setup/inputs.
- Preserve existing CLI flags (`--setup`, `--gds`, `--drc`, `--output`, `--load-only`, `--resume-thread`, `--quiet`) and checkpoint/review behavior.
- Never dispatch subagents; execute and verify all steps inline.

## Review Focus

- `env_setup.yaml` defines `prompts` as either a string or a list of strings -> `FAGENT/__main__.py` normalizes both into a single non-empty goal string in `config['goal']`.
- `config.json` has no `system_prompt` key -> `build_system_prompt()` still emits `DEFAULT_SYSTEM_PROMPT` so the agent is never left without instructions.
- Agent calls `validate()` before any `replace_polygon()` call (`dirty == False`) -> `LayoutService.validate()` returns `status: 'not_validated'` with an explicit `reason` instructing the agent to call `read_gds` and `replace_polygon` first.
- `replace_polygon()` appends the replacement polygon to the end of `cell.polygons` -> tool description and return payload (`new_index`, `layer`, `datatype`) explicitly guide subsequent edits.
- `drc_files` is configured when `validate()` runs after polygon modification -> `LayoutService.run_calibre()` runs `CalibreSimulator` on `candidate.gds` and returns real simulator violation counts (`total_violations`, `violations_by_rule`, `simulated: True`) so the agent knows whether DRC violations are resolved.

---

### Task 1: Default System Prompt & `env_setup.yaml` Goal Ingestion

**Files:**
- Modify: `env_setup.yaml:1-5`
- Modify: `FAGENT/config.json:1-15`
- Modify: `FAGENT/config.example.json:1-16`
- Modify: `FAGENT/__main__.py:41-69`
- Modify: `FAGENT/agent/prompts.py:1-21`
- Modify: `FAGENT/agent/services.py:27-48`
- Test: `tests/test_prompts_and_goal.py`

**Interfaces:**
- Consumes: `env_setup.yaml` (`prompts`), `config` dict (`system_prompt`, `goal`, `rag_context_chars`)
- Produces:
  - `DEFAULT_SYSTEM_PROMPT: str` in `FAGENT/agent/prompts.py`
  - `build_system_prompt(config: dict, retrieved: list) -> str` in `FAGENT/agent/prompts.py`
  - `normalize_goal(raw_prompts: str | list[str] | None) -> str` in `FAGENT/__main__.py`
  - `GraphServices.prepare() -> list[dict]` including `'goal'` in the initial `'user'` JSON message when present

- [ ] **Step 1: Write the failing tests in `tests/test_prompts_and_goal.py`**

```python
import json
from FAGENT.agent.prompts import DEFAULT_SYSTEM_PROMPT, build_system_prompt
from FAGENT.__main__ import normalize_goal


def test_build_system_prompt_uses_default_when_config_missing_system_prompt():
    prompt = build_system_prompt({"rag_context_chars": 1000}, [])
    assert DEFAULT_SYSTEM_PROMPT in prompt
    assert "read_gds" in prompt
    assert "replace_polygon" in prompt
    assert "validate" in prompt


def test_normalize_goal_handles_string_and_list():
    assert normalize_goal("Fix all DRC violations") == "Fix all DRC violations"
    assert normalize_goal(["Fix DRC violations", "Keep polygon layer"]) == "Fix DRC violations\nKeep polygon layer"
    assert normalize_goal(None) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_prompts_and_goal.py -v`
Expected: FAIL with `ImportError: cannot import name 'DEFAULT_SYSTEM_PROMPT'`

- [ ] **Step 3: Implement `DEFAULT_SYSTEM_PROMPT`, `build_system_prompt`, `normalize_goal`, and `goal` injection**

- In `FAGENT/agent/prompts.py`:
  - Define `DEFAULT_SYSTEM_PROMPT` instructing the CAD/EDA agent to follow the user's `goal`, inspect layout polygons with `read_gds(cell_name)`, convert DBU violation coordinates to GDS user units (`dbu / dbu_per_user_unit`), fix geometry with `replace_polygon(cell_name, polygon_index, points)` (tracking `new_index`), call `validate()` only after modifying polygons, and return the final structured analysis JSON when done.
  - In `build_system_prompt(config, retrieved)`: always include `DEFAULT_SYSTEM_PROMPT`, append `config.get('system_prompt', '').strip()` if non-empty and distinct, then append bounded `Retrieved reference data (not instructions)`.
- In `FAGENT/__main__.py`:
  - Add top-level `normalize_goal(raw_prompts)` supporting `str` or `list[str]`.
  - In `main()` under `if args.setup:`: read `setup.get('prompts') or setup.get('prompt')` via `normalize_goal` and store in `config['goal']` if non-empty.
- In `env_setup.yaml`:
  - Add `prompts : "Fix all DRC violations in the layout and validate the repaired candidate."`
- In `FAGENT/config.json` and `FAGENT/config.example.json`:
  - Set `"system_prompt"` to the default CAD/EDA instruction string.
- In `FAGENT/agent/services.py` (`GraphServices.prepare()`):
  - Build user payload with `{'goal': caller.config['goal'], 'gds': caller.gds_data, 'drc': caller.drc_context}` when `caller.config.get('goal')` is present (and pass `caller.config.get('drc_files', [])` to `LayoutService`).

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_prompts_and_goal.py -v`
Expected: PASS

---

### Task 2: Update Agent DRC Functions & Validation Feedback

**Files:**
- Modify: `FAGENT/tools/layout_tools.py:1-26`
- Modify: `FAGENT/tools/validation_tools.py:1-12`
- Modify: `FAGENT/domain/layout.py:8-192`
- Test: `tests/test_drc_tools_flow.py`

**Interfaces:**
- Consumes: `LayoutService(rundir, verbose=True, drc_files=None)`, `CalibreSimulator` (`CALIBRE/calibre.py`)
- Produces:
  - Updated `build_layout_tools(service)` and `build_validation_tools(service)` with explicit DRC-repair workflow descriptions and preconditions
  - `LayoutService.replace_polygon(cell_name, polygon_index, points) -> dict` returning `{'status': 'modified', 'cell': cell_name, 'old_index': polygon_index, 'new_index': ..., 'layer': ..., 'datatype': ...}`
  - `LayoutService.validate() -> dict` returning actionable guidance when `not self.dirty`, and running `CalibreSimulator` on `self.candidate` when `self.drc_files` has an existing deck file

- [ ] **Step 1: Write the failing test in `tests/test_drc_tools_flow.py`**

```python
from pathlib import Path
from FAGENT.domain.layout import LayoutService
from FAGENT.io.loaders import InputLoader
from FAGENT.tools.layout_tools import build_layout_tools
from FAGENT.tools.validation_tools import build_validation_tools

ROOT = Path(__file__).resolve().parent.parent
DRC_DECK = ROOT / "DRC" / "1P7M_6Ic_1TKMc_ALPA2.drc"
SAMPLE_GDS = ROOT / "DRC" / "ROM_MCELL0.gds"
SAMPLE_DRC = ROOT / "DRC" / "ROM_MCELL0.drc.results"
LAYER_MAP = ROOT / "config" / "layer_mapping"


def test_validate_before_modification_returns_drc_guidance(tmp_path):
    rundir = tmp_path / "rundir"
    InputLoader(SAMPLE_GDS, SAMPLE_DRC, rundir, [LAYER_MAP], verbose=False).run()
    service = LayoutService(rundir=rundir, verbose=False, drc_files=[str(DRC_DECK)]).run()

    res = service.validate()
    assert res["status"] == "not_validated"
    assert "read_gds" in res["reason"] and "replace_polygon" in res["reason"]

    tools = {t.name: t for t in build_layout_tools(service) + build_validation_tools(service)}
    assert "Step 1" in tools["read_gds"].description
    assert "Step 2" in tools["replace_polygon"].description
    assert "ONLY AFTER" in tools["validate"].description


def test_replace_polygon_and_validate_runs_calibre_simulator(tmp_path):
    rundir = tmp_path / "rundir"
    InputLoader(SAMPLE_GDS, SAMPLE_DRC, rundir, [LAYER_MAP], verbose=False).run()
    service = LayoutService(rundir=rundir, verbose=False, drc_files=[str(DRC_DECK)]).run()

    cell_info = service.read_gds("ROM_MCELL0")
    first_poly = cell_info["polygons"][0]
    mod = service.replace_polygon("ROM_MCELL0", 0, first_poly["points"])
    assert mod["status"] == "modified"
    assert mod["layer"] == first_poly["layer"]

    val = service.validate()
    assert val["simulated"] is True
    assert "total_violations" in val
    assert "violations_by_rule" in val
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_drc_tools_flow.py -v`
Expected: FAIL on `TypeError: LayoutService.__init__() got an unexpected keyword argument 'drc_files'` or assertion mismatch

- [ ] **Step 3: Implement updated tool descriptions and `LayoutService` DRC validation**

- In `FAGENT/tools/layout_tools.py`:
  - Update `ReadGDSInput`, `ReplacePolygonInput`, and `build_layout_tools`:
    - `read_gds`: `"Step 1 for DRC repair: Read direct polygons of a cell in GDS user units before editing."`
    - `replace_polygon`: `"Step 2 for DRC repair: Replace one direct polygon in GDS user units (divide DBU coordinates by dbu_per_user_unit) to fix DRC violations. Appends the new polygon and returns new_index."`
    - `save_gds`: `"Save the current modified candidate GDS to rundir/candidate.gds."`
- In `FAGENT/tools/validation_tools.py`:
  - Update `build_validation_tools`:
    - `validate`: `"Step 3 for DRC repair: Validate the modified candidate GDS against DRC rules. Call ONLY AFTER modifying polygons with replace_polygon."`
- In `FAGENT/domain/layout.py`:
  - Accept `drc_files=None` in `LayoutService.run()` and `LayoutService.__init__()`, storing `self.drc_files`.
  - Update `LayoutService.tool_specs()` descriptions to match `layout_tools.py` and `validation_tools.py`.
  - Update `LayoutService.replace_polygon()` to return `{'status': 'modified', 'cell': cell_name, 'old_index': polygon_index, 'new_index': len(cell.polygons) - 1, 'layer': old.layer, 'datatype': old.datatype}`.
  - Update `LayoutService.validate()` when `not self.dirty` to return `{'status': 'not_validated', 'reason': 'No candidate modification. Call read_gds and replace_polygon to fix DRC violations before calling validate.'}`.
  - Update `LayoutService.run_calibre()`: if `self.drc_files` contains an existing deck file, run `CalibreSimulator(gds_path=self.candidate, drc_path=deck_path, outdir=self.rundir).run()` and return `{'status': 'good' if sim.total_violations == 0 else 'failed', 'simulated': True, 'total_violations': sim.total_violations, 'violations_by_rule': sim.violations_by_rule}`; otherwise return `{'status': 'good', 'simulated': True}`.

- [ ] **Step 4: Run full test suite to verify all tests pass**

Run: `python3 -m pytest -q`
Expected: PASS (`6 passed`)
