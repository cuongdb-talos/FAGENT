import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
from langgraph.graph import END
import pytest

from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.task_contracts import TaskContext
from FAGENT.engine.workflow_loader import WorkflowLoader
from FAGENT.tasks.drc.checklist import Checklist


def test_workflow_loader_loads_plan_action_and_assets():
    workflow_path = Path('FAGENT/tasks/drc/workflow.yaml').resolve()
    workflow = WorkflowLoader(workflow_path).run()

    step_ids = [s.id for s in workflow.steps]
    assert step_ids == ['read', 'plan', 'repair', 'human_review']

    plan_step = next(s for s in workflow.steps if s.id == 'plan')
    assert plan_step.action == 'plan'
    assert plan_step.next == 'repair'
    assert isinstance(plan_step.prompt, str) and 'DRC planning agent' in plan_step.prompt
    assert isinstance(plan_step.schema, dict)
    assert 'tasks' in plan_step.schema.get('properties', {})

    repair_step = next(s for s in workflow.steps if s.id == 'repair')
    assert repair_step.action == 'agent'
    assert repair_step.next == 'human_review'
    expected_tools = {'read_gds', 'replace_polygon', 'check_cover', 'read_checklist', 'update_checklist'}
    assert expected_tools.issubset(set(repair_step.tools.keys()))


def test_plan_schema_valid_and_invalid():
    schema_path = Path('FAGENT/tasks/drc/schemas/plan.json')
    schema = json.loads(schema_path.read_text())

    valid_plan = {
        'summary': 'Planned 2 tasks',
        'tasks': [
            {
                'rule': 'R_EN',
                'violation_id': 1,
                'occurrence': 0,
                'work': 'Fix enclosure',
                'status': 'pending',
                'reason': '',
            },
            {
                'rule': 'R_DENSITY',
                'violation_id': 1,
                'occurrence': 0,
                'work': 'Skip density',
                'status': 'skipped',
                'reason': 'Goal excludes density',
            },
        ],
    }
    jsonschema.Draft202012Validator(schema).validate(valid_plan)

    # Invalid: missing summary
    invalid_missing_summary = {'tasks': valid_plan['tasks']}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(invalid_missing_summary)

    # Invalid status
    invalid_status = {
        'summary': 'Bad status',
        'tasks': [{'rule': 'R1', 'violation_id': 1, 'occurrence': 0, 'work': 'w', 'status': 'unknown_status', 'reason': ''}],
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(schema).validate(invalid_status)


def test_execute_plan_direct_functional(tmp_path):
    rundir = tmp_path / 'rundir'
    rundir.mkdir(parents=True)
    schema_path = Path('FAGENT/tasks/drc/schemas/plan.json')
    schema = json.loads(schema_path.read_text())

    plan_content = {
        'summary': 'Planned 1 task',
        'tasks': [
            {'rule': 'R_EN', 'violation_id': 1, 'occurrence': 0, 'work': 'Fix enclosure', 'status': 'pending', 'reason': ''}
        ],
    }

    class _MockServices:
        def __init__(self, rundir):
            self.rundir = rundir
            self.config = {'goal': 'Fix DRC'}
            self.workflow = SimpleNamespace(response_schema=schema)

        def chat(self, messages, specs, response_schema=None):
            return {'role': 'assistant', 'content': json.dumps(plan_content)}

        def check_output_path(self, path):
            return path

    services = _MockServices(rundir)
    nodes = GraphNodes(services)

    step = SimpleNamespace(id='plan', action='plan', prompt='Plan prompt', schema=schema)
    context = TaskContext(
        task='drc',
        inputs={},
        state={
            'messages': [],
            'report': {
                'rules': [
                    {'name': 'R_EN', 'total_count': 1, 'geometry': [{'id': 1}]}
                ]
            },
        },
        services=services,
    )

    updates = nodes.execute_plan(context, step)
    assert 'checklist' in updates
    assert updates['checklist']['summary']['counts']['pending'] == 1
    assert updates['checklist_digest'] == updates['checklist']['digest']
    assert (rundir / 'checklist.json').exists()

    # Rejection of tool calls in plan step
    class _MockToolCallingServices(_MockServices):
        def chat(self, messages, specs, response_schema=None):
            return {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call1'}]}

    nodes_tool_fail = GraphNodes(_MockToolCallingServices(rundir))
    with pytest.raises(ValueError, match='Plan step cannot invoke tools'):
        nodes_tool_fail.execute_plan(context, step)


def test_premature_completion_guard_functional():
    schema = json.loads(Path('FAGENT/tasks/drc/schemas/drc.json').read_text())

    class _MockServices:
        def __init__(self):
            self.max_iterations = 5
            self.config = {'goal': 'Fix DRC'}
            self.workflow = SimpleNamespace(response_schema=schema)

        def chat(self, messages, specs, response_schema=None):
            return {
                'role': 'assistant',
                'content': json.dumps({
                    'decision': 'completed',
                    'status': 'incomplete',
                    'reason': 'Premature attempt',
                    'violations_analysis': [],
                    'recommendations': 'none',
                }),
            }

    services = _MockServices()
    nodes = GraphNodes(services)
    step = SimpleNamespace(id='repair', action='agent', tools={}, schema=schema)

    # 1. With pending checklist task -> guard blocks completion (response set to None)
    pending_checklist = Checklist().run(
        report={'rules': [{'name': 'R1', 'total_count': 1, 'geometry': [{'id': 1}]}]},
        proposed_tasks=[{'rule': 'R1', 'violation_id': 1, 'occurrence': 0, 'work': 'fix', 'status': 'pending', 'reason': ''}],
    ).read()

    context = TaskContext(
        task='drc',
        inputs={},
        state={'messages': [], 'checklist': pending_checklist, 'iteration': 0},
        services=services,
    )
    updates = nodes.execute_agent(context, step)
    assert updates.get('response') is None
    assert any('Cannot complete repair while pending' in json.dumps(m) for m in updates.get('messages', []))

    # 2. When checklist has all resolved (e.g. skipped or pass) -> guard permits completion
    resolved_checklist = Checklist().run(
        report={'rules': [{'name': 'R_DENSITY', 'total_count': 1, 'geometry': [{'id': 1}]}]},
        proposed_tasks=[{'rule': 'R_DENSITY', 'violation_id': 1, 'occurrence': 0, 'work': 'skip', 'status': 'skipped', 'reason': 'goal excludes density'}],
        goal='ignore density',
    ).read()
    context_resolved = TaskContext(
        task='drc',
        inputs={},
        state={'messages': [], 'checklist': resolved_checklist, 'iteration': 0},
        services=services,
    )
    updates_resolved = nodes.execute_agent(context_resolved, step)
    assert updates_resolved.get('response') is not None
    assert updates_resolved['response']['decision'] == 'completed'


def test_routing_functional():
    nodes = GraphNodes(None)

    step_plan = SimpleNamespace(id='plan', action='plan', next='repair')
    assert nodes.route_task_step({'failed': False}, step_plan) == 'repair'

    step_repair = SimpleNamespace(id='repair', action='agent', next='human_review')
    # If response is None (e.g. blocked by premature guard), retry repair
    assert nodes.route_task_step({'failed': False, 'response': None}, step_repair) == 'repair'
    # If response is set, route to human_review
    assert nodes.route_task_step({'failed': False, 'response': {'decision': 'completed'}}, step_repair) == 'human_review'

    step_review = SimpleNamespace(id='human_review', action='review', next='END', continue_to='repair')
    assert nodes.route_task_step({'failed': False, 'review': {'decision': 'stop'}}, step_review) == END
    assert nodes.route_task_step({'failed': False, 'review': {'decision': 'continue'}}, step_review) == 'repair'
    assert nodes.route_task_step({'failed': False, 'review': {'decision': 'unclear'}}, step_review) == 'human_review'


def test_execute_plan_partial_model_plan_and_negative_cases(tmp_path):
    rundir = tmp_path / 'rundir'
    rundir.mkdir(parents=True)
    schema_path = Path('FAGENT/tasks/drc/schemas/plan.json')
    schema = json.loads(schema_path.read_text())

    # Partial model plan: covers only violation 1 of 2
    plan_content = {
        'summary': 'Planned partial task',
        'tasks': [
            {'rule': 'R_EN', 'violation_id': 1, 'occurrence': 0, 'work': 'Fix enclosure 1', 'status': 'pending', 'reason': ''}
        ],
    }

    class _MockServices:
        def __init__(self, rundir, plan_dict=None, error=None):
            self.rundir = rundir
            self.config = {'goal': 'Fix DRC'}
            self.workflow = SimpleNamespace(response_schema=schema)
            self.plan_dict = plan_dict
            self.error = error

        def chat(self, messages, specs, response_schema=None):
            if self.error:
                raise self.error
            return {'role': 'assistant', 'content': json.dumps(self.plan_dict)}

        def check_output_path(self, path):
            return path

    services = _MockServices(rundir, plan_dict=plan_content)
    nodes = GraphNodes(services)

    step = SimpleNamespace(id='plan', action='plan', prompt='Plan prompt', schema=schema, next='repair')
    context = TaskContext(
        task='drc',
        inputs={},
        state={
            'messages': [],
            'report': {
                'rules': [
                    {'name': 'R_EN', 'total_count': 2, 'geometry': [{'id': 1}, {'id': 2}]}
                ]
            },
        },
        services=services,
    )

    updates = nodes.execute_plan(context, step)
    assert 'checklist' in updates
    checklist = updates['checklist']
    tasks = checklist['tasks']
    assert len(tasks) == 2
    assert tasks[0]['status'] == 'pending'
    assert tasks[0]['violation_id'] == 1
    assert tasks[1]['status'] == 'unresolved'
    assert tasks[1]['violation_id'] == 2
    assert tasks[1]['reason'] != ''

    # Routing proceeds to repair
    assert nodes.route_task_step({'failed': False}, step) == 'repair'

    # Summary correctly indicates incomplete / unresolved
    assert checklist['summary']['counts']['pending'] == 1
    assert checklist['summary']['counts']['unresolved'] == 1
    assert checklist['summary']['all_resolved'] is False

    # Checkpoint digest and persistence
    assert updates['checklist_digest'] == checklist['digest']
    assert (rundir / 'checklist.json').exists()

    # Negative case 1: Schema-invalid response from model fails closed
    invalid_schema_services = _MockServices(rundir, plan_dict={'tasks': []})  # missing 'summary'
    nodes_invalid_schema = GraphNodes(invalid_schema_services)
    with pytest.raises(jsonschema.ValidationError):
        nodes_invalid_schema.execute_plan(context, step)

    # Negative case 2: Model exception propagates fail-fast
    error_services = _MockServices(rundir, error=RuntimeError('Model inference failed'))
    nodes_error = GraphNodes(error_services)
    with pytest.raises(RuntimeError, match='Model inference failed'):
        nodes_error.execute_plan(context, step)
