# DRC repair checklist and enclosure inspection

## Goal and scope

For each DRC run, let an agent build and maintain a checklist of individual report violations, inspect layout polygons by stable IDs, repair them, and check geometric enclosure. Present progress and evidence to human review. The user supplies task instructions through the `prompt` or `prompts` entry in `env_setup.yaml`; density or area rules are skipped only when those instructions say so. The current setup mentions density, not area.

## Flow and checklist

The DRC workflow runs `read` → `plan` → `repair` → `human_review`. `plan` calls the agent with the DRC report, task goal, and relevant rule-deck context. It produces one checklist task per report geometry violation, including skipped violations with reasons. A report whose counts cannot be reconciled with its geometry must retain an unresolved task rather than silently omit violations. The run stores the checklist as structured JSON in its own run directory. A fresh run creates a fresh checklist; resuming its checkpoint restores that same run's checklist and candidate.

Each task records a task ID, rule name, report geometry ID, intended work, status, reason, referenced polygon IDs, and evidence. Report geometry IDs are scoped to their rule and are distinct from layout polygon IDs. Supported states include pending, in progress, pass, skipped, and unresolved. Example:

```json
{"task_id":"task-1","rule":"CT_EN_7a_7b","violation_id":1,"work":"Inspect enclosure","status":"pending","reason":"","polygon_ids":[],"evidence":[]}
```

The agent reads and updates tasks through dedicated tools. The tools validate structure, task identity and allowed states and persist updates; the agent decides the state, including pass, and records its reasoning and evidence. Every repair call receives current checklist information. Changes to geometry may invalidate earlier evidence: affected tasks must be reconsidered and their previous evidence identified as stale. The design must prevent accidental loss of tasks on updates and keep checkpoint state and the persisted file consistent on resume.

When all tasks are pass or intentionally skipped, repair ends and the flow presents the checklist to human review. When tasks cannot be resolved or lack suitable checks, the agent records the reason and ends with incomplete status for review. The iteration limit remains a fallback; exhaustion is incomplete. Passing checklist tasks represents agent assessment, not a full DRC pass. Running the actual DRC remains necessary to verify all rules and detect new violations.

## Polygon identity

Assign each layout polygon an ID scoped to its cell and stable throughout one run. Replacing a polygon preserves its ID, and editing another polygon does not change it. Inspection exposes ID, layer, datatype and coordinates; replacement and cover checks address polygons by ID. Save and restore the ID-to-geometry mapping alongside the candidate and checkpoint, and detect missing or inconsistent mappings rather than silently choosing another polygon. New runs create fresh mappings.

Initial parsed GDS `poly` data may include path-derived geometry, whereas the current editor operates on direct polygons. Identify the object type and ensure IDs in initial context and inspection consistently reference the same editable polygon; do not treat paths or referenced geometry as editable direct polygons. Unsupported geometry must be clearly identified. IDs must not depend on transient array indices after replacement.

## Enclosure check

Expose `check_cover` with the cell, IDs of the inner and covering polygons, and the minimum enclosure in GDS user units. The agent derives candidate layer relationships and thresholds from the rule deck and report; record the source and assumptions. If the rule cannot be interpreted confidently or requires unsupported conditions, the corresponding task remains unresolved. This two-polygon geometric check does not independently establish equivalence to a DRC deck rule involving derived layers, exceptions, directional constraints, or multiple shapes.

First establish full coverage, then compare the shortest boundary-to-boundary distance with the required enclosure. Return pass for full coverage with sufficient distance, fail otherwise, and unknown with a reason for invalid IDs or geometry, missing thresholds, or unsupported cases. Results include polygon IDs, units, measured distance where meaningful, required enclosure, and all violating boundary segments on both polygons with their coordinates and associations where determinable.

For an inside portion with insufficient enclosure, the shortage is required enclosure minus measured distance. For a portion outside the covering polygon, the shortage is its distance to the cover boundary plus required enclosure; for example, 0.02 µm outside with 0.03 µm required gives a shortage of 0.05 µm. Report shortage for each violation, including enough location and pairing information to interpret it; do not invent a corresponding edge if the association is ambiguous. Geometry precision and equality at the threshold must be explicitly handled using GDS precision and documented in the implementation.

## Verification

Test per-violation planning, skipped and unresolved tasks, checklist persistence and resume, and termination behavior. Test stable IDs across edits and restore, including multiple cells, paths, and invalid IDs. Test coverage and enclosure for pass, boundary equality, insufficient margin, partial and complete outside geometry, multiple violating segments, nonrectangular polygons, and numerical precision. Verify that task status is agent-controlled, stale evidence is not treated as current after edits, and checklist completion is never reported as a full DRC validation. Exercise the existing ROM example with its density, area and enclosure violations without hard-coding its rule names or dimensions.
