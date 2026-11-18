# FAGENT LangGraph Normalization Design

## Goal and scope

Normalize FAGENT's names and responsibilities and introduce LangGraph orchestration with LangChain-compatible tools and model interfaces. Preserve the existing local llama.cpp workflow, CAD behavior, CLI, user review authorization, hybrid retrieval and output artifacts. The removed legacy mandatory/ and references/ directories are intentionally absent; do not restore them or silently require them. Support explicitly configured reference sources where applicable.

## Architecture

Use lowercase Python packages: `agent`, `models`, `tools`, `domain`, `io`, and `rag`. Keep `python -m FAGENT` as the CLI entry point. Separate the GDS and DRC domain services and loaders from model-visible tools. Keep the GDS parser independent of LangChain. Split chat and embedding adapters by responsibility, preserving in-process llama.cpp and existing JSON-call compatibility until an equivalent integration is verified. Wrap the four existing operations (`read_gds`, `replace_polygon`, `save_gds`, `validate`) in typed tools; retain their existing names and contracts.

Represent the orchestration as a LangGraph state graph with explicit input preparation, retrieval/context assembly, model/tool loop, validation result handling, human review, finalization and conditional knowledge storage. State contains serializable messages, input and candidate paths, iteration count, validation, result and review evidence. Runtime services and model clients are supplied as dependencies rather than persisted in state. Continue enforcing the iteration limit and preserve tool-call IDs and error feedback.

A review node uses interrupt/resume with a checkpointer and thread ID, while the CLI obtains user input and resumes the graph. Review authorization continues to require explicit completion and save intent; layout modification or validation invalidates prior authorization. Saving knowledge remains conditional on authorization and current structured analysis. Resume and retry must not duplicate writes or unintentionally repeat side effects.

## RAG and data

Separate source ingestion, record persistence, retrieval, and approved-case extraction. Preserve record IDs and metadata, exact-rule priority, hybrid scoring, lexical fallback, embedding identity and caching, and filtering of review-authorized learned records. Preserve existing `records.json` and `rag.sqlite3` data and migration behavior; do not delete, overwrite, or relocate existing knowledge without a migration strategy. The system prompt must not silently depend on deleted directories; document and configure any replacement instructions explicitly.

## Validation and compatibility

`run_calibre()` currently returns simulated results. Preserve the simulated marker, and never mark simulated or absent DRC validation as verified. Preserve GDS input protection, geometry checks, review invalidation and output formats (`candidate.gds`, `conversation.json`, `result.json`). Keep old imports working through compatibility shims during migration, and update internal imports, tests, documentation and dependencies. Configuration supports local model paths without committing machine-specific defaults as required values.

## Error handling and testing

Report model, tool, embedding and persistence errors explicitly while preserving lexical fallback. Test the graph with deterministic fake models and tools; cover multiple tool calls, bounded iteration, pause/resume, invalidation of approval, no unauthorized knowledge storage, simulated versus real validation, GDS input protection and existing retrieval rankings. Run existing targeted FAGENT tests and new graph integration tests. Check compatibility with the actual local GGUF separately when the model and dependencies are available; do not claim that a fake-model test verifies real model tool calling.

## Representative flow

Input: CLI receives a GDS path, Calibre DRC results and configuration. The graph retrieves relevant records, asks the model for an allowed tool, updates a candidate and validates it. Output: a candidate GDS, conversation and result JSON; a simulated validation remains `incomplete`. The graph pauses for review, and stores a case only after explicit completion and save authorization.

## Migration sequence

1. Establish compatibility tests and isolate domain operations and configuration.
2. Introduce lowercase packages and compatibility shims; split local model and RAG responsibilities.
3. Add typed tool wrappers and LangGraph state/nodes with checkpointed review.
4. Switch the CLI to the graph, preserve artifacts and document configuration and migration.
5. Run targeted regression and integration tests and assess actual local model compatibility.
