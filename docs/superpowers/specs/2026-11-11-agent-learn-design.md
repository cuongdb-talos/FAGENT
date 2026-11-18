# Agent Knowledge Extraction and Continuous Learning (`Agent_learn`) Design

## Goal and Scope

Add `FAGENT/learn.py` containing `Agent_learn` to collect validated conversation histories (`conversation.json`, `result.json`, `drc.json`), extract structured DRC repair knowledge via a hybrid (deterministic + optional LLM refinement) pipeline, persist the canonical database in `.agent/knowledge/knowledge_db.json`, and render compact Markdown in `.agent/knowledge/learned_rules.md` for automatic injection into `Agent_caller.system_prompt()`.

## Trigger Condition (Strict Validation Pass)

1. In `Agent_caller.run()`, validation passes strictly when `self.functions.last_validation` is a dictionary with `status in ('verified', 'pass', 'good')`.
2. When the tool-calling loop completes after a passing validation, `Agent_caller` sets `self.result['status'] = 'verified'` and `self.result['reason'] = 'DRC validation passed'`.
3. Only after `conversation.json` and `result.json` are safely written to `self.rundir` AND `self.result.get('status') == 'verified'`, `Agent_caller` invokes `Agent_learn(rundir=self.rundir, config=self.config, client=self.client, verbose=self.verbose).run()`.
4. `Agent_learn` can also be invoked directly on any existing `rundir` containing a verified `result.json` and `conversation.json`.

## Dual-Format Knowledge Data Structure (Method 1)

### 1. Canonical JSON Source of Truth (`.agent/knowledge/knowledge_db.json`)

Managed by rule key (`rules[<rule_name>]`):

```json
{
  "version": 1,
  "updated_at": "2026-11-11T00:00:00Z",
  "total_runs_learned": 1,
  "rules": {
    "BD_R_1_GT": {
      "rule": "BD_R_1_GT",
      "description": "Gate boundary enclosure rule",
      "category": "enclosure",
      "action_pattern": "repaired",
      "tool_sequence": ["read_gds", "replace_polygon", "save_gds", "validate"],
      "golden_fix_recipe": "Adjust polygon vertices in user units (DBU / dbu_per_user_unit) to satisfy boundary enclosure.",
      "coordinate_delta_examples": [
        {
          "cell": "ROM_MCELL0",
          "polygon_index": 0,
          "before": [[0.0, 0.0], [0.22, 0.0], [0.22, 0.1], [0.0, 0.1]],
          "after": [[0.0, 0.0], [0.25, 0.0], [0.25, 0.1], [0.0, 0.1]]
        }
      ],
      "pitfalls": [
        "Do not modify polygons with GDS properties",
        "Ensure polygon area > 0"
      ],
      "hit_count": 1,
      "confidence": 0.8
    }
  }
}
```

### 2. Auto-Rendered Markdown (`.agent/knowledge/learned_rules.md`)

Generated deterministically from `knowledge_db.json` so `Agent_caller.system_prompt()` loads it alongside `.agent/{rules,contexts,skills,knowledge}` without JSON token overhead:

```markdown
# Learned DRC Repair Knowledge

## Rule: BD_R_1_GT
- **Category:** enclosure
- **Action Pattern:** repaired
- **Tool Sequence:** read_gds -> replace_polygon -> save_gds -> validate
- **Golden Fix Recipe:** Adjust polygon vertices in user units (DBU / dbu_per_user_unit) to satisfy boundary enclosure.
- **Pitfalls:** Do not modify polygons with GDS properties; Ensure polygon area > 0
- **Confidence / Hits:** 0.8 (1 verified run)
```

## Hybrid Extraction Pipeline (`Agent_learn`)

1. **Deterministic Extraction (`extract_deterministic`):**
   - Reads `drc.json`, `conversation.json`, and `result.json` from `self.rundir`.
   - Extracts rule names, descriptions, and violation counts from `drc.json`.
   - Traces assistant `tool_calls` and corresponding `tool` role outputs in `conversation.json`:
     - Records ordered `tool_sequence`.
     - Computes `coordinate_delta_examples` by matching `read_gds` polygon points (`before`) with `replace_polygon` arguments (`after`) for the same `(cell_name, polygon_index)`.
     - Collects any tool call errors (`{"error": "..."}`) into `pitfalls`.
   - Parses `violations_analysis` and `recommendations` from `result.json['response']` (when structured JSON is present) to populate `action_pattern` and initial `golden_fix_recipe`.
2. **LLM Refinement (`refine_with_llm`):**
   - If `self.client` is provided (or enabled via `config.get('learn_use_llm', True)`), sends a compact summary of the deterministic extraction (excluding raw `gds.json` payloads) to the model requesting a JSON object mapping each rule to `{ "category", "golden_fix_recipe", "pitfalls" }`.
   - Merges valid LLM fields into the deterministic record; falls back cleanly to deterministic values if the provider fails, times out, or returns non-JSON.
3. **Database Merge & Markdown Render (`merge_knowledge` & `render_markdown`):**
   - Upserts each rule into `knowledge_db.json`: increments `hit_count`, updates `confidence = min(1.0, round(0.5 + 0.1 * hit_count, 2))`, deduplicates `pitfalls`, and retains up to `max_examples_per_rule` (default 3) `coordinate_delta_examples`.
   - Writes `.agent/knowledge/knowledge_db.json` and `.agent/knowledge/learned_rules.md` after checking that neither aliases input GDS/DRC paths.

## Coding Standards & Class Architecture

- `Agent_learn.run(...)` is the first method in `Agent_learn`.
- Zero nested `def` functions anywhere in `FAGENT/learn.py` or modified modules.
- All outputs and intermediate states (`self.rundir`, `self.knowledge_dir`, `self.drc_data`, `self.conversation_data`, `self.result_data`, `self.extracted_rules`, `self.knowledge_db`, `self.markdown_content`) are stored on `self`, and `run()` returns `self`.
