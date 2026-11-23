# Compact DRC Context Implementation Plan

**Goal:** Reduce model input while retaining DRC marker identity, geometry, rule knowledge, and shared constraints in the effective goal.

**Architecture:** The read node fetches references once, loads global constraints from knowledge, and returns a compact context; the full parsed report stays in state for checklist reconciliation. Plan and agent consume the same context without further RAG retrieval. DRC marker IDs are never represented as editable GDS polygon IDs.

**Input example:** A rule with marker ID 7 and DBU points, a configured goal, a knowledge reference, and a shared width constraint.

**Output example:** `{"drc":{"top_cell":"cell","dbu_per_user_unit":1000,"rules":[{"name":"rule","geometry":[{"id":7,"points_dbu":[[0,0],[1,1]]}]}]},"knowledge":{"references":[],"constraints":["..."]},"goal":"..."}`.

- [x] Add failing tests for compact context, constraints, goal propagation, missing marker geometry, and one RAG retrieval.
- [x] Implement compact context and shared configurable knowledge constraints without mutating the full report.
- [x] Update planning and repair to use the effective goal, document configuration, and verify using real run artifacts.
