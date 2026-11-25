"""Tests for task-attributed history, DBU-to-user-unit marker context, and live candidate refresh."""

# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace

from langgraph.checkpoint.memory import MemorySaver

from FAGENT.engine.graph_runner import GraphRunner
from FAGENT.engine.task_contracts import ToolResult, ToolSpec
from FAGENT.tasks.drc.checklist import Checklist


# === SECTION 2: Module-Level Helpers ===
class FakeMutableLayout:
    """Mutable layout double tracking polygon edits and live read_gds calls."""

    def run(self):
        return self

    def __init__(self, cell_name: str, polygons: list[dict]):
        self.cell_name = cell_name
        self.polygons = [dict(p) for p in polygons]
        self.read_calls = []
        self.edit_count = 0

    def read_gds(self, cell_name: str):
        self.read_calls.append(cell_name)
        return {
            'cell': cell_name,
            'polygons': [dict(p) for p in self.polygons],
            'references': [],
            'paths': [],
        }

    def edit_first_polygon(self, new_points: list[list[float]]):
        self.polygons[0]['points'] = new_points
        self.edit_count += 1


class ContextTestServices:
    """Services double for testing active_task context, task_history, and candidate refresh."""

    def run(self):
        return self

    def __init__(self, rundir: Path, steps: tuple, report: dict, layout: FakeMutableLayout, chat_fn):
        self.rundir = rundir
        self.config = {'goal': 'Fix DRC', 'max_iterations': 5}
        self.max_iterations = 5
        self.drc_report = report
        self.layout = layout
        self.chat_fn = chat_fn
        self.chat_calls = []
        self.trace = None
        self.conversation_trace = None
        self.config_digest = 'cfg-digest'
        self.workflow = SimpleNamespace(
            task='drc',
            version=1,
            digest='wf-digest',
            config_digest='cfg-digest',
            inputs=('gds',),
            nodes=steps,
            steps=steps,
            response_schema={
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
            },
            feedback_schema=None,
            knowledge_review=False,
            prompt_text=('Repair',),
            protocol_text='',
            review_text='',
        )

    def check_output_path(self, path: Path):
        if not Path(path).resolve().is_relative_to(self.rundir.resolve()):
            raise ValueError(f'Path escapes rundir: {path}')

    def node_prompt(self, step=None):
        return 'Repair active marker.'

    def chat(self, messages, specs, response_schema=None):
        self.chat_calls.append(messages)
        return self.chat_fn(len(self.chat_calls), messages, specs, response_schema)


def make_two_task_report(cell_name: str = 'CELL_A') -> dict:
    """Create a report with two markers and DBU scale 1000."""
    return {
        'source': 'test.drc',
        'top_cell': cell_name,
        'dbu_per_user_unit': 1000,
        'total_violations': 2,
        'clean': False,
        'rules': [
            {
                'name': 'CT_EN',
                'reported_count': 1,
                'total_count': 1,
                'geometry': [{'type': 'p', 'id': 101, 'points_dbu': [[65, 70], [118, 70], [118, 160], [65, 160]]}],
            },
            {
                'name': 'GT_A',
                'reported_count': 1,
                'total_count': 1,
                'geometry': [{'type': 'p', 'id': 201, 'points_dbu': [[-90, 210], [310, 210], [310, 270], [-90, 270]]}],
            },
        ],
    }


def make_initial_read_step(report: dict, layout: FakeMutableLayout) -> SimpleNamespace:
    """Build a read step seeding checklist, task_queue, and drc_context."""
    tasks = [
        {
            'task_id': 'task-1',
            'rule': 'CT_EN',
            'violation_id': 101,
            'occurrence': 0,
            'work': '',
            'status': 'pending',
            'reason': '',
            'polygon_ids': [],
            'evidence': [],
            'stale': False,
        },
        {
            'task_id': 'task-2',
            'rule': 'GT_A',
            'violation_id': 201,
            'occurrence': 0,
            'work': '',
            'status': 'pending',
            'reason': '',
            'polygon_ids': [],
            'evidence': [],
            'stale': False,
        },
    ]
    cl = Checklist(tasks=tasks).read()

    def read_fn(context):
        return {
            'report': report,
            'checklist': cl,
            'checklist_digest': cl['digest'],
            'task_queue': ['task-1', 'task-2'],
            'current_task_id': 'task-1',
            'drc_context': {
                'drc': report,
                'gds': layout.read_gds(report['top_cell']),
                'target_cell': report['top_cell'],
                'candidate_path': None,
                'candidate_digest': None,
                'knowledge': {'references': [], 'constraints': []},
                'goal': 'Fix DRC',
            },
        }

    return SimpleNamespace(
        id='read',
        action='steps:read_drc',
        function=read_fn,
        functions=(read_fn,),
        next='repair',
        continue_to=None,
        tools={},
        schema=None,
        prompt=None,
    )


def make_completion_reply(reason: str) -> dict:
    """Return a schema-valid completion message."""
    return {
        'role': 'assistant',
        'content': json.dumps({
            'decision': 'completed',
            'status': 'incomplete',
            'reason': reason,
            'violations_analysis': [],
            'recommendations': 'Run DRC',
        }),
    }


# === SECTION 3: Tests ===
def test_task_attributed_history_and_candidate_refresh_across_two_tasks(tmp_path: Path) -> None:
    """Task-1 edit refreshes GDS geometry and candidate_digest for task-2 while preserving task_history and DBU scale."""
    report = make_two_task_report('CELL_A')
    layout = FakeMutableLayout('CELL_A', [{'id': 'CELL_A:poly:0', 'points': [[0.0, 0.0], [0.1, 0.0], [0.1, 0.1], [0.0, 0.1]]}])
    observed_payloads = []

    def edit_poly_tool(context, changed: bool):
        if changed:
            new_pts = [[0.0, 0.0], [0.25, 0.0], [0.25, 0.1], [0.0, 0.1]]
            context.services.layout.edit_first_polygon(new_pts)
            return ToolResult(
                {'status': 'modified'},
                {'candidate_path': str(tmp_path / 'candidate.gds'), 'candidate_digest': 'sha-after-task-1'},
            )
        return ToolResult({'status': 'unchanged'}, {})

    edit_spec = ToolSpec(
        function=edit_poly_tool,
        description='Edit polygon',
        parameters={
            'type': 'object',
            'properties': {'changed': {'type': 'boolean'}},
            'required': ['changed'],
            'additionalProperties': False,
        },
    )

    def chat_fn(call_idx, messages, specs, schema):
        last_user = json.loads(messages[-1]['content']) if messages[-1]['role'] == 'user' else None
        if last_user is not None:
            observed_payloads.append(last_user)
        if call_idx == 1:
            return {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': 'call-t1', 'type': 'function', 'function': {'name': 'edit_poly', 'arguments': '{"changed": true}'}}],
            }
        if call_idx == 2:
            return make_completion_reply('Fixed task-1')
        return make_completion_reply('Checked task-2')

    repair_step = SimpleNamespace(
        id='repair',
        action='agent',
        function=None,
        functions=(),
        next='END',
        continue_to=None,
        tools={'edit_poly': edit_spec},
        schema=None,
        prompt='Repair',
    )
    steps = (make_initial_read_step(report, layout), repair_step)
    services = ContextTestServices(tmp_path, steps, report, layout, chat_fn)
    runner = GraphRunner(services, tmp_path, checkpointer=MemorySaver())
    result = runner.run([{'role': 'system', 'content': 'Fix DRC'}], {'gds': 'a.gds'})

    assert len(observed_payloads) == 3
    # First request (task-1): DBU-to-user-unit conversion and initial geometry
    req1 = observed_payloads[0]
    assert req1['active_task']['task_id'] == 'task-1'
    assert req1['active_task']['dbu_per_user_unit'] == 1000
    assert req1['active_task']['points_user'] == [[0.065, 0.07], [0.118, 0.07], [0.118, 0.16], [0.065, 0.16]]
    assert req1['task_history'] == []

    # Second request (still task-1, right after edit): refreshed geometry and candidate digest
    req2 = observed_payloads[1]
    assert req2['active_task']['candidate_digest'] == 'sha-after-task-1'
    assert req2['active_task']['gds']['polygons'][0]['points'] == [[0.0, 0.0], [0.25, 0.0], [0.25, 0.1], [0.0, 0.1]]

    # Third request (task-2): sees task-1 in task_history and task-1's updated geometry
    req3 = observed_payloads[2]
    assert req3['active_task']['task_id'] == 'task-2'
    assert req3['active_task']['points_user'] == [[-0.09, 0.21], [0.31, 0.21], [0.31, 0.27], [-0.09, 0.27]]
    assert req3['active_task']['gds']['polygons'][0]['points'] == [[0.0, 0.0], [0.25, 0.0], [0.25, 0.1], [0.0, 0.1]]
    assert req3['task_history'] == [
        {'sequence': 1, 'task_id': 'task-1', 'status': 'pass', 'reason': 'Fixed task-1'}
    ]

    # Tool-call IDs remain intact in conversation
    tool_msgs = [m for m in result['checklist']['tasks']]
    assert len(tool_msgs) == 2
    assert any(m.get('tool_call_id') == 'call-t1' for m in runner.messages if m.get('role') == 'tool')


def test_unchanged_or_failed_edit_does_not_falsely_refresh_candidate(tmp_path: Path) -> None:
    """An unchanged edit must not trigger a live layout refresh or alter candidate_digest."""
    report = make_two_task_report('CELL_A')
    layout = FakeMutableLayout('CELL_A', [{'id': 'CELL_A:poly:0', 'points': [[0.0, 0.0], [0.1, 0.0], [0.1, 0.1], [0.0, 0.1]]}])
    observed_payloads = []

    def edit_poly_tool(context):
        return ToolResult({'status': 'unchanged'}, {})

    edit_spec = ToolSpec(
        function=edit_poly_tool,
        description='Edit polygon',
        parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    )

    def chat_fn(call_idx, messages, specs, schema):
        last_user = json.loads(messages[-1]['content']) if messages[-1]['role'] == 'user' else None
        if last_user is not None:
            observed_payloads.append(last_user)
        if call_idx == 1:
            return {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{'id': 'call-noop', 'type': 'function', 'function': {'name': 'edit_poly', 'arguments': '{}'}}],
            }
        return make_completion_reply('Done')

    repair_step = SimpleNamespace(
        id='repair',
        action='agent',
        function=None,
        functions=(),
        next='END',
        continue_to=None,
        tools={'edit_poly': edit_spec},
        schema=None,
        prompt='Repair',
    )
    steps = (make_initial_read_step(report, layout), repair_step)
    services = ContextTestServices(tmp_path, steps, report, layout, chat_fn)
    runner = GraphRunner(services, tmp_path, checkpointer=MemorySaver())
    runner.run([{'role': 'system', 'content': 'Fix DRC'}], {'gds': 'a.gds'})

    # Only the initial read_gds happened; unchanged edit did not re-read layout
    assert layout.read_calls == ['CELL_A']
    assert observed_payloads[1]['active_task']['candidate_digest'] is None
