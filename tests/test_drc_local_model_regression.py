import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import FAGENT.engine.graph_nodes as graph_module
from FAGENT.capabilities.layout.cover_check import CoverCheck, WidthCheck
from FAGENT.capabilities.layout.repair_proposals import AreaProposalBuilder, WidthProposalBuilder
from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.task_contracts import TaskContext
from FAGENT.engine.workflow_loader import WorkflowLoader
from FAGENT.tasks.drc.steps import human_review_route
from FAGENT.tasks.drc.tools import DrcTools, fix_area, fix_cover, fix_width


INNER = [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0]]
SMALL = [[0.5, 0.5], [1.5, 0.5], [1.5, 1.5], [0.5, 1.5]]
OUTER = [[0.7, 0.7], [1.3, 0.7], [1.3, 1.3], [0.7, 1.3]]


class PointList:
    def __init__(self, points):
        self.values = points

    def tolist(self):
        return self.values


class PolygonIds:
    def __init__(self, polygons):
        self.polygons = polygons
        self.saved_digest = None

    def resolve(self, cell_name, polygon_id):
        return SimpleNamespace(points=PointList(self.polygons[polygon_id]))

    def save(self, path, digest):
        self.saved_digest = digest
        Path(path).write_text(digest)


class FakeLayout:
    def __init__(self, candidate, polygons):
        self.candidate = candidate
        self.gds_path = candidate
        self.library = SimpleNamespace(precision=0.001, unit=1.0)
        self.gds_data = {'top_cell': 'TOP'}
        self.polygon_ids = PolygonIds(polygons)
        self.polygons = polygons

    def load_library(self):
        return self.library

    def apply_polygon_changes(self, cell_name, changes):
        snapshot = {change['polygon_id']: self.polygons[change['polygon_id']] for change in changes}
        for change in changes:
            self.polygons[change['polygon_id']] = change.get('points', change.get('after_points_user'))
        return snapshot

    def rollback_transaction(self, snapshot):
        self.polygons.update(snapshot)


class FakeServices:
    def __init__(self, rundir, layout):
        self.rundir = Path(rundir)
        self.layout = layout

    def checkpoint_candidate(self):
        self.layout.candidate.write_text(json.dumps(self.layout.polygons, sort_keys=True))
        return self.layout.candidate


class FakeInterrupt:
    def __init__(self, response):
        self.response = response

    def __call__(self, payload):
        return self.response


class ReviewServices:
    def __init__(self, decision='unclear'):
        self.workflow = SimpleNamespace(knowledge_review=False)
        self.decision = decision

    def interpret_feedback(self, feedback, state):
        return {'decision': self.decision}


class LocalModelStub:
    def __init__(self, arguments):
        self.arguments = arguments
        self.selected_tools = []

    def chat(self, messages, tool_specs, response_schema=None):
        self.selected_tools = [spec['function']['name'] for spec in tool_specs]
        return {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': 'local-call-1', 'type': 'function',
            'function': {'name': 'fix_width', 'arguments': json.dumps(self.arguments)},
        }]}


def make_tool_context(tmp_path, polygons, state_updates=None):
    candidate = tmp_path / 'candidate.gds'
    candidate.write_text(json.dumps(polygons, sort_keys=True))
    layout = FakeLayout(candidate, polygons)
    services = FakeServices(tmp_path, layout)
    digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
    state = {
        'checklist': {'tasks': [{'task_id': 'T1', 'cell': 'TOP'}]},
        'current_task_id': 'T1', 'candidate_path': str(candidate), 'candidate_digest': digest,
        'task_transactions': {}, 'task_checks': {}, 'proposals': {}, 'task_queue': ['T1'],
    }
    state.update(state_updates or {})
    return TaskContext('drc', {}, state, services)


def test_cover_propose_apply_check_updates_candidate_digest(tmp_path):
    polygons = {'CT:1': INNER, 'M1:1': SMALL, 'AA:1': OUTER}
    context = make_tool_context(tmp_path, polygons)
    proposed = fix_cover(context, task_id='T1', action='propose', inner_id='CT:1',
                         groups=[['M1:1'], ['AA:1']], minimum_enclosure=0)
    assert proposed.payload['status'] == 'proposed'
    state = {**context.state, **proposed.updates}
    applied = fix_cover(TaskContext('drc', {}, state, context.services), task_id='T1', action='apply',
                        proposal_id=proposed.payload['proposals'][0]['proposal_id'])
    assert applied.payload['status'] == 'applied'
    assert applied.updates['candidate_digest'] != context.state['candidate_digest']
    checked = DrcTools.check_cover(TaskContext('drc', {}, {**state, **applied.updates}, context.services), 'T1')
    assert checked.payload['status'] == 'pass'
    assert checked.updates['task_checks']['T1']['candidate_digest'] == applied.updates['candidate_digest']


def test_failed_area_check_restores_geometry_candidate_and_polygon_digest(tmp_path):
    polygons = {'P:1': [[0, 0], [1, 0], [1, 0.02], [0, 0.02]]}
    context = make_tool_context(tmp_path, polygons)
    before_file = context.services.layout.candidate.read_bytes()
    proposed = fix_area(context, task_id='T1', action='propose', polygon_id='P:1', minimum_area=0.1)
    state = {**context.state, **proposed.updates}
    applied = fix_area(TaskContext('drc', {}, state, context.services), task_id='T1', action='apply',
                       proposal_id=proposed.payload['proposals'][0]['proposal_id'])
    tx_state = {**state, **applied.updates}
    tx_state['task_transactions']['T1']['bound_inputs']['minimum_area'] = 10.0
    failed = DrcTools.check_area(TaskContext('drc', {}, tx_state, context.services), 'T1')
    assert failed.payload['rolled_back'] is True
    assert context.services.layout.polygons['P:1'] == polygons['P:1']
    assert context.services.layout.candidate.read_bytes() == before_file
    assert context.services.layout.polygon_ids.saved_digest == context.state['candidate_digest']


def test_free_text_review_requeues_task_and_preserves_review_context(monkeypatch):
    monkeypatch.setattr(graph_module, 'interrupt', FakeInterrupt({'text': 'sửa width'}))
    services = ReviewServices('unclear')
    nodes = GraphNodes(services)
    nodes.build_active_task_context = lambda state, task_id: {
        'task': {'task_id': task_id, 'rule': 'W_MIN'},
        'layout': {'polygons': [{'id': 'P:1'}], 'region': [0, 0, 1, 1]},
        'layer_map': {'M1': [1, 0]},
    }
    previous_tool = {'role': 'assistant', 'tool_calls': [
        {'id': 'c1', 'function': {'name': 'fix_cover', 'arguments': '{"task_id":"T1"}'}}]}
    tool_result = {'role': 'tool', 'tool_call_id': 'c1', 'content': '{"status":"fail","rolled_back":true}'}
    state = {'messages': [previous_tool, tool_result], 'task_message_start': 2,
             'task_history': [{'task_id': 'T1', 'status': 'unresolved'}], 'task_queue': [],
             'current_task_id': None, 'task_checks': {'T1': {'status': 'fail'}},
             'candidate_path': '/tmp/candidate.gds', 'candidate_digest': 'digest', 'iteration': 9,
             'repair_error': 'cover rollback',
             'response': {'status': 'incomplete'}, 'pending_tools': True}
    update = nodes.review(state)
    assert update['review']['decision'] == 'continue'
    assert update['task_queue'] == ['T1'] and update['current_task_id'] == 'T1'
    assert update['iteration'] == 0
    assert update['repair_error'] is None
    assert update['response'] is None and update['pending_tools'] is False
    assert update['messages'][-1]['role'] == 'user'
    review_message = json.loads(update['messages'][-1]['content'])
    assert review_message['user_review'] == 'sửa width'
    assert review_message['last_tool']['name'] == 'fix_cover'
    assert review_message['last_tool']['result']['rolled_back'] is True
    assert review_message['active_task']['layout']['polygons'][0]['id'] == 'P:1'
    assert review_message['active_task']['layer_map'] == {'M1': [1, 0]}
    assert nodes._select_task_messages(update['messages'], 99)[-1] == update['messages'][-1]
    assert update['messages'][-1] not in nodes._select_task_messages(update['messages'], 99, 'T2')


def test_width_proposal_meets_minimum_and_preserves_m1_aa_cover():
    inner = [[1, 1], [1.08, 1], [1.08, 1.4], [1, 1.4]]
    m1 = [[0.8, 0.8], [1.3, 0.8], [1.3, 1.6], [0.8, 1.6]]
    aa = [[0.9, 0.9], [1.2, 0.9], [1.2, 1.5], [0.9, 1.5]]
    built = WidthProposalBuilder().run(
        inner, minimum_width=0.12, grid=0.01, polygon_id='CT:1',
        cover_context={'groups': [['M1:1'], ['AA:1']], 'minimum_enclosure': 0},
        cover_polygons={'M1:1': m1, 'AA:1': aa},
    )
    assert built['status'] == 'fail'
    after = built['proposals'][0]['changes'][0]['after_points_user']
    assert WidthCheck().run(after, 0.12, 0.01)['status'] == 'pass'
    assert CoverCheck().run(inner=after, groups=[[m1], [aa]], minimum=0, precision=0.01)['status'] == 'pass'


def test_area_cover_context_contracts_bloated_cover_without_losing_enclosure():
    inner = [[0, 0], [1, 0], [1, 1], [0, 1]]
    bloated = [[-1, -1], [2, -1], [2, 2], [-1, 2]]
    built = AreaProposalBuilder().run(
        bloated, grid=0.01, polygon_id='M1:1',
        cover_context={'protected_inner_ids': ['CT:1'], 'minimum_enclosure': 0.2},
        inner_polygons=[inner],
    )
    assert built['status'] == 'fail'
    change = built['proposals'][0]['changes'][0]
    assert change['polygon_id'] == 'M1:1'
    assert CoverCheck().run(inner=inner, outer=change['after_points_user'], minimum=0.2, precision=0.01)['status'] == 'pass'


def test_review_exposes_width_tool_and_full_width_transaction(tmp_path):
    polygons = {
        'CT:1': [[1, 1], [1.08, 1], [1.08, 1.4], [1, 1.4]],
        'M1:1': [[0.8, 0.8], [1.3, 0.8], [1.3, 1.6], [0.8, 1.6]],
        'AA:1': [[0.9, 0.9], [1.2, 0.9], [1.2, 1.5], [0.9, 1.5]],
    }
    context = make_tool_context(tmp_path, polygons)
    response_schema = json.loads(Path('FAGENT/tasks/drc/schemas/drc.json').read_text())
    tool_specs = [tool.task_tool.model_spec(name) for name, tool in (
        ('fix_cover', fix_cover), ('fix_area', fix_area), ('fix_width', fix_width),
    )]
    nodes = GraphNodes(SimpleNamespace(layout=context.services.layout))
    exposed, _ = nodes.drc_repair_schemas(
        {**context.state, 'review': {'decision': 'continue', 'context': {'task_id': 'T1'}}},
        tool_specs, response_schema, verified_pass=True,
    )
    assert {item['function']['name'] for item in exposed} == {'fix_cover', 'fix_area', 'fix_width'}
    width_schema = next(item for item in exposed if item['function']['name'] == 'fix_width')
    assert width_schema['function']['parameters']['properties']['action']['enum'] == ['propose']
    assert 'save_gds' not in {tool.__name__ for tool in DrcTools().run()}

    proposed = fix_width(context, task_id='T1', action='propose', polygon_id='CT:1', minimum_width=0.12,
                         cover_context={'groups': [['M1:1'], ['AA:1']], 'minimum_enclosure': 0})
    assert proposed.payload['status'] == 'proposed'
    state = {**context.state, **proposed.updates}
    applied = fix_width(TaskContext('drc', {}, state, context.services), task_id='T1', action='apply',
                        proposal_id=proposed.payload['proposals'][0]['proposal_id'])
    assert applied.payload['status'] == 'applied'
    checked = DrcTools.check_width(TaskContext('drc', {}, {**state, **applied.updates}, context.services), 'T1')
    assert checked.payload['status'] == 'pass'
    assert checked.payload['measurement']['cover']['status'] == 'pass'


def test_review_only_stops_when_empty_or_classifier_requests_stop(monkeypatch):
    monkeypatch.setattr(graph_module, 'interrupt', FakeInterrupt({'feedback': 'sửa width'}))
    nodes = GraphNodes(ReviewServices('stop'))
    nodes.build_active_task_context = lambda state, task_id: {'task': {'task_id': task_id}}
    state = {'messages': [], 'task_history': [{'task_id': 'T1'}], 'task_queue': [],
             'candidate_digest': 'd', 'task_checks': {}}
    assert nodes.review(state)['review']['decision'] == 'stop'


def test_review_route_goes_to_repair_and_workflow_hides_save_tool():
    workflow = WorkflowLoader(Path('FAGENT/tasks/drc/workflow.yaml')).run()
    review_step = next(step for step in workflow.steps if step.id == 'human_review')
    assert review_step.routes['continue'] == 'repair'
    repair_step = next(step for step in workflow.steps if step.id == 'repair')
    assert {'fix_cover', 'fix_area', 'fix_width', 'check_cover', 'check_area', 'check_width'} <= set(repair_step.tools)
    assert 'save_gds' not in repair_step.tools
    nodes = GraphNodes(SimpleNamespace(workflow=workflow, conversation_trace=None))
    assert nodes.route_task_step({'review': {'decision': 'continue'}}, review_step) == 'repair'


def test_local_model_can_select_and_execute_width_repair_after_review(tmp_path):
    polygons = {
        'CT:1': [[1, 1], [1.08, 1], [1.08, 1.4], [1, 1.4]],
        'M1:1': [[0.8, 0.8], [1.3, 0.8], [1.3, 1.6], [0.8, 1.6]],
        'AA:1': [[0.9, 0.9], [1.2, 0.9], [1.2, 1.5], [0.9, 1.5]],
    }
    context = make_tool_context(tmp_path, polygons)
    loaded = WorkflowLoader(Path('FAGENT/tasks/drc/workflow.yaml')).run()
    step = next(item for item in loaded.steps if item.id == 'repair')
    workflow = SimpleNamespace(task='drc', request_builder=None, response_schema=loaded.response_schema)
    model = LocalModelStub({
        'task_id': 'T1', 'action': 'propose', 'polygon_id': 'CT:1', 'minimum_width': 0.12,
        'cover_context': {'groups': [['M1:1'], ['AA:1']], 'minimum_enclosure': 0},
    })
    services = context.services
    services.workflow = workflow
    services.config = {}
    services.max_iterations = 4
    services.chat = model.chat
    services.node_prompt = lambda step: 'Repair the active DRC marker.'
    nodes = GraphNodes(services)
    nodes.build_active_task_context = lambda state, task_id: {'task': {'task_id': task_id}}
    state = {**context.state, 'messages': [], 'task_message_start': 0, 'drc_context': {},
             'task_transactions': {}, 'task_checks': {}, 'review': {
                 'decision': 'continue', 'context': {'task_id': 'T1', 'user_review': 'sửa width'}},
             'iteration': 0, 'checklist': context.state['checklist']}
    agent = nodes.execute_agent(TaskContext('drc', {}, state, services), step)
    assert {'fix_cover', 'fix_area', 'fix_width'} <= set(model.selected_tools)
    tool_context = TaskContext('drc', {}, {**state, **agent}, services)
    executed = nodes.execute_tools(tool_context, step)
    assert json.loads(executed['messages'][-1]['content'])['status'] == 'proposed'

