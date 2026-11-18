# FAGENT Ollama and Human Review Design

## Objective

Run both reasoning and embeddings through Ollama, retaining `gemma4` for reasoning, improve CPU embedding performance using measurable optimizations, and require human review before learning from a run. The target deployment is an AMD EPYC 7H12 server with approximately 60 GB available RAM and no GPU. Select an embedding model and batch configuration using measurements on that server rather than assuming a winner.

## Current flow

`cmd` selects `config.agy.json`, so its reasoning provider is AGY. Both current configurations already point embeddings to local Ollama. `Agent_caller` writes the run artifacts and immediately invokes `Agent_learn`; learned records are subsequently eligible for retrieval without human review. Validation currently reports a simulated result and must remain distinguishable from a real Calibre run.

## Provider and embedding

- Use Ollama for reasoning and embeddings; the normal command selects the Ollama configuration and `gemma4` remains the reasoning model. The supported FAGENT execution flow no longer routes reasoning through the AGY adapter.
- Keep the existing hybrid ranking using exact rule matches, lexical matching and semantic similarity. Only eligible records participate in retrieval and embedding work.
- Use the Ollama embedding API with configurable embedding model and batch size. Batch missing document embeddings and reuse cached vectors; embed the query separately. Avoid unnecessary repeated client construction and per-record requests.
- Cache identity must capture all settings that affect vector compatibility, including model identity and query/document formatting. A changed identity invalidates the affected vector cache. Do not compare vectors from incompatible identities or dimensions.
- Preserve the current knowledge data while rebuilding caches. Embedding errors are visible and fall back to exact-rule and lexical retrieval; reasoning errors are reported clearly.
- Compare candidate Ollama embedding models and batch sizes against the current implementation on the target CPU. Measure cold indexing, warm retrieval, query latency, memory use and retrieval quality on a representative DRC query set. Record hardware, versions and settings. Select the default from measured results; no speedup or quality guarantee is assumed before measurement.

## Interactive review flow

1. The agent processes the task using `gemma4` and the existing functions. It displays its result and validation source in the terminal.
2. The user enters free-form review feedback in the terminal. Append the feedback to the conversation as a user message and return it to the agent.
3. The agent may call functions to continue work, ask for clarification or propose saving knowledge. After further work, it returns to review. A model request or function error, or reaching an iteration limit, is surfaced to the user rather than treated as task completion.
4. Saving a run's knowledge requires both the user's explicit confirmation that the task is complete and an explicit request to save knowledge. Both must be established from the user's review, not inferred solely from a model response. Ambiguous feedback requires clarification. Incomplete tasks are not learned, even if the user requests saving them.
5. Enforce the save gate in the orchestrator; a model tool call alone cannot authorize persistence. Once both conditions are satisfied, the agent may initiate the save. Record the review and save decision alongside run artifacts and attach review provenance to learned records.
6. If the user ends the interaction, interrupts it, or no review input is available, do not save knowledge without the explicit required review. Retain available run artifacts and conversation where possible.

Human completion is an assessment of the task, not proof of Calibre validation. Preserve validation status and source, including `simulated`, separately from human feedback. A simulated validation must not be represented as a real DRC pass.

## Existing knowledge

Keep existing learned case and run-context records intact but exclude them from retrieval unless they have a valid human review satisfying the new gate. Legacy records without such provenance are ineligible. Mandatory instructions and independently managed reference documents remain usable under their existing roles. Do not relabel legacy data as reviewed automatically. This change does not require a bulk legacy-review interface; records remain retained for a later review workflow.

## Representative interaction

Input: the agent reports a candidate layout and `validation_source: simulated`. The user says: `CT enclosure is still wrong; inspect it and repair it.`

Output: the agent continues with functions and asks for review again; no knowledge is stored.

Input: the user says: `The task is complete. Save the knowledge from this run.`

Output: after the orchestrator verifies both explicit conditions, the agent can store the run's knowledge with the review and the separate simulated validation provenance.

Input: the user says: `The task is incomplete, but save this experience.`

Output: no knowledge is stored because the user did not confirm completion.

## Verification

Use focused tests for explicit completion and save permission, ambiguous and negative reviews, review-driven tool calls, repeated review, interruption and missing input, and simulated validation provenance. Verify legacy exclusion and continued access to mandatory instructions and reference documents. Test batch embedding, cache reuse and invalidation, fallback, and provider configuration without relying on a live service. Exercise the complete workflow with a real Ollama instance and benchmark representative workloads on the target server before claiming performance or retrieval-quality results.

Relevant documentation section: `grep -n 'Hybrid RAG\|Calibre và validation\|AGY' README.md`.
