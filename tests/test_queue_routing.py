"""Tests for marker queue routing, item-local failures, and checkpoint resume."""

# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace

from langgraph.checkpoint.memory import MemorySaver

from FAGENT.engine.graph_builder import GraphBuilder
from FAGENT.engine.graph_runner import GraphRunner
from FAGENT.engine.task_contracts import ToolResult, ToolSpec
from FAGENT.tasks.drc.checklist import Checklist


# === SECTION 2: Module-Level Helpers ===
def make_checklist_state(task_specs: list[tuple[str, str, int]]) -> tuple[dict, list[str]]:
    """Build a Checklist snapshot dict and matching task_queue."""
    tasks = [
        {
            'task_id': tid,
            'rule': rule,
            'violation_id': vid,
            'occurrence': 0,
            'work': '',
            'status': 'pending',
            'reason': '',
            'polygon_ids': [],
            'evidence': [],
            'stale': False,
        }
        for tid, rule, vid in task_specs
    ]
    cl = Checklist(tasks=tasks)
    return cl.read(), [tid for tid, _, _ in task_specs]


def make_response_schema() -> dict:
    """Return minimal DRC agent response schema."""
    return {
        'type': 'object',
        'properties': {
            'decision': {'type': 'string', 'enum': ['completed', 'stop']},
            'status': {'type': 'string', 'enum': ['verified', 'incomplete']},
            'reason': {'type': 'string'},
            'violations_analysis': {'type': 'array', 'items': {'type': 'object'}},
            'recommendations': {'type': 'string'},
        },
        'required': ['decision', 'status', 'reason', 'violations_analysis', 'recommendations'],
        'additionalProperties': False,
    }


def ok_agent_reply(reason: str = 'Done') -> dict:
    """Return a schema-valid assistant completion message."""
    payload = {
        'decision': 'completed',
        'status': 'incomplete',
        'reason': reason,
        'violations_analysis': [],
        'recommendations': 'Run DRC',
    }
    return {'role': 'assistant', 'content': json.dumps(payload)}


class ScriptedServices:
    """Deterministic GraphServices double for queue routing tests."""

    def run(self):
        return self

    def __init__(self, rundir: Path, steps: tuple, chat_fn, max_iterations: int = 5):
        self.rundir = rundir
        self.config = {'goal': 'Fix DRC', 'max_iterations': max_iterations}
        self.max_iterations = max_iterations
        self.workflow = SimpleNamespace(
            task='drc',
            version=1,
            digest='wf-digest',
            config_digest='cfg-digest',
            inputs=('gds',),
            nodes=steps,
            steps=steps,
            response_schema=make_response_schema(),
            feedback_schema=None,
            knowledge_review=False,
            prompt_text=('Repair prompt',),
            protocol_text='',
            review_text='',
        )
        self.config_digest = 'cfg-digest'
        self.chat_fn = chat_fn
        self.chat_calls = []
        self.trace = None
        self.conversation_trace = None
        self.drc_report = {'top_cell': 'CELL_A', 'dbu_per_user_unit': 1000, 'rules': []}
        self.layout = None

    def check_output_path(self, path: Path):
        if not Path(path).resolve().is_relative_to(self.rundir.resolve()):
            raise ValueError(f'Path escapes rundir: {path}')

    def node_prompt(self, step=None):
        return 'Repair the active violation.'

    def chat(self, messages, specs, response_schema=None):
        self.chat_calls.append(messages)
        return self.chat_fn(len(self.chat_calls), messages, specs, response_schema)

    def interpret_feedback(self, feedback, state):
        return {'decision': feedback.strip().lower()}


def make_read_step(checklist_snapshot: dict, queue: list[str], next_node: str = 'repair') -> SimpleNamespace:
    """Create a read step that seeds checklist, task_queue, and current_task_id."""
    def read_fn(context):
        return {
            'report': {'top_cell': 'CELL_A', 'dbu_per_user_unit': 1000, 'rules': []},
            'checklist': checklist_snapshot,
            'checklist_digest': checklist_snapshot['digest'],
            'task_queue': list(queue),
            'current_task_id': queue[0] if queue else None,
        }

    return SimpleNamespace(
        id='read',
        action='steps:read_drc',
        function=read_fn,
        functions=(read_fn,),
        next=next_node,
        continue_to=None,
        tools={},
        schema=None,
        prompt=None,
    )


def make_repair_step(tools: dict, next_node: str = 'END') -> SimpleNamespace:
    """Create an agent repair step."""
    return SimpleNamespace(
        id='repair',
        action='agent',
        function=None,
        functions=(),
        next=next_node,
        continue_to=None,
        tools=tools,
        schema=make_response_schema(),
        prompt='Repair',
    )


def make_review_step(continue_to: str = 'repair', next_node: str = 'END') -> SimpleNamespace:
    """Create a human review step."""
    return SimpleNamespace(
        id='human_review',
        action='review',
        function=None,
        functions=(),
        next=next_node,
        continue_to=continue_to,
        tools={},
        schema=None,
        prompt=None,
    )


def make_tool_spec(fn, desc: str = 'Test tool') -> ToolSpec:
    """Wrap a module-level callable in a ToolSpec."""
    return ToolSpec(
        function=fn,
        description=desc,
        parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    )


def run_runner(services: ScriptedServices, rundir: Path, review_provider=None) -> dict:
    """Execute GraphRunner with an in-memory checkpointer."""
    runner = GraphRunner(services, rundir, review_provider=review_provider, checkpointer=MemorySaver())
    return runner.run([{'role': 'system', 'content': 'Fix DRC'}], {'gds': 'a.gds'})


# === SECTION 3: Tests ===
def test_ordered_queue_processing_advances_once_per_item(tmp_path: Path) -> None:
    """Queue processes task-1 then task-2 in order and finishes when empty."""
    cl, queue = make_checklist_state([('task-1', 'R1', 1), ('task-2', 'R2', 2)])
    seen_tasks = []

    def chat_fn(call_idx, messages, specs, schema):
        last_user = json.loads(messages[-1]['content'])
        seen_tasks.append(last_user.get('current_task_id'))
        return ok_agent_reply(f'Finished {last_user.get("current_task_id")}')

    steps = (make_read_step(cl, queue), make_repair_step({}))
    services = ScriptedServices(tmp_path, steps, chat_fn)
    result = run_runner(services, tmp_path)

    assert seen_tasks == ['task-1', 'task-2']
    assert result['status'] == 'incomplete'
    assert all(t['status'] == 'pass' for t in result['checklist']['tasks'])


def test_empty_queue_finishes_without_invoking_agent(tmp_path: Path) -> None:
    """Empty task_queue skips repair node and finishes cleanly."""
    cl, queue = make_checklist_state([])

    def chat_fn(call_idx, messages, specs, schema):
        raise AssertionError('Agent must not be called when queue is empty')

    steps = (make_read_step(cl, queue), make_repair_step({}))
    services = ScriptedServices(tmp_path, steps, chat_fn)
    result = run_runner(services, tmp_path)

    assert services.chat_calls == []
    assert result['status'] == 'incomplete'


def test_failed_tool_marks_item_unresolved_and_advances_to_next(tmp_path: Path) -> None:
    """A tool exception marks only the current item unresolved and advances to task-2."""
    cl, queue = make_checklist_state([('task-1', 'R1', 1), ('task-2', 'R2', 2)])
    seen_tasks = []

    def failing_tool(context):
        raise ValueError('Geometry not editable')

    def chat_fn(call_idx, messages, specs, schema):
        if messages[-1]['role'] == 'user':
            payload = json.loads(messages[-1]['content'])
            seen_tasks.append(payload.get('current_task_id'))
            if payload.get('current_task_id') == 'task-1':
                return {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [{'id': 'c1', 'type': 'function', 'function': {'name': 'boom', 'arguments': '{}'}}],
                }
        return ok_agent_reply('Fixed task-2')

    steps = (make_read_step(cl, queue), make_repair_step({'boom': make_tool_spec(failing_tool)}))
    services = ScriptedServices(tmp_path, steps, chat_fn)
    result = run_runner(services, tmp_path)

    assert seen_tasks == ['task-1', 'task-2']
    tasks = result['checklist']['tasks']
    assert tasks[0]['status'] == 'unresolved'
    assert 'Geometry not editable' in tasks[0]['reason']
    assert tasks[1]['status'] == 'pass'
    assert result['status'] == 'incomplete'
    assert 'task-1' in result['reason']


def test_invalid_model_response_marks_item_unresolved_and_advances(tmp_path: Path) -> None:
    """An invalid model response marks task-1 unresolved and advances to task-2."""
    cl, queue = make_checklist_state([('task-1', 'R1', 1), ('task-2', 'R2', 2)])
    seen_tasks = []

    def chat_fn(call_idx, messages, specs, schema):
        payload = json.loads(messages[-1]['content'])
        seen_tasks.append(payload.get('current_task_id'))
        if payload.get('current_task_id') == 'task-1':
            raise ValueError('Invalid llama.cpp chat response: response truncated')
        return ok_agent_reply('Fixed task-2')

    steps = (make_read_step(cl, queue), make_repair_step({}))
    services = ScriptedServices(tmp_path, steps, chat_fn)
    result = run_runner(services, tmp_path)

    assert seen_tasks == ['task-1', 'task-2']
    tasks = result['checklist']['tasks']
    assert tasks[0]['status'] == 'unresolved'
    assert 'response truncated' in tasks[0]['reason']
    assert tasks[1]['status'] == 'pass'
    assert result['status'] == 'incomplete'


def test_per_item_iteration_limit_marks_unresolved_and_resets_for_next(tmp_path: Path) -> None:
    """Hitting max_iterations on task-1 marks it unresolved, resets iteration, and runs task-2."""
    cl, queue = make_checklist_state([('task-1', 'R1', 1), ('task-2', 'R2', 2)])
    noop_calls = []

    def noop_tool(context):
        noop_calls.append(context.state.get('current_task_id'))
        return ToolResult({'status': 'ok'}, {})

    def chat_fn(call_idx, messages, specs, schema):
        last_user = next((m for m in reversed(messages) if m['role'] == 'user'), None)
        tid = json.loads(last_user['content']).get('current_task_id') if last_user else None
        if tid == 'task-1':
            return {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': f'c{call_idx}', 'type': 'function', 'function': {'name': 'noop', 'arguments': '{}'}}],
            }
        return ok_agent_reply('Fixed task-2')

    steps = (make_read_step(cl, queue), make_repair_step({'noop': make_tool_spec(noop_tool)}))
    services = ScriptedServices(tmp_path, steps, chat_fn, max_iterations=2)
    result = run_runner(services, tmp_path)

    assert noop_calls == ['task-1', 'task-1']
    tasks = result['checklist']['tasks']
    assert tasks[0]['status'] == 'unresolved'
    assert 'iteration limit' in tasks[0]['reason'].lower()
    assert tasks[1]['status'] == 'pass'


def test_checkpoint_resume_does_not_replay_completed_mutation(tmp_path: Path) -> None:
    """Resuming from an interrupted review checkpoint does not replay an already executed mutation."""
    cl, queue = make_checklist_state([('task-1', 'R1', 1)])
    mutation_calls = []

    def mutate_tool(context):
        mutation_calls.append(context.state.get('current_task_id'))
        return ToolResult({'status': 'modified'}, {})

    def chat_fn(call_idx, messages, specs, schema):
        if call_idx == 1:
            return {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': 'm1', 'type': 'function', 'function': {'name': 'mutate', 'arguments': '{}'}}],
            }
        return ok_agent_reply('Completed mutation')

    steps = (
        make_read_step(cl, queue),
        make_repair_step({'mutate': make_tool_spec(mutate_tool)}, next_node='human_review'),
        make_review_step(),
    )
    services = ScriptedServices(tmp_path, steps, chat_fn)
    checkpointer = MemorySaver()
    runner = GraphRunner(services, tmp_path, checkpointer=checkpointer)
    graph = GraphBuilder(services, checkpointer).run()
    config = {'configurable': {'thread_id': 't-resume'}}
    interrupted = graph.invoke(
        {'messages': [{'role': 'system', 'content': 'Fix DRC'}], 'inputs': {'gds': 'a.gds'}, 'iteration': 0, 'identity': runner.workflow_identity()},
        config,
    )
    assert interrupted.get('__interrupt__')
    assert len(mutation_calls) == 1

    runner.thread_id = 't-resume'
    runner.graph = graph
    runner.graph_config = config
    resumed = runner.resume(feedback={'feedback': 'stop'})
    assert len(mutation_calls) == 1
    assert resumed['review']['decision'] == 'stop'


def test_global_failure_missing_input_stops_run(tmp_path: Path) -> None:
    """Missing required workflow input triggers global failure without advancing queue."""
    cl, queue = make_checklist_state([('task-1', 'R1', 1)])

    def chat_fn(call_idx, messages, specs, schema):
        raise AssertionError('Agent must not run on missing inputs')

    steps = (make_read_step(cl, queue), make_repair_step({}))
    services = ScriptedServices(tmp_path, steps, chat_fn)
    services.workflow.inputs = ('gds', 'drc')
    runner = GraphRunner(services, tmp_path, checkpointer=MemorySaver())
    result = runner.run([{'role': 'system', 'content': 'Fix DRC'}], {'gds': 'a.gds'})

    assert result['status'] == 'incomplete'
    assert 'Missing inputs' in result['reason']
