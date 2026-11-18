# FAGENT Hybrid RAG Design

## Goal and scope

Move FAGENT knowledge and its indexes under `FAGENT/knowledge/`, introduce an independent `Agent_rag` class, and retrieve relevant knowledge for each DRC repair. Preserve mandatory operating instructions in every system prompt. Update `.agent/AI_Workflow.md` to describe the actual implementation and simulated validation. This design does not implement real Calibre validation or fine-tune a model.

## Components and storage

- `FAGENT/rag.py` owns `Agent_rag`: ingestion, document normalization, persistent indexing, embedding, and hybrid retrieval. It exposes methods to index documents and retrieve ranked records; it does not operate on GDS files or execute repairs.
- `FAGENT/knowledge/` contains mandatory instructions, reference documents, learned records, and the local persistent index. Move existing `.agent/rules`, `.agent/contexts`, and `.agent/skills` contents here, retaining their distinction as mandatory instructions or searchable reference material. Avoid maintaining competing copies.
- Store authoritative records with stable IDs, source provenance, content, rule and other available metadata, outcome and validation source. The persistent index is rebuildable from these records. A record per repair case preserves failures and distinct examples; it must not overwrite all prior cases for the same rule.
- Migrate existing `knowledge_db.json` without discarding its contents. Mark legacy provenance and validation as unknown because earlier verified statuses may have been simulated. Avoid treating historical confidence as a measured probability.
- The knowledge location is contained in `FAGENT`; reject an external `knowledge_dir` instead of silently writing elsewhere. Use resolved paths and guard against traversal or aliases outside the permitted root.

## Indexing and retrieval

Use a local SQLite database for records and keyword indexing, with vectors stored locally and cosine similarity calculated using existing Python/NumPy dependencies. Use the configured local Ollama OpenAI-compatible embedding endpoint and a separately configurable embedding model; do not assume a model is installed. Both Ollama and AGY generation providers use the same independently configured local embedding service. Store the embedding model identity and rebuild or reject stale vectors when it changes.

Normalize each knowledge document into meaningful chunks with stable source IDs. Keep repair cases intact when practical, preserve rule names verbatim, and attach their outcome and validation source. Index exact rule identifiers and text alongside vectors. Hybrid retrieval combines exact rule/metadata matching and lexical relevance with embedding similarity; rank exact rule matches ahead of unrelated rules. Make retrieval count and context size configurable. Return provenance, score and outcome labels to the caller. Do not include unbounded retrieved text in prompts.

When there are no matching records, return an empty result and allow the repair flow to continue with mandatory instructions and the input DRC. If the embedding service is unavailable, use exact and lexical retrieval and report the degraded mode; never represent keyword-only results as semantic matches. Indexing persists the source record first and records pending embeddings for later retry. Avoid changing vector model silently or losing existing knowledge on an embedding error.

## Learning and validation

`Agent_caller` writes the conversation and result for both completed and incomplete runs and invokes `Agent_learn` for either outcome. `Agent_learn` extracts case evidence and delegates storage/indexing to `Agent_rag`. Preserve `pass`, `fail`, and `incomplete` as distinct outcomes when evidence supports them; absence of a validation pass is `incomplete`, not automatically `fail`. Store whether validation was simulated, real, or unknown. The current `run_calibre()` simulated `good` result remains accepted by the existing caller; its simulated label must reach persisted results and knowledge. Do not claim real DRC verification from simulated output.

Only associate an operation or error with an individual rule when the available evidence supports the association. Otherwise record it as run-level context, not as a proven rule-specific repair. Preserve coordinate units and source details where available. Reprocessing the same run must not create duplicate evidence or inflate counts; retain distinct runs and outcomes.

## Prompt and migration flow

On each run, load mandatory instructions from their new location, use DRC rule names and descriptions to retrieve context, then provide selected records and their labels to the model alongside the existing GDS and DRC payload. Clearly distinguish retrieved reference content from mandatory system instructions. Never inject all learned cases by default.

Migration must be repeatable, preserve original case data, and support an empty initial database. Update references and tests for the moved instructions. `.agent/AI_Workflow.md` remains at its requested path and documents the real flow, local RAG storage, both outcomes, degraded retrieval, and the simulated Calibre result.

## Verification

Test document and case ingestion, distinct records and idempotent migration, exact rule matching, semantic and lexical ranking, local model mismatch, bounded prompt context, and persistence across instances. Test missing embedding service and empty index, both DRC input formats, pass/fail/incomplete labels, simulated validation provenance, and avoiding unsupported rule-to-operation attribution. Use mocked embedding responses for repeatable automated tests and separately report whether a real local Ollama model and Calibre were available. Run the existing FAGENT tests and applicable CAD/EDA verification checks; a mocked or simulated validation is not a real Calibre run.
