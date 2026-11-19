from pathlib import Path
import shutil

import pytest
import yaml

from FAGENT.engine.workflow import load_workflow


@pytest.fixture
def task(tmp_path):
    assets = Path(__file__).resolve().parents[1] / 'FAGENT'
    shutil.copytree(assets / 'workflows', tmp_path / 'workflows')
    shutil.copytree(assets / 'knowledge' / 'prompts', tmp_path / 'knowledge' / 'prompts')
    return tmp_path / 'workflows' / 'drc_repair.yaml'


def test_valid_drc(task):
    workflow = load_workflow(task)
    assert workflow.task == 'drc'
    assert workflow.steps[0].action == 'read_drc'
    assert workflow.digest


@pytest.mark.parametrize('defect', ['prompt', 'schema', 'duplicate', 'action', 'escape', 'cycle', 'dependency'])
def test_invalid_workflow(task, defect):
    data = yaml.safe_load(task.read_text())
    if defect in ('prompt', 'schema'):
        (task.parent.parent / (data['prompts'][0] if defect == 'prompt' else data['schema'])).unlink()
    elif defect == 'duplicate':
        data['steps'].append(data['steps'][0])
    elif defect == 'action':
        data['steps'][0]['action'] = 'os.system'
    elif defect == 'escape':
        data['prompts'][0] = '../outside.txt'
    else:
        data['steps'][0]['requires'] = [data['steps'][-1]['id'] if defect == 'cycle' else 'missing']
    task.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError):
        load_workflow(task)


def test_schema_rejects_external_reference(task):
    schema = task.parent / 'schemas' / 'drc.json'
    schema.write_text('{"type":"object","properties":{"data":{"$ref":"https://example.invalid/schema.json"}}}')
    with pytest.raises(ValueError, match='reference'):
        load_workflow(task)
