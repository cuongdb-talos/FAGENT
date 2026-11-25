"""Tests for sequential node action lists in workflow definitions and execution."""

# === SECTION 1: Imports ===
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.workflow_loader import WorkflowLoader


# === SECTION 2: Module-Level Helpers ===
def write_task_package(root: Path, task_name: str, workflow_data: dict, steps_source: str) -> Path:
    """Create a temporary task package with workflow.yaml and steps.py."""
    task_dir = root / task_name
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / 'steps.py').write_text(steps_source, encoding='utf-8')
    wf_path = task_dir / 'workflow.yaml'
    wf_path.write_text(yaml.safe_dump(workflow_data, sort_keys=False), encoding='utf-8')
    return wf_path


def make_services(workflow: SimpleNamespace) -> SimpleNamespace:
    """Build minimal services namespace for GraphNodes execution."""
    return SimpleNamespace(workflow=workflow, trace=None, conversation_trace=None)


# === SECTION 3: Tests ===
def test_scalar_action_compatibility(tmp_path: Path) -> None:
    """Scalar task-local action string continues to load and execute."""
    steps_source = (
        "def first_step(context):\n"
        "    return {'alpha': context.inputs['value'] + 1}\n"
    )
    wf_data = {
        'task': 'sample',
        'version': 1,
        'inputs': ['value'],
        'nodes': [{'id': 'read', 'action': 'steps:first_step', 'next': 'END'}],
    }
    wf_path = write_task_package(tmp_path, 'sample', wf_data, steps_source)
    workflow = WorkflowLoader(wf_path, tmp_path).run()
    nodes = GraphNodes(make_services(workflow)).run()

    result = nodes.run_task_step(
        {'inputs': {'value': 10}, 'task_state': {}},
        workflow.nodes[0],
    )
    assert result['alpha'] == 11
    assert result['task_state']['alpha'] == 11


def test_action_list_ordered_execution_and_state_propagation(tmp_path: Path) -> None:
    """Action list executes in order and propagates each step's updates to subsequent actions."""
    steps_source = (
        "def step_one(context):\n"
        "    order = list(context.state.get('order', []))\n"
        "    order.append('one')\n"
        "    return {'order': order, 'from_one': 'ok'}\n"
        "\n"
        "def step_two(context):\n"
        "    order = list(context.state.get('order', []))\n"
        "    order.append('two')\n"
        "    return {'order': order, 'saw_one': context.state.get('from_one')}\n"
    )
    wf_data = {
        'task': 'sample',
        'version': 1,
        'inputs': [],
        'nodes': [{'id': 'read', 'action': ['steps:step_one', 'steps:step_two'], 'next': 'END'}],
    }
    wf_path = write_task_package(tmp_path, 'sample', wf_data, steps_source)
    workflow = WorkflowLoader(wf_path, tmp_path).run()
    nodes = GraphNodes(make_services(workflow)).run()

    result = nodes.run_task_step({'inputs': {}, 'task_state': {}}, workflow.nodes[0])
    assert result['order'] == ['one', 'two']
    assert result['from_one'] == 'ok'
    assert result['saw_one'] == 'ok'
    assert result['task_state']['order'] == ['one', 'two']
    assert result['task_state']['saw_one'] == 'ok'


def test_empty_action_list_rejected(tmp_path: Path) -> None:
    """Empty action list must be rejected by WorkflowLoader."""
    steps_source = "def step_one(context):\n    return {}\n"
    wf_data = {
        'task': 'sample',
        'version': 1,
        'inputs': [],
        'nodes': [{'id': 'read', 'action': [], 'next': 'END'}],
    }
    wf_path = write_task_package(tmp_path, 'sample', wf_data, steps_source)
    with pytest.raises(ValueError, match='Invalid workflow'):
        WorkflowLoader(wf_path, tmp_path).run()


@pytest.mark.parametrize('bad_action', [['agent', 'steps:step_one'], ['steps:step_one', 'plan'], ['review']])
def test_mixed_or_builtin_action_list_rejected(tmp_path: Path, bad_action: list[str]) -> None:
    """Lists containing built-in actions (agent, plan, review) must be rejected."""
    steps_source = "def step_one(context):\n    return {}\n"
    wf_data = {
        'task': 'sample',
        'version': 1,
        'inputs': [],
        'nodes': [{'id': 'read', 'action': bad_action, 'next': 'END'}],
    }
    wf_path = write_task_package(tmp_path, 'sample', wf_data, steps_source)
    with pytest.raises(ValueError, match='Invalid workflow'):
        WorkflowLoader(wf_path, tmp_path).run()
