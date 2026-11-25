"""Tests for GraphNodes.build_active_task_context and focused DRC/GDS context assembly."""

# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace

import gdstk

from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.task_contracts import TaskContext
from FAGENT.tasks.drc.steps import Steps


# === SECTION 2: Module-Level Helpers ===
class FocusedContextServices:
    """Services double providing real LayoutService, config, and DRC sources."""

    def run(self):
        return self

    def __init__(
        self,
        rundir: Path,
        layout,
        drc_report: dict,
        decks: list | None = None,
        layer_maps: list | None = None,
        margin_dbu: float | None = None,
    ):
        self.rundir = rundir
        self.layout = layout
        self.drc_report = drc_report
        self.config = {'goal': 'Fix DRC', 'max_iterations': 5}
        if margin_dbu is not None:
            self.config['drc_context_margin_dbu'] = margin_dbu
        self.input_sources = {
            'decks': list(decks or []),
            'layer_maps': list(layer_maps or []),
        }
        self.trace = None
        self.conversation_trace = None
        self.max_iterations = 5

    def build_drc_context(self):
        return {'drc': self.drc_report, 'goal': self.config['goal'], 'knowledge': {'constraints': []}}

    def build_system_message(self, goal: str = '', constraints: list | None = None):
        return f'Goal: {goal}'

    def check_output_path(self, path: Path):
        if not Path(path).resolve().is_relative_to(self.rundir.resolve()):
            raise ValueError(f'Path escapes rundir: {path}')


def make_gds_layout(tmp_path: Path, with_opaque_ref: bool = False) -> LayoutService:
    """Build a real GDS layout with multiple candidate layers in the marker region."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    top = lib.new_cell('TOP_CELL')
    # Two CT polygons on layer 15, datatype 0
    top.add(gdstk.rectangle((0.95, 1.95), (1.05, 2.05), layer=15, datatype=0))
    top.add(gdstk.rectangle((1.08, 1.95), (1.18, 2.05), layer=15, datatype=0))
    # Unrelated M1 polygon on layer 16, datatype 0
    top.add(gdstk.rectangle((0.90, 1.90), (1.20, 2.15), layer=16, datatype=0))
    # Far-away polygon for task-2
    top.add(gdstk.rectangle((5.00, 5.00), (5.20, 5.20), layer=15, datatype=0))
    if with_opaque_ref:
        sub = lib.new_cell('SUB_CELL')
        sub.add(gdstk.rectangle((0.0, 0.0), (0.2, 0.2), layer=16, datatype=0))
        top.add(gdstk.Reference(sub, origin=(1.0, 2.0)))

    gds_path = tmp_path / 'sample.gds'
    lib.write_gds(str(gds_path))
    (tmp_path / 'gds.json').write_text(json.dumps({'source': str(gds_path), 'top_cell': 'TOP_CELL'}))
    (tmp_path / 'drc.json').write_text(json.dumps({'rules': []}))
    service = LayoutService(rundir=tmp_path, verbose=False).run()
    service.load_library()
    return service


def write_deck_and_map(tmp_path: Path, ambiguous: bool = False) -> tuple[Path, Path]:
    """Write a DRC rule deck and layer map file."""
    deck = tmp_path / 'rules.drc'
    deck.write_text('CT_EN_7a {\n  ENC CT M1 < 0.05\n}\n', encoding='utf-8')
    lmap = tmp_path / 'layer.map'
    lines = ['CT drawing 15 0\n', 'M1 drawing 16 0\n']
    if ambiguous:
        lines.append('CT drawing 99 0\n')
    lmap.write_text(''.join(lines), encoding='utf-8')
    return deck, lmap


def make_report(points_dbu: list, scale: float | None = 1000.0) -> dict:
    """Create a DRC report with duplicate violation_id occurrences."""
    report = {
        'top_cell': 'TOP_CELL',
        'rules': [
            {
                'name': 'CT_EN_7a',
                'geometry': [
                    {'id': 'v1', 'points_dbu': [[1000, 2000], [1100, 2100]]},
                    {'id': 'v1', 'points_dbu': points_dbu},
                ],
            },
            {
                'name': 'OTHER_RULE',
                'geometry': [
                    {'id': 'v2', 'points_dbu': [[5000, 5000], [5200, 5200]]},
                ],
            },
        ],
    }
    if scale is not None:
        report['dbu_per_user_unit'] = scale
    return report


# === SECTION 3: Tests ===
def test_build_active_task_context_selects_exact_occurrence_and_margin(tmp_path: Path):
    """Exact rule + violation_id + occurrence selection with DBU conversion and margin."""
    layout = make_gds_layout(tmp_path)
    deck, lmap = write_deck_and_map(tmp_path)
    report = make_report([[1000, 2000], [1100, 2100]])
    services = FocusedContextServices(
        tmp_path,
        layout,
        report,
        decks=[deck],
        layer_maps=[lmap],
        margin_dbu=100.0,
    )
    nodes = GraphNodes(services)
    state = {
        'report': report,
        'candidate_digest': 'sha-1',
        'candidate_path': str(tmp_path / 'candidate.gds'),
        'checklist': {
            'tasks': [
                {'task_id': 'task-1', 'rule': 'CT_EN_7a', 'violation_id': 'v1', 'occurrence': 1, 'status': 'pending'},
                {'task_id': 'task-2', 'rule': 'OTHER_RULE', 'violation_id': 'v2', 'occurrence': 0, 'status': 'pending'},
            ]
        },
    }

    ctx = nodes.build_active_task_context(state, 'task-1')
    assert ctx['task']['task_id'] == 'task-1'
    assert ctx['task']['rule'] == 'CT_EN_7a'
    assert ctx['task']['violation_id'] == 'v1'
    assert ctx['task']['occurrence'] == 1
    assert ctx['drc']['status'] == 'available'
    assert ctx['drc']['dbu_per_user_unit'] == 1000.0
    assert ctx['drc']['marker_user'] == [[1.0, 2.0], [1.1, 2.1]]
    assert ctx['drc']['rule_evidence']['found'] is True
    assert ctx['drc']['rule_evidence']['deck_file'] == str(deck.resolve())
    assert ctx['drc']['rule_evidence']['layer_mapping']['CT'] == {'layer': 15, 'datatype': 0}

    assert ctx['layout']['status'] == 'available'
    assert ctx['layout']['candidate_digest'] == 'sha-1'
    assert ctx['layout']['search_region']['margin_user'] == 0.1
    assert ctx['layout']['search_region']['bounds_user'] == [0.9, 1.9, 1.2, 2.2]
    assert len(ctx['layout']['polygons']) == 3


def test_build_active_task_context_supports_one_two_and_multipoint_markers(tmp_path: Path):
    """One-point, two-point, and multi-point markers convert to user bounds cleanly."""
    layout = make_gds_layout(tmp_path)
    deck, lmap = write_deck_and_map(tmp_path)

    for pts, expected_bounds in [
        ([[1000, 2000]], [1.0, 2.0, 1.0, 2.0]),
        ([[1000, 2000], [1100, 2100]], [1.0, 2.0, 1.1, 2.1]),
        ([[1000, 2000], [1100, 2000], [1100, 2100], [1000, 2100]], [1.0, 2.0, 1.1, 2.1]),
    ]:
        report = make_report(pts)
        services = FocusedContextServices(tmp_path, layout, report, decks=[deck], layer_maps=[lmap])
        nodes = GraphNodes(services)
        state = {
            'report': report,
            'checklist': {
                'tasks': [
                    {'task_id': 'task-1', 'rule': 'CT_EN_7a', 'violation_id': 'v1', 'occurrence': 1, 'status': 'pending'},
                ]
            },
        }
        ctx = nodes.build_active_task_context(state, 'task-1')
        assert ctx['layout']['search_region']['bounds_user'] == expected_bounds
        assert ctx['layout']['search_region']['margin_user'] == 0.0


def test_build_active_task_context_handles_missing_and_ambiguous_evidence(tmp_path: Path):
    """Missing marker/scale/rule/deck/mapping and ambiguous mappings produce explicit unavailable status."""
    layout = make_gds_layout(tmp_path)
    deck, lmap = write_deck_and_map(tmp_path, ambiguous=True)

    # 1. Missing marker
    report_no_marker = make_report([[1000, 2000]])
    nodes = GraphNodes(FocusedContextServices(tmp_path, layout, report_no_marker, decks=[deck], layer_maps=[lmap]))
    ctx_missing_marker = nodes.build_active_task_context(
        {'report': report_no_marker, 'checklist': {'tasks': [{'task_id': 't1', 'rule': 'CT_EN_7a', 'violation_id': 'missing', 'occurrence': 0}]}},
        't1',
    )
    assert ctx_missing_marker['layout']['status'] == 'unavailable'
    assert ctx_missing_marker['layout']['search_region']['bounds_user'] is None

    # 2. Missing scale
    report_no_scale = make_report([[1000, 2000]], scale=None)
    ctx_no_scale = nodes.build_active_task_context(
        {'report': report_no_scale, 'checklist': {'tasks': [{'task_id': 't1', 'rule': 'CT_EN_7a', 'violation_id': 'v1', 'occurrence': 0}]}},
        't1',
    )
    assert ctx_no_scale['layout']['status'] == 'unavailable'
    assert ctx_no_scale['layout']['search_region']['bounds_user'] is None

    # 3. Missing deck
    report_ok = make_report([[1000, 2000]])
    nodes_no_deck = GraphNodes(FocusedContextServices(tmp_path, layout, report_ok, decks=[], layer_maps=[lmap]))
    ctx_no_deck = nodes_no_deck.build_active_task_context(
        {'report': report_ok, 'checklist': {'tasks': [{'task_id': 't1', 'rule': 'CT_EN_7a', 'violation_id': 'v1', 'occurrence': 0}]}},
        't1',
    )
    assert ctx_no_deck['drc']['rule_evidence']['found'] is False
    assert ctx_no_deck['drc']['status'] == 'unavailable'

    # 4. Missing rule
    ctx_missing_rule = nodes.build_active_task_context(
        {'report': report_ok, 'checklist': {'tasks': [{'task_id': 't1', 'rule': 'NO_SUCH_RULE', 'violation_id': 'v1', 'occurrence': 0}]}},
        't1',
    )
    assert ctx_missing_rule['drc']['rule_evidence']['found'] is False
    assert ctx_missing_rule['drc']['status'] == 'unavailable'

    # 5. Ambiguous mapping
    ctx_ambig = nodes.build_active_task_context(
        {'report': report_ok, 'checklist': {'tasks': [{'task_id': 't1', 'rule': 'CT_EN_7a', 'violation_id': 'v1', 'occurrence': 0}]}},
        't1',
    )
    assert ctx_ambig['drc']['rule_evidence']['supported'] is False
    assert 'CT' in ctx_ambig['drc']['rule_evidence']['mapping_diagnostics']['ambiguous_layers']


def test_build_active_task_context_marks_references_incomplete(tmp_path: Path):
    """Overlapping references in region are surfaced conservatively with complete=False."""
    layout = make_gds_layout(tmp_path, with_opaque_ref=True)
    deck, lmap = write_deck_and_map(tmp_path)
    report = make_report([[1000, 2000], [1100, 2100]])
    services = FocusedContextServices(tmp_path, layout, report, decks=[deck], layer_maps=[lmap], margin_dbu=100.0)
    nodes = GraphNodes(services)
    ctx = nodes.build_active_task_context(
        {'report': report, 'checklist': {'tasks': [{'task_id': 't1', 'rule': 'CT_EN_7a', 'violation_id': 'v1', 'occurrence': 0}]}},
        't1',
    )
    assert ctx['layout']['complete'] is False
    assert len(ctx['layout']['references_in_region']) == 1


def test_read_drc_retains_report_in_state_without_appending_full_report_message(tmp_path: Path):
    """read_drc stores report in state and does not inject full drc_context into initial messages."""
    layout = make_gds_layout(tmp_path)
    report = make_report([[1000, 2000], [1100, 2100]])
    services = FocusedContextServices(tmp_path, layout, report)
    steps = Steps()
    ctx = TaskContext('drc', {}, {}, services)
    updates = steps.read_drc(ctx)

    assert updates['report'] == report
    assert updates['drc_context']['drc'] == report
    assert all('drc_context' not in (m.get('content') or '') for m in updates['messages'])


def test_model_request_excludes_full_report_and_checklist_and_scopes_tool_history(tmp_path: Path):
    """Model requests omit full report/checklist, keep current tool pairs, and preserve full state history."""
    layout = make_gds_layout(tmp_path)
    deck, lmap = write_deck_and_map(tmp_path)
    report = make_report([[1000, 2000], [1100, 2100]])
    services = FocusedContextServices(tmp_path, layout, report, decks=[deck], layer_maps=[lmap], margin_dbu=100.0)
    services.config['constraints'] = ['Do not touch unrelated cells']
    nodes = GraphNodes(services)

    state_task2 = {
        'report': report,
        'drc_context': {'drc': report, 'goal': 'Fix DRC', 'knowledge': {'constraints': ['Keep M1 width >= 0.05']}},
        'checklist': {
            'tasks': [
                {'task_id': 'task-1', 'rule': 'CT_EN_7a', 'violation_id': 'v1', 'occurrence': 0, 'status': 'unresolved'},
                {'task_id': 'task-2', 'rule': 'OTHER_RULE', 'violation_id': 'v2', 'occurrence': 0, 'status': 'pending'},
            ]
        },
        'task_queue': ['task-2'],
        'current_task_id': 'task-2',
        'task_history': [
            {'sequence': 1, 'task_id': 'task-1', 'status': 'unresolved', 'reason': 'Interrupted tool call'},
        ],
        'task_message_start': 3,
        'messages': [
            {'role': 'system', 'content': 'System instructions'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call-t1', 'type': 'function', 'function': {'name': 'replace_polygon', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call-t1', 'content': '{"ok": false}'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call-t2-ok', 'type': 'function', 'function': {'name': 'check_cover', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'call-t2-ok', 'content': '{"ok": true}'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call-t2-dangling', 'type': 'function', 'function': {'name': 'replace_polygon', 'arguments': '{}'}}]},
        ],
    }

    req_msgs, payload = nodes.build_model_request(state_task2, node_prompt='Repair active marker.')

    assert 'current_checklist' not in payload
    assert 'report' not in payload
    assert payload['goal'] == 'Fix DRC'
    assert payload['constraints'] == ['Keep M1 width >= 0.05']
    assert payload['task_history'] == [{'sequence': 1, 'task_id': 'task-1', 'status': 'unresolved', 'reason': 'Interrupted tool call'}]
    assert payload['active_task']['task']['task_id'] == 'task-2'

    # System message preserved, task-1 tool pair excluded, task-2 paired tool kept, dangling tool call excluded
    assert req_msgs[0] == {'role': 'system', 'content': 'System instructions'}
    tool_call_ids_sent = [
        c['id']
        for m in req_msgs
        for c in (m.get('tool_calls') or [])
    ]
    tool_result_ids_sent = [m.get('tool_call_id') for m in req_msgs if m.get('role') == 'tool']
    assert tool_call_ids_sent == ['call-t2-ok']
    assert tool_result_ids_sent == ['call-t2-ok']

    # Full prior messages remain untouched in state
    assert len(state_task2['messages']) == 6
