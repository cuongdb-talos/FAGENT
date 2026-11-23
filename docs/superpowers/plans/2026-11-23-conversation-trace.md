# Complete Conversation Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task inline. Do NOT dispatch subagents. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Record every model request, model response, and tool result in the run's `conversation.json` so the flow can be audited.

**Architecture:** Keep model conversation state unchanged and maintain a separate, chronological, versioned trace in the run directory. Each request stores its exact messages, tool specifications and response schema at the `GraphServices.chat` boundary; responses, failures and tool results are correlated to their request or tool call. Persist atomically before and after each boundary, and never overwrite the trace with graph state at finish.

**Tech Stack:** Python, LangGraph, pytest, JSON, llama.cpp adapter.

**Spec:** User request in this conversation: capture the complete model–flow conversation for tracing; example run `output/ROM_MCELL0/runs/16:20:31-23Nov2026-drc/rundir`. This historical run lacks the ephemeral requests and cannot be reconstructed exactly.

## Global Constraints

- Derive the output path from the configured run directory; do not hard-code a cell or run ID.
- Preserve actual request order, DRC rule names, marker IDs, units, content, tool arguments and outputs without modifying model input or tool behavior.
- Keep snapshots atomic and available during execution and on failure; retain trace across checkpoint resume.
- Historical logs must not be presented as exact reconstructed requests.
- Document input location: `grep -n -A 12 'Node `read`' FAGENT/tasks/README.md`.

## Review Focus

- Model call fails after request: retain request and record failure.
- Tool raises or returns non-JSON data: record the actual error returned to the model.
- Multiple requests with changing checklist: preserve each exact request, in order.
- Resume or finish: do not overwrite or duplicate existing trace events.
- Legacy conversation list consumed by knowledge learning: preserve compatible behavior and fingerprints.

## File Structure

- Create `FAGENT/engine/conversation_trace.py`: atomic run-scoped trace store, append and correlation.
- Modify `FAGENT/engine/graph_services.py`: capture all model requests and outcomes at the shared chat boundary.
- Modify `FAGENT/engine/graph_nodes.py`: capture tool results and preserve failure state without replacing the trace.
- Modify `FAGENT/engine/graph_runner.py`: stop overwriting trace on finish.
- Modify `FAGENT/rag/case_learner.py`: consume both legacy message arrays and new trace format.
- Modify `tests/test_conversation_snapshots.py`, `tests/test_drc_context_flow.py`, and knowledge learner tests; document format in `FAGENT/tasks/README.md`.

Representative trace (abbreviated; actual `messages` contains the full original request):

```json
{"version": 2, "events": [
  {"type": "model_request", "request_id": "1", "messages": [{"role": "user", "content": "{\"drc_context\":{\"drc\":{\"rules\":[{\"name\":\"CT_R_11\"}]}}}"}, {"role": "user", "content": "{\"current_checklist\":{},\"goal\":\"repair\"}"}], "tools": [], "response_schema": {}},
  {"type": "model_response", "request_id": "1", "message": {"role": "assistant", "content": null, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read_gds", "arguments": "{}"}}]}},
  {"type": "tool_result", "message": {"role": "tool", "tool_call_id": "call-1", "content": "{\"cell\":\"C\"}"}}
]}
```

### Task 1: Trace model requests and outcomes

**Files:** Create `FAGENT/engine/conversation_trace.py`; modify `FAGENT/engine/graph_services.py`; test `tests/test_conversation_snapshots.py`.

**Interfaces:** `ConversationTrace(rundir, check_output_path).append(event: dict) -> None` atomically updates `conversation.json`; `GraphServices.chat(messages, specs, response_schema=None)` retains its existing signature and return value. Trace `request_id` is unique within the run and persists across resume.

- [ ] Write tests calling `chat` twice with different checklist messages; assert each `model_request` exactly equals the arguments received by the client, including tool specs and effective schema, followed by the corresponding `model_response` with the same ID. Assert the client receives the original inputs unchanged.
- [ ] Add a test where the client raises: the file contains the request and a `model_error` event with its request ID and exception type/message; the exception still propagates. Add a resume test appending to an existing trace without loss or duplicate IDs.
- [ ] Run `pytest tests/test_conversation_snapshots.py -q`; confirm the new tests fail because trace events do not exist.
- [ ] Implement the trace store and instrument `GraphServices.chat` before and after client invocation. Record the effective schema actually passed to the client, not an inferred default. Store request, response and failure events in call order.
- [ ] Run `pytest tests/test_conversation_snapshots.py -q`; expect all task tests to pass.

### Task 2: Capture tools and protect the artifact

**Files:** Modify `FAGENT/engine/graph_nodes.py`, `FAGENT/engine/graph_runner.py`; test `tests/test_conversation_snapshots.py` and `tests/test_drc_context_flow.py`.

**Interfaces:** Use `ConversationTrace.append(event)` from Task 1; tool-result events contain the exact `role: tool` message including `tool_call_id`. Graph `messages` remains the model's conversation state and does not acquire trace-only events.

- [ ] Add an integration test for plan → `read_gds` → repair: compare each recorded request against the mock client's actual arguments, verify DRC context and each current checklist, and verify assistant call ID matches the recorded tool result. Verify a tool exception records exactly the error message returned to the model.
- [ ] Add tests that `finish` and checkpoint resume preserve earlier events, that the request remains on disk if the process fails before response, and that invalid model output does not discard the trace.
- [ ] Run `pytest tests/test_conversation_snapshots.py tests/test_drc_context_flow.py -q`; confirm new assertions fail.
- [ ] Replace conversation snapshot writes with trace events, preserve existing graph failure-state handling, and remove the runner's final overwrite of `conversation.json`. Make resume append safely to the existing run trace.
- [ ] Run `pytest tests/test_conversation_snapshots.py tests/test_drc_context_flow.py -q`; expect PASS.

### Task 3: Compatibility and verification

**Files:** Modify `FAGENT/rag/case_learner.py`, `FAGENT/tasks/README.md`; add tests to the existing knowledge learner test module.

**Interfaces:** The learner accepts both legacy list snapshots and version 2 traces; for a trace derive assistant/tool messages from ordered response and tool-result events, excluding repeated request histories.

- [ ] Add tests for equivalent legacy and version 2 conversations: identical extracted assistant/tool sequence and fingerprint for the same run, plus empty trace and failed-call cases. Run the relevant learner tests and confirm the version 2 case fails.
- [ ] Update the learner and document the event schema, request IDs, sensitive full-context contents, live persistence, historical-run limitation, and path derived from `rundir`.
- [ ] Run `pytest tests/test_conversation_snapshots.py tests/test_drc_context_flow.py` plus the identified learner tests; expect PASS. Run the project's applicable full test suite and report failures separately.
- [ ] Run a representative local DRC flow if the configured model and input tools are available. Inspect the actual `conversation.json`, correlate every request/response/tool ID, compare a request with captured client input, and report the process exit status. This validates logging, not DRC correctness; if unavailable, report the missing dependency and rely on mock tests.
