# Full workflow trace design

## Goal

Record the workflow's observable operations, including preparation, DRC reading and RAG retrieval, alongside existing model and tool events. Keep complete event inputs and outputs in `conversation.json` and a readable chronological view in `trace.log`.

## Scope and event contract

Start tracing once a per-cell run directory has been allocated, before input loading. Record preparation and service initialization, workflow start and finish, node start and outcome including routing, DRC report reading, RAG initialization/indexing and retrieval, model requests/responses/errors, tool calls/results/errors, validation and human review when executed, and artifact writing. For each operation record its available inputs and outputs, status, UTC timestamp, and a stable operation identifier linking start and completion. Record failures before propagating or handling the original error. Do not invent information about work that occurred before a run directory exists.

Keep the existing version 2 envelope and existing event types and fields unchanged. Add event types and fields in a backward-compatible manner so the case learner continues extracting assistant and tool messages. Preserve event ordering across preparation, graph execution and resume. On resume reload the existing trace, continue identifiers without collision, and regenerate the readable log. The JSON is authoritative; each event is persisted before regenerating the log. Record artifact paths and output content where available; avoid recursively recording the trace artifacts themselves.

The complete DRC report and retrieved records are recorded as structured JSON, not as escaped JSON strings. RAG events include query text, rule names, requested limit, returned records and scores, plus actual indexing inputs and results available from the APIs. Store summaries rather than inventing unavailable internal operations. Model request events retain their present format. Capture failures and incomplete operations explicitly; a missing completion after an interrupted process remains visible as an unfinished operation.

## Readable output

Render events in chronological order with timestamps, step and status. Show operation inputs, key output counts, paths and errors; show RAG query, record IDs and scores, and refer to `conversation.json` for the full data. Preserve readable model responses and decoded tool arguments/results. Prevent repeated full conversation histories from overwhelming the log. Existing logs can be rebuilt from recorded events, but earlier unrecorded operations cannot be recovered.

Example input: a DRC report containing two violations and a RAG query for their rule names. Example output: `READ DRC ... violations=2`, then `RAG RETRIEVE ... rules=... results=...`, followed by `MODEL REQUEST 1` and its response, with full corresponding data available in `conversation.json`.

## Error handling and verification

Use the existing output-path validation and atomic file replacement. Trace write failures remain visible; do not claim that an event is durable until its write succeeds. Preserve workflow failure and incomplete semantics and never report DRC validation without evidence. Do not alter repair decisions or enable disabled workflow steps.

Tests exercise real event persistence through input preparation, read/plan/repair graph execution and RAG retrieval, including errors, resume, chronological ordering, JSON completeness, readable output and compatibility with the case learner and legacy trace events. Use isolated fixtures and stubs only at external model/EDA boundaries. Run targeted trace tests and the repository test suite. Existing run artifacts and unrelated workspace modifications remain untouched.
