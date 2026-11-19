"""Emit live workflow metadata without logging execution payloads."""

# === SECTION 1: Imports ===
from typing import TextIO


# === SECTION 2: Trace output ===
class Trace:
    """Format live task, node, and tool events."""

    def run(self, line: str):
        """Write one trace line to the selected stream."""
        if self.enabled:
            print(line, file=self.stream, flush=True)

    def __init__(self, enabled: bool, stream: TextIO):
        """Configure an opt-in trace stream."""
        self.enabled = enabled
        self.stream = stream

    def task(self, name):
        """Identify the workflow."""
        self.run(f'[Task {name}] Starting workflow.')

    def node_start(self, node, step, action, *, tools=False):
        """Describe the current activity before executing the node."""
        if tools:
            activity = 'Processing model tool calls'
        elif action == 'agent':
            activity = 'Requesting a response from the model'
        elif action == 'review':
            activity = 'Processing human review'
        else:
            activity = f'Running workflow action {action}'
        self.run(f'[Step {step}] {activity} (node: {node})')

    def tool(self, name, step):
        """Name the tool and its step before invocation."""
        self.run(f'[Step {step}] Calling tool {name}.')

    def node_end(self, node, step, status, next_node=None):
        """Report status and the actual next node when available."""
        if status == 'waiting':
            self.run(f'[Step {step}] Waiting for human feedback (node: {node}).')
            return
        message = f'[Step {step}] {status.capitalize()} (node: {node}).'
        if next_node == 'END':
            message += ' Workflow finished.'
        elif next_node is not None:
            message += f' Next node: {next_node}.'
        self.run(message)
