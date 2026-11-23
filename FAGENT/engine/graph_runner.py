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
from ..tasks.drc.checklist import Checklist


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
        if self.checkpointer is None and not (self.rundir / 'graph-checkpoints.sqlite3').is_file():
            raise ValueError(f'No checkpoint database exists in {self.rundir}')
        self.prepare_graph()
        snapshot = self.graph.get_state(self.graph_config)
        workflow_nodes = getattr(self.services.workflow, 'nodes', None) or self.services.workflow.steps
        review_ids = {GraphNodes.node_name(step.id) for step in workflow_nodes if step.action == 'review'}
        if not snapshot.next or not set(snapshot.next) <= review_ids or not any(task.interrupts for task in snapshot.tasks):
            raise ValueError('No graph paused at review is available for this thread')
        if snapshot.values.get('identity') != self.workflow_identity():
            raise ValueError('Checkpoint workflow identity does not match the selected workflow')
        self.verify_candidate(snapshot.values)
        if feedback is self.PROMPT_FOR_FEEDBACK:
            pending = next(item for task in snapshot.tasks for item in task.interrupts)
            feedback = self.read_feedback(pending.value)
        trace_inst = getattr(self.services, 'conversation_trace', None)
        resume_op = None
        if trace_inst:
            resume_op = trace_inst.start_operation('resume', {'thread_id': self.thread_id})
        try:
            res = self.finish(self.resume_graph(snapshot.values, feedback))
            if trace_inst and resume_op:
                trace_inst.finish_operation(resume_op, 'resume', status='completed')
            return res
        except Exception as exc:
            if trace_inst and resume_op:
                trace_inst.finish_operation(resume_op, 'resume', status='failed', error=exc)
            raise

    def verify_candidate(self, state):
        """Check the saved layout and checklist before using a review response."""
        candidate = state.get('candidate_path')
        if candidate:
            self.services.check_output_path(Path(candidate))
            digest = state.get('candidate_digest')
            if not digest or GraphServices.candidate_digest(Path(candidate)) != digest:
                raise ValueError('Checkpoint candidate is missing or changed')
        checklist_path = state.get('checklist_path')
        if checklist_path:
            self.services.check_output_path(Path(checklist_path))
            checklist_digest = state.get('checklist_digest')
            Checklist().restore(Path(checklist_path), checklist_digest)
    def resume_graph(self, state, feedback):
        """Recheck identity after waiting for feedback and resume the graph."""
        if state.get('identity') != self.workflow_identity():
            raise ValueError('Checkpoint workflow identity does not match the selected workflow')
        self.verify_candidate(state)
        if candidate := state.get('candidate_path'):
            self.services.restore_candidate(candidate, state.get('validation'), state.get('candidate_digest'))
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
        fallback = {'status': 'incomplete', 'reason': 'No result'}
        if state.get('response') is not None:
            fallback = {'status': 'incomplete', 'reason': 'No verified validation', 'response': state['response']}
        self.result = dict(state.get('result') or fallback)
        inputs = state.get('inputs') or {}
        self.result['run_id'] = inputs.get('run_id') or (self.rundir.parent.name if self.rundir.name == 'rundir' else self.thread_id)
        self.result['thread_id'] = self.thread_id
        self.result['rundir'] = str(self.rundir)
        self.result['task'] = state.get('identity')
        self.result['inputs'] = getattr(self.services, 'input_sources', None) or inputs
        if task_state := state.get('task_state'):
            self.result['step_outputs'] = {key: value for key, value in task_state.items() if key not in ('messages', 'result')}
        self.result['candidate_digest'] = state.get('candidate_digest')
        self.result['checklist_digest'] = state.get('checklist_digest')
        if state.get('checklist') is not None:
            self.result['checklist'] = state.get('checklist')
        self.result['validation'] = state.get('validation')
        self.result['review'] = state.get('review') or {'authorized': False}
        self.messages = state.get('messages', [])

        trace_inst = getattr(self.services, 'conversation_trace', None)
        final_op = None
        if trace_inst:
            final_op = trace_inst.start_operation(
                'finalization',
                {'status': self.result.get('status'), 'rundir': str(self.rundir)},
            )
        try:
            artifacts = (
                (self.rundir / 'result.json', self.result),
            )
            for target, _ in artifacts:
                self.services.check_output_path(target)
            for target, value in artifacts:
                art_op = None
                if trace_inst:
                    art_op = trace_inst.start_operation('artifact_write', {'target': str(target)})
                try:
                    target.write_text(json.dumps(value, indent=2) + '\n')
                    if trace_inst and art_op:
                        trace_inst.finish_operation(art_op, 'artifact_write', status='completed')
                except Exception as exc:
                    if trace_inst and art_op:
                        trace_inst.finish_operation(art_op, 'artifact_write', status='failed', error=exc)
                    raise
            if not state.get('failed') and state.get('save_requested') and self.result['review'].get('authorized'):
                self.services.store_knowledge(self.result['review'])
            if trace_inst and final_op:
                trace_inst.finish_operation(
                    final_op,
                    'finalization',
                    status='completed',
                    outputs={'status': self.result.get('status'), 'result_path': str(self.rundir / 'result.json')},
                )
            return self.result
        except Exception as exc:
            if trace_inst and final_op:
                trace_inst.finish_operation(final_op, 'finalization', status='failed', error=exc)
            raise

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
