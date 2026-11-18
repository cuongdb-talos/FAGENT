# Agent Learn (`FAGENT/learn.py`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `FAGENT/learn.py` (`Agent_learn`) to extract DRC repair knowledge from validated runs using a hybrid (deterministic + LLM) pipeline, persist Dual-Format knowledge (`.agent/knowledge/knowledge_db.json` and `.agent/knowledge/learned_rules.md`), and automatically trigger learning in `Agent_caller` when DRC validation passes and final results are stored.

**Architecture:** `Agent_learn` consumes `drc.json`, `conversation.json`, and `result.json` from a verified `rundir`. It extracts deterministic tool sequences, coordinate deltas (in GDS user units), and pitfalls per DRC rule, optionally refines recipes via the configured LLM client, merges rules by `rule_name` into `knowledge_db.json`, and renders `learned_rules.md`. `Agent_caller` records `status: 'verified'` when `last_validation` passes, calls `Agent_learn` after writing `result.json`, and includes `.agent/knowledge/*.md` in `system_prompt()`.

**Tech Stack:** Python 3.9+, `json`, `pathlib`, OpenAI SDK / `AgyClient`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-11-agent-learn-design.md` and `.agent/AI_Workflow.md` (locate workflow sections with `grep -nE 'Giai đoạn 6|Agent_learn|knowledge_db' .agent/AI_Workflow.md`).

## Global Constraints

- Never overwrite the source GDS or DRC input; check resolved output paths before writing `knowledge_db.json` or `learned_rules.md`.
- Keep class `run` methods first in every class (`Agent_learn.run` must be the first method in `Agent_learn`).
- No nested `def` functions anywhere in production code.
- Store all primary outputs and intermediate states on `self` attributes (`self.rundir`, `self.knowledge_dir`, `self.drc_data`, `self.conversation_data`, `self.result_data`, `self.extracted_rules`, `self.knowledge_db`, `self.markdown_content`, `self.learned`) and return `self` from `Agent_learn.run()`.
- No hard-coded fixed paths; allow `rundir`, `knowledge_dir`, `config`, and `client` to be configured with sensible project-relative defaults.
- Preserve Zero-Assumption validation: `Agent_learn` must only run when `result.json` has `status == 'verified'` (which requires `last_validation['status'] in ('verified', 'pass', 'good')`).

## Review Focus

- Unverified or incomplete runs (`status != 'verified'`) must be skipped by `Agent_learn.run()` without creating or mutating `knowledge_db.json` (Task 1 & Task 3).
- Repeated learning on the same `rule_name` must increment `hit_count`, deduplicate `pitfalls`, and cap `coordinate_delta_examples` instead of duplicating rule entries (Task 1).
- LLM failure, timeout, or malformed JSON during hybrid refinement must fall back cleanly to deterministic extraction without failing the run (Task 2).
- `knowledge_db.json` or `learned_rules.md` path aliasing input GDS or DRC files must raise `ValueError` before writing (Task 1).
- Modified polygon edits invalidated by a subsequent `replace_polygon` without re-running `validate()` must not be treated as verified in `Agent_caller` (Task 3).

## File Map

- Create: `FAGENT/learn.py` — `Agent_learn` class implementing deterministic extraction, LLM refinement, `knowledge_db.json` merging, and `learned_rules.md` rendering.
- Modify: `FAGENT/caller.py` — Track verified `last_validation` state, write `status: 'verified'` to `result.json` when validation passes, invoke `Agent_learn` after saving final results, and load `.agent/knowledge/*.md` in `system_prompt()`.
- Modify: `.agent/AI_Workflow.md` — Document `Agent_learn`, Stage 6 (Knowledge Extraction & Continuous Learning), and `.agent/knowledge/` artifacts.
- Modify: `tests/test_agent.py` — Add unit and integration tests for `Agent_learn` deterministic extraction, hybrid LLM refinement fallback, caller trigger on validation pass, and system prompt knowledge injection.

---

### Task 1: Implement `Agent_learn` deterministic extraction and Dual-Format persistence

**Files:**
- Create: `FAGENT/learn.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Consumes: `<rundir>/drc.json`, `<rundir>/conversation.json`, `<rundir>/result.json`, optional `<rundir>/gds.json`.
- Produces:
  - `Agent_learn(rundir=None, knowledge_dir=None, config=None, client=None, verbose=True)`
  - `Agent_learn.run(self, rundir=None, knowledge_dir=None, config=None, client=None, verbose=None) -> Agent_learn` (must be first method in class)
  - Attributes on `self`: `self.learned` (`bool`), `self.extracted_rules` (`dict`), `self.knowledge_db` (`dict`), `self.markdown_content` (`str`), `self.db_path` (`Path`), `self.md_path` (`Path`).

- [x] **Step 1: Write the failing test in `tests/test_agent.py`**

```python
def test_agent_learn_deterministic_and_dedup(tmp_path):
    from FAGENT.learn import Agent_learn

    rundir = tmp_path / 'rundir'
    know_dir = tmp_path / 'knowledge'
    rundir.mkdir()

    (rundir / 'gds.json').write_text(json.dumps({'source': str(tmp_path / 'src.gds')}))
    (rundir / 'drc.json').write_text(json.dumps({
        'source': str(tmp_path / 'src.drc'),
        'top_cell': 'ROM_MCELL0',
        'dbu_per_user_unit': 1000,
        'total_violations': 2,
        'rules': [{'name': 'BD_R_1_GT', 'total_count': 2, 'description': ['Gate enclosure >= 0.05 um']}],
    }))
    conversation = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': '{}'},
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c1', 'type': 'function', 'function': {'name': 'read_gds', 'arguments': '{"cell_name": "ROM_MCELL0"}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({
            'cell': 'ROM_MCELL0',
            'polygons': [{'index': 0, 'layer': 1, 'datatype': 0, 'points': [[0.0, 0.0], [0.22, 0.0], [0.22, 0.1], [0.0, 0.1]]}],
        })},
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c2', 'type': 'function', 'function': {
                'name': 'replace_polygon',
                'arguments': json.dumps({'cell_name': 'ROM_MCELL0', 'polygon_index': 0, 'points': [[0.0, 0.0], [0.25, 0.0], [0.25, 0.1], [0.0, 0.1]]}),
            }}
        ]},
        {'role': 'tool', 'tool_call_id': 'c2', 'content': json.dumps({'status': 'modified', 'new_index': 0})},
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c3', 'type': 'function', 'function': {'name': 'validate', 'arguments': '{}'}}
        ]},
        {'role': 'tool', 'tool_call_id': 'c3', 'content': json.dumps({'status': 'verified'})},
        {'role': 'assistant', 'content': json.dumps({
            'decision': 'completed',
            'status': 'verified',
            'reason': 'Fixed BD_R_1_GT enclosure',
            'violations_analysis': [{'rule': 'BD_R_1_GT', 'action': 'repaired', 'details': 'Extended X edge from 0.22 to 0.25 um'}],
            'recommendations': 'Check neighbor spacing after extending gate',
        })},
    ]
    (rundir / 'conversation.json').write_text(json.dumps(conversation))
    (rundir / 'result.json').write_text(json.dumps({
        'status': 'verified',
        'reason': 'DRC validation passed',
        'response': json.loads(conversation[-1]['content']),
    }))

    learner = Agent_learn(rundir=rundir, knowledge_dir=know_dir, verbose=False).run()
    assert learner.learned is True
    assert (know_dir / 'knowledge_db.json').exists()
    assert (know_dir / 'learned_rules.md').exists()

    rule_entry = learner.knowledge_db['rules']['BD_R_1_GT']
    assert rule_entry['rule'] == 'BD_R_1_GT'
    assert rule_entry['action_pattern'] == 'repaired'
    assert rule_entry['tool_sequence'] == ['read_gds', 'replace_polygon', 'validate']
    assert rule_entry['hit_count'] == 1
    assert rule_entry['coordinate_delta_examples'][0]['before'][1] == [0.22, 0.0]
    assert rule_entry['coordinate_delta_examples'][0]['after'][1] == [0.25, 0.0]
    assert 'BD_R_1_GT' in learner.markdown_content

    # Second run increments hit_count without duplicating rule keys
    learner2 = Agent_learn(rundir=rundir, knowledge_dir=know_dir, verbose=False).run()
    assert learner2.knowledge_db['rules']['BD_R_1_GT']['hit_count'] == 2
    assert learner2.knowledge_db['total_runs_learned'] == 2

    # Unverified run is skipped
    (rundir / 'result.json').write_text(json.dumps({'status': 'incomplete', 'reason': 'Not validated'}))
    know_dir_unverified = tmp_path / 'know_unverified'
    unverified_learner = Agent_learn(rundir=rundir, knowledge_dir=know_dir_unverified, verbose=False).run()
    assert unverified_learner.learned is False
    assert not (know_dir_unverified / 'knowledge_db.json').exists()

    # Path aliasing input GDS/DRC is rejected before writing
    (rundir / 'result.json').write_text(json.dumps({'status': 'verified', 'reason': 'DRC validation passed'}))
    alias_dir = tmp_path / 'alias_know'
    alias_dir.mkdir()
    src_gds = tmp_path / 'src.gds'
    src_gds.write_bytes(b'gds')
    import os
    os.link(src_gds, alias_dir / 'knowledge_db.json')
    with pytest.raises(ValueError, match='overwrite'):
        Agent_learn(rundir=rundir, knowledge_dir=alias_dir, verbose=False).run()
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent.py -k test_agent_learn_deterministic_and_dedup -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'FAGENT.learn'`

- [x] **Step 3: Implement `Agent_learn` in `FAGENT/learn.py`**

Implement `Agent_learn` with `def run(self, rundir=None, knowledge_dir=None, config=None, client=None, verbose=None)` as the first method in the class, followed by `__init__`, `check_output_path`, `extract_deterministic`, `refine_with_llm`, `merge_knowledge`, and `render_markdown` (no nested `def`).
- `run()` loads `result.json`, `conversation.json`, and `drc.json`; if `self.result_data.get('status') != 'verified'`, sets `self.learned = False` and returns `self`.
- `extract_deterministic()` builds rule records keyed by `rule_name` with fields: `rule`, `description`, `category`, `action_pattern`, `tool_sequence`, `golden_fix_recipe`, `coordinate_delta_examples`, and `pitfalls`.
- `merge_knowledge()` loads existing `knowledge_db.json` (if present), increments `total_runs_learned` and each rule's `hit_count`, updates `confidence = min(1.0, round(0.5 + 0.1 * hit_count, 2))`, deduplicates `pitfalls`, and caps `coordinate_delta_examples` at 3.
- `render_markdown()` formats `# Learned DRC Repair Knowledge` with one `## Rule: <name>` section per rule.

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent.py -k test_agent_learn_deterministic_and_dedup -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add FAGENT/learn.py tests/test_agent.py
git commit -m "feat: add Agent_learn deterministic knowledge extraction and dual-format storage"
```

---

### Task 2: Add Hybrid LLM Refinement and Input Overwrite Guards in `Agent_learn`

**Files:**
- Modify: `FAGENT/learn.py`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Consumes: `self.client` (compatible with `client.chat.completions.create(model=..., messages=...)`) and `self.config`.
- Produces: `Agent_learn.refine_with_llm(self) -> dict` updating `category`, `golden_fix_recipe`, and `pitfalls` on `self.extracted_rules` when the LLM returns valid JSON, or preserving deterministic values on error.

- [x] **Step 1: Write the failing test in `tests/test_agent.py`**

```python
def test_agent_learn_hybrid_llm_and_fallback(tmp_path):
    from types import SimpleNamespace
    from FAGENT.learn import Agent_learn

    rundir = tmp_path / 'rundir'
    know_dir = tmp_path / 'knowledge'
    rundir.mkdir()
    (rundir / 'drc.json').write_text(json.dumps({
        'rules': [{'name': 'M1.S.1', 'total_count': 1, 'description': ['Metal1 spacing >= 0.09']}],
    }))
    (rundir / 'conversation.json').write_text(json.dumps([
        {'role': 'assistant', 'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'validate', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': json.dumps({'status': 'verified'})},
    ]))
    (rundir / 'result.json').write_text(json.dumps({'status': 'verified', 'reason': 'DRC validation passed'}))

    class FakeCompletions:
        def create(self, **kwargs):
            payload = {
                'M1.S.1': {
                    'category': 'spacing',
                    'golden_fix_recipe': 'Shift M1 edge by 0.03 um away from neighbor.',
                    'pitfalls': ['Check opposite edge spacing'],
                }
            }
            msg = SimpleNamespace(model_dump=lambda **kw: {'role': 'assistant', 'content': json.dumps(payload)})
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    learner = Agent_learn(
        rundir=rundir,
        knowledge_dir=know_dir,
        config={'model': 'gemma4', 'learn_use_llm': True},
        client=fake_client,
        verbose=False,
    ).run()

    rule = learner.knowledge_db['rules']['M1.S.1']
    assert rule['category'] == 'spacing'
    assert rule['golden_fix_recipe'] == 'Shift M1 edge by 0.03 um away from neighbor.'
    assert 'Check opposite edge spacing' in rule['pitfalls']

    # Failing LLM client falls back cleanly to deterministic extraction
    class BrokenCompletions:
        def create(self, **kwargs):
            raise RuntimeError('LLM offline')

    broken_client = SimpleNamespace(chat=SimpleNamespace(completions=BrokenCompletions()))
    know_dir_fallback = tmp_path / 'knowledge_fallback'
    fallback_learner = Agent_learn(
        rundir=rundir,
        knowledge_dir=know_dir_fallback,
        config={'model': 'gemma4', 'learn_use_llm': True},
        client=broken_client,
        verbose=False,
    ).run()
    assert fallback_learner.learned is True
    assert 'M1.S.1' in fallback_learner.knowledge_db['rules']
```

- [x] **Step 2: Run test to verify it fails or exercises new LLM path**

Run: `python -m pytest tests/test_agent.py -k test_agent_learn_hybrid_llm_and_fallback -v`
Expected: FAIL until `refine_with_llm` is wired into `Agent_learn.run()`

- [x] **Step 3: Implement `refine_with_llm` and path overwrite checks in `FAGENT/learn.py`**

In `Agent_learn.refine_with_llm(self)`:
- Skip if `self.client is None` or `(self.config or {}).get('learn_use_llm', True) is False`.
- Call `self.client.chat.completions.create(model=(self.config or {}).get('model', ''), messages=[...])` with a compact prompt containing `self.extracted_rules`.
- Parse JSON response (stripping optional markdown code fences) and merge non-empty `category`, `golden_fix_recipe`, and `pitfalls` lists into `self.extracted_rules`. Catch all exceptions so offline/malformed LLM responses never abort persistence.

- [x] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_agent.py -k test_agent_learn_hybrid_llm_and_fallback -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add FAGENT/learn.py tests/test_agent.py
git commit -m "feat: add hybrid LLM refinement with deterministic fallback in Agent_learn"
```

---

### Task 3: Integrate `Agent_learn` and `.agent/knowledge/` into `Agent_caller`

**Files:**
- Modify: `FAGENT/caller.py`
- Modify: `.agent/AI_Workflow.md`
- Modify: `tests/test_agent.py`

**Interfaces:**
- Consumes: `Agent_learn` from `FAGENT.learn`, `self.functions.last_validation` from `Agent_functions`.
- Produces:
  - `Agent_caller.run()` sets `self.result['status'] = 'verified'` when `isinstance(self.functions.last_validation, dict)` and `self.functions.last_validation.get('status') in ('verified', 'pass', 'good')`.
  - After writing `conversation.json` and `result.json`, if `self.result.get('status') == 'verified'`, `Agent_caller` runs `self.learner = Agent_learn(rundir=self.rundir, knowledge_dir=self.knowledge_dir, config=self.config, client=self.client, verbose=self.verbose).run()`.
  - `Agent_caller.system_prompt()` reads `*.md` from `('rules', 'contexts', 'skills', 'knowledge')`.

- [x] **Step 1: Write the failing test in `tests/test_agent.py`**

```python
def test_caller_triggers_learn_on_validation_pass_and_injects_system_prompt(tmp_path, monkeypatch):
    from FAGENT.loader import Agent_loader
    from FAGENT.caller import Agent_caller
    from FAGENT.functions import Agent_functions

    rundir = tmp_path / 'rundir'
    know_dir = tmp_path / 'knowledge'
    Agent_loader().run(SAMPLE.parent / 'ROM_MCELL0.gds', SAMPLE, rundir, verbose=False)

    def fake_validate(self):
        self.last_validation = {'status': 'verified', 'total_violations': 0}
        return self.last_validation

    monkeypatch.setattr(Agent_functions, 'validate', fake_validate)

    calls = iter([
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call_read', 'type': 'function', 'function': {'name': 'read_gds', 'arguments': '{"cell_name": "ROM_MCELL0"}'}}
        ]},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call_rep', 'type': 'function', 'function': {
                'name': 'replace_polygon',
                'arguments': json.dumps({'cell_name': 'ROM_MCELL0', 'polygon_index': 0, 'points': [[0.0, 0.0], [0.25, 0.0], [0.25, 0.1], [0.0, 0.1]]}),
            }}
        ]},
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call_val', 'type': 'function', 'function': {'name': 'validate', 'arguments': '{}'}}
        ]},
        {'role': 'assistant', 'content': json.dumps({
            'decision': 'completed',
            'status': 'verified',
            'reason': 'All violations resolved',
            'violations_analysis': [{'rule': 'BD_R_1_GT', 'action': 'repaired', 'details': 'Adjusted gate polygon'}],
            'recommendations': 'Run LVS check',
        })},
    ])
    monkeypatch.setattr(Agent_caller, 'chat', lambda self: next(calls))
    monkeypatch.setattr(Agent_caller, 'create_client', lambda self: None)

    config = {'provider': 'ollama', 'max_iterations': 6, 'knowledge_dir': str(know_dir)}
    caller = Agent_caller(rundir=rundir, config=config, verbose=False)
    result = caller.run()

    assert result['status'] == 'verified'
    saved_result = json.loads((rundir / 'result.json').read_text())
    assert saved_result['status'] == 'verified'
    assert caller.learner is not None
    assert caller.learner.learned is True
    assert (know_dir / 'knowledge_db.json').exists()
    assert (know_dir / 'learned_rules.md').exists()
    assert 'BD_R_1_GT' in caller.system_prompt()
```

- [x] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_agent.py -k test_caller_triggers_learn_on_validation_pass_and_injects_system_prompt -v`
Expected: FAIL because `Agent_caller` currently hard-codes `status: 'incomplete'` and does not call `Agent_learn`.

- [x] **Step 3: Update `FAGENT/caller.py`**

- Import `Agent_learn` from `.learn`.
- In `Agent_caller.__init__`, initialize `self.learner = None` and `self.knowledge_dir = None`.
- In `Agent_caller.run()`, resolve `self.knowledge_dir = Path(self.config['knowledge_dir']).resolve() if self.config.get('knowledge_dir') else None`.
- When the tool loop finishes (`if not calls:`), check if `isinstance(self.functions.last_validation, dict) and self.functions.last_validation.get('status') in ('verified', 'pass', 'good')`. If true, set `status = 'verified'` and `reason = 'DRC validation passed'`; otherwise keep `status = 'incomplete'` and `reason = 'Agent stopped without verified DRC'`.
- After writing `conversation.json` and `result.json`, if `self.result.get('status') == 'verified'`, call `self.learner = Agent_learn(rundir=self.rundir, knowledge_dir=self.knowledge_dir, config=self.config, client=self.client, verbose=self.verbose).run()`.
- In `Agent_caller.system_prompt()`, include `'knowledge'` in the folder tuple `('rules', 'contexts', 'skills', 'knowledge')` and also append any `.md` files from `self.knowledge_dir` if a custom `self.knowledge_dir` outside `.agent/knowledge` was configured.

- [x] **Step 4: Run full test suite to verify everything passes**

Run: `python -m pytest -q`
Expected: All existing and new tests PASS.

- [x] **Step 5: Commit**

```bash
git add FAGENT/caller.py .agent/AI_Workflow.md docs/superpowers/specs/2026-11-11-agent-learn-design.md docs/superpowers/plans/2026-11-11-agent-learn-plan.md tests/test_agent.py
git commit -m "feat: trigger Agent_learn on verified DRC pass and load learned_rules into system prompt"
```
