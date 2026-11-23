# === SECTION 1: Imports ===
import copy
import json
from pathlib import Path
import sys
import tempfile

# === SECTION 2: Conversation Trace Class ===


class ConversationTrace:
    """Run-scoped conversation trace manager for auditing model requests, responses, and tool outcomes."""

    def __init__(self, rundir, check_output_path=None):
        """Initialize the conversation trace instance."""
        self.rundir = Path(rundir).resolve() if rundir else None
        self.check_output_path = check_output_path
        self.target_path = (self.rundir / 'conversation.json') if self.rundir else None
        self.version = 2
        self.events = []
        self.next_request_id = 1
        self.loaded = False
        self.run()

    def run(self):
        """Execute initialization and load existing trace state from disk."""
        self.load_state()
        return self

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
                    max_id = 0
                    for event in self.events:
                        req_id = event.get('request_id')
                        if req_id is not None:
                            try:
                                max_id = max(max_id, int(req_id))
                            except (ValueError, TypeError):
                                pass
                    self.next_request_id = max_id + 1
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

    def append(self, event):
        """Atomically append an event to the trace and persist to disk."""
        if not self.loaded:
            self.run()
        if not isinstance(event, dict):
            raise TypeError('Trace event must be a dict')
        self.events.append(copy.deepcopy(event))
        self.persist()

    def persist(self):
        """Atomically write the conversation trace JSON file."""
        if not self.target_path:
            return
        if self.check_output_path:
            self.check_output_path(self.target_path)

        payload = {
            'version': self.version,
            'events': self.events,
        }
        text = json.dumps(payload, indent=2) + '\n'

        temp_dir = self.target_path.parent
        temp_file = None
        try:
            with tempfile.NamedTemporaryFile('w', dir=temp_dir, delete=False, encoding='utf-8') as handle:
                temp_file = Path(handle.name)
                handle.write(text)
            temp_file.replace(self.target_path)
        except Exception as exc:
            if temp_file and temp_file.exists():
                temp_file.unlink(missing_ok=True)
            print(f'[ERROR] Failed to atomically persist conversation trace to {self.target_path}: {exc}', file=sys.stderr)
            raise
