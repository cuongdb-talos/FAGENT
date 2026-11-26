# Rule-Based DRC Scope Classification Implementation Plan

> **For agentic workers:** Use executing-plans to execute the tasks inline. Steps use checkbox syntax.

**Goal:** Let the configured model classify each distinct DRC rule against the user's goal using its full deck expression, then apply that decision to its pending markers.

**Architecture:** The existing read step groups pending checklist entries by exact rule name and obtains deck evidence once per rule. The existing scope classifier sends one request without tools, validates one decision per rule, and maps decisions back to every pending marker. Existing graph topology and the marker-specific repair flow remain intact.

**Tech stack:** Python, JSON Schema, pytest, existing GraphServices and checklist.

**Design:** User-approved conversation on rule-level classification. Classifying a rule in scope does not imply its repair implementation is supported.

## Constraints and review focus

- Preserve report ordering, task IDs, marker occurrences, pre-existing skipped/unresolved entries, trace and candidate state.
- Send actual configured goal, constraints and deck text, including supported rules. Do not derive a layer from a rule name.
- Treat missing or ambiguous deck evidence explicitly. The model may return uncertain; never fabricate expressions.
- Reject duplicate, missing or unknown rule decisions and blank reasons before updating any pending marker.
- If classification fails, record unresolved for the entire submitted batch with a reason and do not enqueue those markers.
- Classify density against the user's goal with the model. Preserve entries already unresolved for missing marker geometry and independently justified existing skips; do not add new keyword-based exclusions.

## Task 1: Rule-level classifier contract

**Files:** Modify `FAGENT/tasks/drc/scope_classifier.py`, `FAGENT/tasks/drc/schemas/scope.json`, `FAGENT/tasks/drc/prompts/scope.md`, `tests/test_drc_scope_classifier.py`.

**Interface:** `ScopeClassifier.run(services, goal: str, constraints: list[str], rules: list[dict]) -> dict` stores and returns a mapping from exact rule name to validated decision row. Each input record has `rule`, `rule_text`, `evidence_status`, `task_ids`; include `target_layer` only if evidenced. The model returns `{"decisions":[{"rule":"...","decision":"in_scope|out_of_scope|uncertain","reason":"..."}]}`.

**Example input:** `{"goal":"Fix M1 and CT errors, ignore density","rules":[{"rule":"CT_R_11","rule_text":"((CT NOT (M1 AND (GT OR AA))) NOT INSIDE INST) NOT INSIDE EXCLU","evidence_status":"available","task_ids":["task-5","task-6"]}]}`.

**Example output:** `{"decisions":[{"rule":"CT_R_11","decision":"in_scope","reason":"The condition checks CT coverage by M1."}]}`. Both task IDs inherit this decision.

- [ ] Write tests for one request per batch, empty tools, the full original expression, goal/constraints, trace-facing schema, empty input, and invalid JSON, missing, extra, duplicate and blank rule decisions. Test supported and unsupported rule evidence without treating repair support as scope evidence.
- [ ] Run `python -m pytest tests/test_drc_scope_classifier.py -q` and observe the new contract tests fail.
- [ ] Implement rule-level schema, prompt and validation; instruct the model to decide scope from goal and rule evidence rather than repair support. Keep deck text as data.
- [ ] Run the same command and require all tests to pass.

## Task 2: Group rules and update the checklist

**Files:** Modify `FAGENT/tasks/drc/steps.py`, `tests/test_drc_five_tool_flow.py`, `tests/test_drc_queue_integration.py`, related scripted test clients, and `FAGENT/tasks/README.md`.

- [ ] Add integration tests with CT_R_11, GT_A_13, a density rule and two markers of the same rule. Assert one entry per rule, exact deck text, one decision mapped to both markers, unchanged task order, and no repair request for excluded tasks. Test two goal phrasings, absent deck, invalid batch, empty pending set and no partial update.
- [ ] Run the targeted tests and observe the new assertions fail.
- [ ] Group pending tasks by rule in report order. Resolve exact configured deck evidence once per rule and send full rule text even when the parser reports unsupported. Remove goal-keyword density skipping for newly constructed entries so the scope model receives density rules with markers. Keep missing-geometry handling and existing checklist data intact. Apply `out_of_scope` as `skipped`, `uncertain` as `unresolved`, and keep `in_scope` pending; preserve reason and queue order.
- [ ] Update scripted model doubles to return one decision per rule and verify repair requests remain per marker with the existing tools and schema.
- [ ] Run `python -m pytest tests/test_drc_scope_classifier.py tests/test_drc_five_tool_flow.py tests/test_drc_queue_integration.py -q`, then run the full applicable test suite and `git diff --check`.
- [ ] Inspect a generated `conversation.json` to verify full deck expressions and no tools in the scope request, plus one subsequent repair request per in-scope marker. Inspect `checklist.json` for rule decisions and reasons.
- [ ] Locate the representative deck rule with `grep -n -A 5 '^CT_R_11[[:space:]]*{' DRC/1P7M_6Ic_1TKMc_ALPA2.drc`. Actual runs must use configured deck paths.
- [ ] Document rule-level grouping, uncertain/error handling and the separate repair-support decision. Report whether real model and Calibre validation were available; scripted tests alone do not establish repair correctness.
