# DRC LangGraph Context Implementation Plan

**Goal:** Make the read node assemble DRC, GDS and retrieved knowledge, plan build a report-driven checklist, and agent execute against that checklist.

**Architecture:** Initialize runtime services before graph invocation; the DRC read node produces a serializable context in graph state and conversation. The plan node consumes this context and persists the reconciled checklist. The agent receives the context and checklist through state and may inspect or edit layout through declared tools.

**Tech Stack:** Python, LangGraph, pytest.

**Input example:** A parsed DRC report with a rule and violation geometry, parsed GDS, retrieved rule knowledge, and a configured goal.

**Output example:** A state context containing `drc`, `gds`, `knowledge`, and `goal`; a checklist task for each report violation; tool calls and candidate only when the agent makes an edit.

- [x] Add failing tests covering context provenance, graph routing, checklist handoff, and conversation snapshots.
- [x] Move knowledge retrieval and context assembly into the DRC read action, with explicit state fields.
- [x] Enable read → plan → agent and checklist tools; preserve incomplete status without verified validation.
- [x] Run focused tests and inspect real parsed run artifacts; report simulator limitations.
