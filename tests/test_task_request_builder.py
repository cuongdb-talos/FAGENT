import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.workflow_loader import WorkflowLoader


def test_task_local_request_builder(tmp_path):
    task = tmp_path / 'sample'
    task.mkdir()
    path = task / 'workflow.yaml'
    path.write_text(
        'task: sample\nversion: 1\ninputs: []\n'
        'request_builder: model_payload:build_request\n'
        'nodes: [{id: read, action: "steps:read", next: END}]\n'
    )
    (task / 'steps.py').write_text('def read(context): return {}\n')
    builder = task / 'model_payload.py'
    builder.write_text('def build_request(state, prompt, services, focused):\n    return {"system": prompt, "user": {"value": 1}}\n')
    workflow = WorkflowLoader(path, tmp_path).run()
    nodes = GraphNodes(SimpleNamespace(config={}, workflow=workflow))
    messages, payload = nodes.build_model_request({'plan': 'private'}, 'Inspect.', task_name='sample')
    assert messages == [
        {'role': 'system', 'content': 'Inspect.'},
        {'role': 'user', 'content': json.dumps({'value': 1})},
    ]
    assert payload == {'value': 1}
    builder.write_text(builder.read_text().replace('1', '2'))
    assert WorkflowLoader(path, tmp_path).run().digest != workflow.digest
    builder.write_text('def build_request(state): return {}')
    with pytest.raises(ValueError, match='request_builder'):
        WorkflowLoader(path, tmp_path).run()
    builder.unlink()
    with pytest.raises(ValueError, match='request_builder'):
        WorkflowLoader(path, tmp_path).run()
    legacy_path = Path('FAGENT/tasks/lvs/workflow.yaml')
    assert WorkflowLoader(legacy_path).run().request_builder is None


def test_drc_payload_keeps_live_geometry_and_only_current_marker_history():
    from FAGENT.tasks.drc.model_payload import build_request

    services = SimpleNamespace(
        config={}, build_system_message=lambda **kwargs: 'Configured goal',
        layout=SimpleNamespace(library=SimpleNamespace(unit=1e-6, precision=1e-9)),
        workflow=SimpleNamespace(request_builder=build_request),
    )
    nodes = GraphNodes(services)
    geometry = {'id': 'CELL:poly:1', 'layer': 30, 'datatype': 0,
                'points': [[0, 0], [1, 0], [0, 1]], 'editable': True}
    nodes.build_active_task_context = lambda state, task_id: {
        'task': {'task_id': task_id, 'rule': 'GT_A_13'},
        'drc': {'marker_user': [[0, 0], [1, 0], [0, 1]], 'rule_evidence': {
            'rule_type': 'area', 'required_area_user': 1, 'target_layer': 'GT',
            'layer_mapping': {'GT': {'layer': 30, 'datatype': 0}},
        }},
        'layout': {'cell': 'CELL', 'polygons': [geometry], 'complete': False,
                   'candidate_digest': state['candidate_digest']},
    }
    call = {'role': 'assistant', 'tool_calls': [{'id': 'current'}]}
    result = {'role': 'tool', 'tool_call_id': 'current', 'content': '{}'}
    state = {'current_task_id': 'task-2', 'candidate_digest': 'new',
             'plan': {'secret': True}, 'task_message_start': 1,
             'messages': [{'role': 'assistant', 'content': 'previous marker'}, call, result]}
    messages, payload = nodes.build_model_request(state, 'Repair.')
    assert messages[2:] == [call, result]
    assert set(payload) == {'task_id', 'cell', 'unit', 'grid', 'violation', 'layer_map', 'layout', 'task_history'}
    assert payload['task_id'] == 'task-2'
    assert payload['grid'] == pytest.approx(0.001)
    assert payload['violation']['marker_metrics']['area'] == 0.5
    assert payload['layout']['polygons'][0]['points'] == geometry['points']
    assert payload['layout']['candidate_digest'] == 'new'
    assert payload['layout']['complete'] is False
