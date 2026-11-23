# === SECTION 1: Imports ===
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import tempfile

# === SECTION 2: Conversation Trace Class ===


class ConversationTrace:
    """Run-scoped conversation trace manager for auditing model requests, responses, tool outcomes, and workflow operations."""

    def run(self):
        """Load existing trace state and regenerate its readable view."""
        self.load_state()
        if self.events:
            self.persist_log()
        return self

    def __init__(self, rundir, check_output_path=None):
        """Initialize the conversation trace instance."""
        self.rundir = Path(rundir).resolve() if rundir else None
        self.check_output_path = check_output_path
        self.target_path = (self.rundir / 'conversation.json') if self.rundir else None
        self.log_path = (self.rundir / 'trace.log') if self.rundir else None
        self.version = 2
        self.events = []
        self.next_request_id = 1
        self.next_operation_id = 1
        self.loaded = False
        self.run()

    # --- Step Methods ---

    def load_state(self):
        """Read and validate existing conversation trace file if present."""
        if not self.target_path or not self.target_path.is_file():
            self.loaded = True
            return

        try:
            content = self.target_path.read_text(encoding='utf-8')
            data = json.loads(content)
            if isinstance(data, dict) and data.get('version') == self.version:
                raw_events = data.get('events', [])
                if isinstance(raw_events, list):
                    self.events = list(raw_events)
                    max_req_id = 0
                    max_op_id = 0
                    for event in self.events:
                        req_id = event.get('request_id')
                        if req_id is not None:
                            try:
                                max_req_id = max(max_req_id, int(req_id))
                            except (ValueError, TypeError):
                                pass
                        op_id = event.get('operation_id')
                        if op_id is not None:
                            matches = re.findall(r'\d+', str(op_id))
                            if matches:
                                max_op_id = max(max_op_id, int(matches[-1]))
                    self.next_request_id = max_req_id + 1
                    self.next_operation_id = max_op_id + 1
        except Exception as exc:
            print(f'[ERROR] Failed to load existing conversation trace from {self.target_path}: {exc}', file=sys.stderr)
            raise
        self.loaded = True

    def allocate_request_id(self):
        """Allocate a monotonically increasing unique request ID within this run."""
        if not self.loaded:
            self.run()
        req_id = str(self.next_request_id)
        self.next_request_id += 1
        return req_id

    def allocate_operation_id(self):
        """Allocate a monotonically increasing unique operation ID within this run."""
        if not self.loaded:
            self.run()
        op_id = f'op-{self.next_operation_id}'
        self.next_operation_id += 1
        return op_id

    def start_operation(self, name, inputs=None):
        """Start a tracked operation and persist its start event."""
        if not self.loaded:
            self.run()
        op_id = self.allocate_operation_id()
        ts = datetime.now(timezone.utc).isoformat()
        event = {
            'type': 'operation_start',
            'operation_id': op_id,
            'name': name,
            'timestamp': ts,
            'inputs': inputs,
        }
        self.append(event)
        return op_id

    def finish_operation(self, operation_id, name, status, outputs=None, error=None):
        """Complete a tracked operation and persist its outcome event."""
        if not self.loaded:
            self.run()
        ts = datetime.now(timezone.utc).isoformat()
        err_val = str(error) if error is not None and isinstance(error, BaseException) else error
        event = {
            'type': 'operation_finish',
            'operation_id': operation_id,
            'name': name,
            'status': status,
            'timestamp': ts,
            'outputs': outputs,
            'error': err_val,
        }
        self.append(event)

    def append(self, event):
        """Atomically append an event to the trace and persist to disk."""
        if not self.loaded:
            self.run()
        if not isinstance(event, dict):
            raise TypeError('Trace event must be a dict')
        self.events.append(copy.deepcopy(event))
        self.persist()
        self.persist_log()

    def format_value(self, value):
        """Pretty-print JSON payloads while preserving ordinary text."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return value
        return json.dumps(value, indent=2, ensure_ascii=False)

    def format_operation_summary(self, event):
        """Format an operation event for human-readable trace log display."""
        kind = event.get('type', '')
        op_id = event.get('operation_id', '')
        name = event.get('name', '')
        ts = event.get('timestamp', '')
        display_name = name.replace('_', ' ').upper()

        if kind == 'operation_start':
            inputs = event.get('inputs')
            lines = [f'OPERATION START {display_name} (id: {op_id}, name: {name}) [{ts}]']
            if isinstance(inputs, dict):
                parts = []
                for k, v in inputs.items():
                    if isinstance(v, (str, int, float, bool)):
                        parts.append(f'{k}={v}')
                    elif isinstance(v, list):
                        parts.append(f'{k}=[{len(v)} items]')
                    elif isinstance(v, dict):
                        parts.append(f'{k}={{{len(v)} keys}}')
                if parts:
                    lines.append('  Inputs: ' + ', '.join(parts))
                else:
                    lines.append('  Inputs: (empty)')
            elif inputs is not None:
                lines.append(f'  Inputs: {self.format_value(inputs)}')
            return '\n'.join(lines)

        if kind == 'operation_finish':
            status = event.get('status', 'unknown')
            error = event.get('error')
            outputs = event.get('outputs')
            lines = [f'OPERATION FINISH {display_name} (id: {op_id}, name: {name}) [{ts}] - Status: {status.upper()}']
            if error:
                lines.append(f'  Error: {error}')
            if isinstance(outputs, dict):
                parts = []
                for k, v in outputs.items():
                    if isinstance(v, (str, int, float, bool)):
                        parts.append(f'{k}={v}')
                    elif isinstance(v, list):
                        parts.append(f'{k}=[{len(v)} items]')
                    elif isinstance(v, dict):
                        parts.append(f'{k}={{{len(v)} keys}}')
                if parts:
                    lines.append('  Outputs: ' + ', '.join(parts))
            elif outputs is not None:
                lines.append(f'  Outputs: {self.format_value(outputs)}')
            return '\n'.join(lines)

        return f'EVENT {kind}\n{self.format_value(event)}'

    def persist_log(self):
        """Write a readable view of the saved events in chronological order."""
        if not self.log_path:
            return
        sections = ['Full requests and schemas: conversation.json']
        calls = {}
        for event in self.events:
            kind = event.get('type', '')
            request_id = event.get('request_id', '')
            if kind == 'model_request':
                sections.append(
                    f"MODEL REQUEST {request_id}\nMessages: {len(event.get('messages', []))}; "
                    f"tools: {len(event.get('tools', []))}. See conversation.json for full request."
                )
            elif kind == 'model_response':
                message = event.get('message') or {}
                lines = [f'MODEL RESPONSE {request_id}']
                if message.get('content') is not None:
                    lines.append(self.format_value(message['content']))
                for call in message.get('tool_calls') or []:
                    function = call.get('function') or {}
                    call_id = call.get('id', '')
                    calls[call_id] = function.get('name', '')
                    lines.append(
                        f"TOOL CALL {function.get('name', '')} (id: {call_id})\nArguments:\n{self.format_value(function.get('arguments', {}))}"
                    )
                sections.append('\n'.join(lines))
            elif kind == 'tool_result':
                message = event.get('message') or {}
                call_id = message.get('tool_call_id', '')
                sections.append(f"TOOL RESULT {calls.get(call_id, '')} (id: {call_id})\n{self.format_value(message.get('content', ''))}")
            elif kind == 'model_error':
                sections.append(f"MODEL ERROR {request_id}\n{event.get('error_type', '')}: {event.get('error_message', '')}")
            elif kind in ('operation_start', 'operation_finish'):
                sections.append(self.format_operation_summary(event))
            else:
                sections.append(f"EVENT {kind}\n{self.format_value(event)}")
        self.write_atomic(self.log_path, '\n\n'.join(sections) + '\n')

    def persist(self):
        """Atomically write the conversation trace JSON file."""
        if not self.target_path:
            return
        payload = {
            'version': self.version,
            'events': self.events,
        }
        text = json.dumps(payload, indent=2) + '\n'

        self.write_atomic(self.target_path, text)

    def write_atomic(self, target, text):
        """Write an artifact atomically after checking its destination."""
        if self.check_output_path:
            self.check_output_path(target)
        temp_dir = target.parent
        temp_file = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=temp_dir, delete=False, encoding='utf-8') as handle:
                temp_file = Path(handle.name)
                handle.write(text)
            temp_file.replace(target)
        except Exception as exc:
            if temp_file and temp_file.exists():
                temp_file.unlink(missing_ok=True)
            print(f'[ERROR] Failed to atomically persist conversation trace to {target}: {exc}', file=sys.stderr)
            raise
