"""LangGraph execution lifecycle, SQLite checkpoints, interrupts, and outputs."""

# ==============================================================================
# SECTION 1: Standard Library Imports
# ==============================================================================
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

# ==============================================================================
# SECTION 2: Third-Party LangGraph Imports
# ==============================================================================
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

# ==============================================================================
# SECTION 3: Internal FAGENT Module Imports
# ==============================================================================
from .graph_builder import GraphBuilder
from .graph_nodes import GraphNodes
from .graph_services import GraphServices


# ==============================================================================
# SECTION 4: Graph Execution Lifecycle Runner Class
# ==============================================================================
class GraphRunner:
    """Manages graph invocation, persistence checkpoints, user feedback interrupts, and result serialization."""

    # --------------------------------------------------------------------------
    # Sentinel Constants
    # --------------------------------------------------------------------------
    PROMPT_FOR_FEEDBACK = object()

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, messages, inputs=None):
        if trace := getattr(self.services, "trace", None):
            trace.task(self.services.workflow.task)
        self.prepare_graph()
        state = self.graph.invoke(
            {
                'messages': messages,
                'inputs': {} if inputs is None else inputs,
                'iteration': 0,
                'identity': self.workflow_identity()},
            self.graph_config,
        )
        return self.finish(state)

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, services, rundir, review_provider=None, checkpointer=None, thread_id=None):
        self.services = services
        self.rundir = Path(rundir)
        self.review_provider = review_provider or self.prompt_review
        self.checkpointer = checkpointer
        self.connection = None
        self.thread_id = thread_id or uuid4().hex
        self.graph = None
        self.messages = []
        self.result = {}
        self.graph_config = None
        self.review_context = None

    # --------------------------------------------------------------------------
    # Checkpointer Setup and Graph Initialization
    # --------------------------------------------------------------------------
    def prepare_graph(self):
        self.rundir.mkdir(parents=True, exist_ok=True)
        if self.checkpointer is None:
            checkpoint_path = self.rundir / 'graph-checkpoints.sqlite3'
            self.services.check_output_path(checkpoint_path)
            self.connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
            self.checkpointer = SqliteSaver(self.connection)
        if self.graph is None:
            self.graph = GraphBuilder(self.services, self.checkpointer).run()
            self.graph_config = {
                'configurable': {'thread_id': self.thread_id},
                'recursion_limit': max(25, self.services.max_iterations * 4 + 10),
            }

    # --------------------------------------------------------------------------
    # Workflow Resume and State Rehydration
    # --------------------------------------------------------------------------
    def resume(self, feedback=PROMPT_FOR_FEEDBACK):
        self.prepare_graph()
        snapshot = self.graph.get_state(self.graph_config)
        review_ids = {GraphNodes.node_name(step.id) for step in self.services.workflow.steps if step.action == 'review'}
        if not snapshot.next or not set(snapshot.next) <= review_ids or not any(task.interrupts for task in snapshot.tasks):
            raise ValueError('No graph paused at review is available for this thread')
        if snapshot.values.get('identity') != self.workflow_identity():
            raise ValueError('Checkpoint workflow identity does not match the selected workflow')
        self.verify_candidate(snapshot.values)
        if feedback is self.PROMPT_FOR_FEEDBACK:
            pending = next(item for task in snapshot.tasks for item in task.interrupts)
            feedback = self.read_feedback(pending.value)
        return self.finish(self.resume_graph(snapshot.values, feedback))

    def verify_candidate(self, state):
        """Check the saved layout before using a review response."""
        candidate = state.get('candidate_path')
        if candidate:
            self.services.check_output_path(Path(candidate))
            digest = state.get('candidate_digest')
            if not digest or GraphServices.candidate_digest(Path(candidate)) != digest:
                raise ValueError('Checkpoint candidate is missing or changed')
    def resume_graph(self, state, feedback):
        """Recheck identity after waiting for feedback and resume the graph."""
        if state.get('identity') != self.workflow_identity():
            raise ValueError('Checkpoint workflow identity does not match the selected workflow')
        self.verify_candidate(state)
        if candidate := state.get('candidate_path'):
            self.services.restore_candidate(candidate, state.get('validation'))
        return self.graph.invoke(
            Command(resume=feedback if feedback is not None else {'feedback': None}),
            self.graph_config,
        )

    # --------------------------------------------------------------------------
    # Workflow Finalization and Artifact Output
    # --------------------------------------------------------------------------
    def finish(self, state):
        while state.get('__interrupt__'):
            feedback = self.read_feedback(state['__interrupt__'][0].value)
            state = self.resume_graph(state, feedback)
        self.result = dict(state.get('result') or {'status': 'incomplete', 'reason': 'No result'})
        self.result['task'] = state.get('identity')
        self.result['inputs'] = getattr(self.services, 'input_sources', None) or state.get('inputs')
        if task_state := state.get('task_state'):
            self.result['step_outputs'] = {key: value for key, value in task_state.items() if key not in ('messages', 'result')}
        self.result['candidate_digest'] = state.get('candidate_digest')
        self.result['validation'] = state.get('validation')
        self.result['review'] = state.get('review') or {'authorized': False}
        self.messages = state.get('messages', [])
        artifacts = (
            (self.rundir / 'conversation.json', self.messages),
            (self.rundir / 'result.json', self.result),
        )
        for target, _ in artifacts:
            self.services.check_output_path(target)
        for target, value in artifacts:
            target.write_text(json.dumps(value, indent=2) + '\n')
        if not state.get('failed') and state.get('save_requested') and self.result['review'].get('authorized'):
            self.services.store_knowledge(self.result['review'])
        return self.result

    # --------------------------------------------------------------------------
    # Interactive Review and User Prompt Utilities
    # --------------------------------------------------------------------------
    def read_feedback(self, context):
        self.review_context = context
        try:
            return self.review_provider()
        except (EOFError, KeyboardInterrupt):
            return None

    def workflow_identity(self):
        workflow = self.services.workflow
        return {
            'task': workflow.task,
            'version': workflow.version,
            'digest': workflow.digest,
            'config_digest': getattr(self.services, 'config_digest', None),
        }

    def prompt_review(self):
        print(json.dumps(self.review_context, ensure_ascii=False, indent=2))
        feedback = input(f'Review task [thread {self.thread_id}] (EOF to finish): ')
        consent = False
        if feedback.strip() and self.services.workflow.knowledge_review:
            try:
                consent = input('Save this run to the knowledge store if the review ends? [y/N] ').strip().lower() in ('y', 'yes')
            except (EOFError, KeyboardInterrupt):
                pass
        return {'feedback': feedback, 'storage_consent': consent}
