"""Tests for DRC marker queue creation, initial GDS context, and checklist persistence."""

# === SECTION 1: Imports ===
from pathlib import Path
from types import SimpleNamespace

from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.task_contracts import TaskContext
from FAGENT.tasks.drc.checklist import Checklist
from FAGENT.tasks.drc.steps import Steps, read_drc


# === SECTION 2: Module-Level Helpers ===
class FakeLayout:
    """Minimal layout service double recording read_gds calls."""

    def run(self):
        return self

    def __init__(self, report: dict, gds_payload: dict):
        self.report = report
        self.gds_payload = gds_payload
        self.read_calls = []

    def read_drc(self):
        return self.report

    def read_gds(self, cell_name: str):
        self.read_calls.append(cell_name)
        return self.gds_payload


class FakeServices:
    """Minimal GraphServices double for read_drc and read_gds unit tests."""

    def run(self):
        return self

    def __init__(self, rundir: Path, report: dict, gds_payload: dict, goal: str = 'Fix all DRC errors.'):
        self.rundir = rundir
        self.config = {'goal': goal}
        self.drc_report = report
        self.layout = FakeLayout(report, gds_payload)
        self.input_sources = {'decks': []}
        self.workflow = SimpleNamespace(task='drc', inputs=('gds', 'drc', 'layer_map'))
        self.trace = None
        self.conversation_trace = None

    def check_output_path(self, path: Path):
        if not Path(path).resolve().is_relative_to(self.rundir.resolve()):
            raise ValueError(f'Path escapes rundir: {path}')

    def build_drc_context(self):
        return {
            'drc': {
                'top_cell': self.drc_report.get('top_cell'),
                'dbu_per_user_unit': self.drc_report.get('dbu_per_user_unit'),
                'total_violations': self.drc_report.get('total_violations', 0),
                'rules': self.drc_report.get('rules', []),
            },
            'knowledge': {'references': [], 'constraints': []},
            'goal': self.config['goal'],
        }

    def build_system_message(self, goal=None, constraints=None):
        return goal or self.config['goal']


def make_report(rules: list[dict], top_cell: str = 'CELL_A') -> dict:
    """Construct a synthetic DRC report dictionary."""
    total = sum(max(int(r.get('total_count', 0)), len(r.get('geometry', []))) for r in rules)
    return {
        'source': 'synthetic.drc.results',
        'top_cell': top_cell,
        'dbu_per_user_unit': 1000,
        'total_violations': total,
        'clean': total == 0,
        'rules': rules,
    }


def make_gds_payload(cell_name: str = 'CELL_A') -> dict:
    """Construct a synthetic user-unit GDS payload."""
    return {
        'cell': cell_name,
        'polygons': [
            {
                'id': f'{cell_name}:poly:0',
                'kind': 'polygon',
                'editable': True,
                'layer': 30,
                'datatype': 0,
                'points': [[0.0, 0.0], [0.2, 0.0], [0.2, 0.1], [0.0, 0.1]],
            }
        ],
        'references': [],
        'paths': [],
    }


# === SECTION 3: Tests ===
def test_read_drc_builds_queue_for_two_markers(tmp_path: Path) -> None:
    """read_drc builds task_queue and current_task_id in report order."""
    report = make_report([
        {'name': 'CT_EN', 'reported_count': 1, 'total_count': 1, 'geometry': [{'type': 'p', 'id': 101, 'points_dbu': [[0, 0], [10, 0], [10, 10], [0, 10]]}]},
        {'name': 'GT_A', 'reported_count': 1, 'total_count': 1, 'geometry': [{'type': 'p', 'id': 201, 'points_dbu': [[20, 20], [30, 20], [30, 30], [20, 30]]}]},
    ])
    services = FakeServices(tmp_path, report, make_gds_payload())
    ctx = TaskContext('drc', {'cell': 'CELL_A'}, {'messages': []}, services)

    updates = read_drc(ctx)
    assert updates['task_queue'] == ['task-1', 'task-2']
    assert updates['current_task_id'] == 'task-1'
    Checklist().restore(Path(updates['checklist_path']), updates['checklist_digest'])


def test_read_drc_preserves_duplicate_marker_ids_with_distinct_occurrences(tmp_path: Path) -> None:
    """Duplicate marker IDs within one rule produce distinct queued tasks."""
    report = make_report([
        {
            'name': 'CT_EN',
            'reported_count': 2,
            'total_count': 2,
            'geometry': [
                {'type': 'p', 'id': 101, 'points_dbu': [[0, 0], [10, 0], [10, 10], [0, 10]]},
                {'type': 'p', 'id': 101, 'points_dbu': [[50, 50], [60, 50], [60, 60], [50, 60]]},
            ],
        }
    ])
    services = FakeServices(tmp_path, report, make_gds_payload())
    ctx = TaskContext('drc', {'cell': 'CELL_A'}, {'messages': []}, services)

    updates = read_drc(ctx)
    assert updates['task_queue'] == ['task-1', 'task-2']
    assert updates['current_task_id'] == 'task-1'
    tasks = updates['checklist']['tasks']
    assert [(t['task_id'], t['violation_id'], t['occurrence']) for t in tasks] == [
        ('task-1', 101, 0),
        ('task-2', 101, 1),
    ]


def test_read_drc_excludes_missing_geometry_and_skipped_from_queue(tmp_path: Path) -> None:
    """Missing geometry becomes unresolved and is excluded from task_queue; checklist keeps all items."""
    report = make_report([
        {'name': 'MISSING_GEOM', 'reported_count': 1, 'total_count': 1, 'geometry': []},
        {'name': 'CT_EN', 'reported_count': 1, 'total_count': 1, 'geometry': [{'type': 'p', 'id': 101, 'points_dbu': [[0, 0], [10, 0], [10, 10], [0, 10]]}]},
    ])
    services = FakeServices(tmp_path, report, make_gds_payload())
    ctx = TaskContext('drc', {'cell': 'CELL_A'}, {'messages': []}, services)

    updates = read_drc(ctx)
    assert updates['task_queue'] == ['task-2']
    assert updates['current_task_id'] == 'task-2'
    tasks = updates['checklist']['tasks']
    assert len(tasks) == 2
    assert tasks[0]['status'] == 'unresolved'
    assert tasks[1]['status'] == 'pending'


def test_read_drc_filters_rules_by_embedding_distance(tmp_path: Path) -> None:
    """Distances below the configured threshold exclude entire rules."""
    report = make_report([
        {'name': 'CT_EN', 'description': ['Contact enclosure'], 'geometry': [{'id': 101}]},
        {'name': 'GT_D', 'description': ['Density > 0.2'], 'geometry': [{'id': 201}]},
        {'name': 'GT_A', 'geometry': [{'id': 301}]},
    ])
    services = FakeServices(tmp_path, report, make_gds_payload())
    services.config.update(skip_prompt='bỏ qua lỗi density', skip_distance_threshold=0.25)
    ctx = TaskContext('drc', {}, {}, services)

    def embed_texts(texts):
        assert texts == ['bỏ qua lỗi density', 'Contact enclosure', 'Density > 0.2', 'GT_A']
        return [[1, 0], [0, 1], [1, 0], [3, 4]]

    services.embed_texts = embed_texts
    updates = read_drc(ctx)
    assert [(task['rule'], task['violation_id']) for task in updates['checklist']['tasks']] == [
        ('CT_EN', 101),
        ('GT_A', 301),
    ]
    assert updates['task_queue'] == ['task-1', 'task-2']


def test_read_drc_empty_report_produces_empty_queue(tmp_path: Path) -> None:
    """Empty DRC report produces an empty queue and None current_task_id."""
    report = make_report([])
    services = FakeServices(tmp_path, report, make_gds_payload())
    ctx = TaskContext('drc', {'cell': 'CELL_A'}, {'messages': []}, services)

    updates = read_drc(ctx)
    assert updates['task_queue'] == []
    assert updates['current_task_id'] is None


def test_ordered_read_drc_then_read_gds_propagates_gds_context(tmp_path: Path) -> None:
    """Executing read_drc then read_gds populates drc_context with GDS geometry and inspected cell."""
    from FAGENT.tasks.drc import steps as drc_steps

    report = make_report([
        {'name': 'CT_EN', 'reported_count': 1, 'total_count': 1, 'geometry': [{'type': 'p', 'id': 101, 'points_dbu': [[0, 0], [10, 0], [10, 10], [0, 10]]}]},
    ])
    gds_payload = make_gds_payload('CELL_A')
    services = FakeServices(tmp_path, report, gds_payload)
    step = SimpleNamespace(
        id='read',
        action=['steps:read_drc', 'steps:read_gds'],
        functions=(drc_steps.read_drc, drc_steps.read_gds),
        next='repair',
    )
    nodes = GraphNodes(services).run()
    merged = nodes.run_task_step({'inputs': {'gds': 'a.gds', 'drc': 'a.drc', 'layer_map': ['a.map']}, 'task_state': {}}, step)

    assert services.layout.read_calls == ['CELL_A']
    assert merged['drc_context']['gds'] == gds_payload
    assert merged['drc_context']['target_cell'] == 'CELL_A'
    assert 'CELL_A' in merged['inspected']
    assert merged['task_queue'] == ['task-1']
    assert merged['current_task_id'] == 'task-1'
    Checklist().restore(Path(merged['checklist_path']), merged['checklist_digest'])
