import json
from pathlib import Path

import pytest

from FAGENT.engine.task_contracts import TaskContext
from FAGENT.tasks.drc.checklist import Checklist
from FAGENT.tasks.drc.tools import read_checklist, update_checklist


def _sample_report():
    return {
        'source': 'sample.drc.results',
        'top_cell': 'top',
        'dbu_per_user_unit': 1000,
        'rules': [
            {
                'name': 'R_EN',
                'reported_count': 2,
                'total_count': 2,
                'description': ['Enclosure rule'],
                'geometry': [
                    {'type': 'p', 'id': 1, 'points_dbu': [[0, 0], [10, 0], [10, 10]]},
                    {'type': 'p', 'id': 2, 'points_dbu': [[20, 0], [30, 0], [30, 10]]},
                ],
            },
            {
                'name': 'R_DUP',
                'reported_count': 2,
                'total_count': 2,
                'description': ['Duplicate geometry IDs'],
                'geometry': [
                    {'type': 'p', 'id': 1, 'points_dbu': [[0, 0], [5, 0], [5, 5]]},
                    {'type': 'p', 'id': 1, 'points_dbu': [[10, 0], [15, 0], [15, 5]]},
                ],
            },
            {
                'name': 'R_MISMATCH',
                'reported_count': 1,
                'total_count': 2,
                'description': ['Count mismatch'],
                'geometry': [
                    {'type': 'p', 'id': 1, 'points_dbu': [[0, 0], [5, 0], [5, 5]]},
                ],
            },
            {
                'name': 'R_DENSITY',
                'reported_count': 1,
                'total_count': 1,
                'description': ['Metal density'],
                'geometry': [
                    {'type': 'p', 'id': 1, 'points_dbu': [[0, 0], [5, 0], [5, 5]]},
                ],
            },
        ],
    }


def _proposed_for_sample(skip_density=True):
    return [
        {'rule': 'R_EN', 'violation_id': 1, 'occurrence': 0, 'work': 'Fix R_EN 1', 'status': 'pending', 'reason': ''},
        {'rule': 'R_EN', 'violation_id': 2, 'occurrence': 0, 'work': 'Fix R_EN 2', 'status': 'pending', 'reason': ''},
        {'rule': 'R_DUP', 'violation_id': 1, 'occurrence': 0, 'work': 'Fix R_DUP a', 'status': 'pending', 'reason': ''},
        {'rule': 'R_DUP', 'violation_id': 1, 'occurrence': 1, 'work': 'Fix R_DUP b', 'status': 'pending', 'reason': ''},
        {'rule': 'R_MISMATCH', 'violation_id': 1, 'occurrence': 0, 'work': 'Inspect R_MISMATCH 1', 'status': 'pending', 'reason': ''},
        {
            'rule': 'R_DENSITY',
            'violation_id': 1,
            'occurrence': 0,
            'work': 'Skip density',
            'status': 'skipped' if skip_density else 'pending',
            'reason': 'Goal excludes density' if skip_density else '',
        },
    ]


def test_checklist_creation_disambiguation_mismatch_and_skip_policy():
    report = _sample_report()
    cl = Checklist().run(report, _proposed_for_sample(skip_density=True), goal='Fix all DRC errors but ignore error about density.')
    tasks = cl.read()['tasks']

    # 2 R_EN + 2 R_DUP + 1 R_MISMATCH geom + 1 R_MISMATCH missing + 1 R_DENSITY = 7 tasks
    assert len(tasks) == 7
    assert len({t['task_id'] for t in tasks}) == 7

    dup_tasks = [t for t in tasks if t['rule'] == 'R_DUP']
    assert [t['occurrence'] for t in dup_tasks] == [0, 1]

    mismatch_tasks = [t for t in tasks if t['rule'] == 'R_MISMATCH']
    assert len(mismatch_tasks) == 2
    assert any(t['status'] == 'unresolved' and t['violation_id'] is None for t in mismatch_tasks)

    density_task = next(t for t in tasks if t['rule'] == 'R_DENSITY')
    assert density_task['status'] == 'skipped'

    # Skipping density without goal permission is rejected
    with pytest.raises(ValueError):
        Checklist().run(report, _proposed_for_sample(skip_density=True), goal='Fix all DRC errors.')


def test_checklist_updates_invalidation_and_persistence(tmp_path):
    report = _sample_report()
    cl = Checklist().run(report, _proposed_for_sample(skip_density=True), goal='Ignore density.')
    t1_id = cl.read()['tasks'][0]['task_id']

    # Unknown task or status rejected
    with pytest.raises(ValueError):
        cl.update('nonexistent', 'pass', 'w', 'reason', ['top:poly:0'], [{'check': 'ok'}])
    with pytest.raises(ValueError):
        cl.update(t1_id, 'bogus', 'w', 'reason', ['top:poly:0'], [{'check': 'ok'}])

    # Pass preserves evidence
    cl.update(t1_id, 'pass', 'Checked enclosure', 'Verified >= 0.03', ['top:poly:0'], [{'tool': 'check_cover', 'status': 'pass'}])
    t1 = next(t for t in cl.read()['tasks'] if t['task_id'] == t1_id)
    assert t1['status'] == 'pass'
    assert t1['stale'] is False
    assert len(t1['evidence']) == 1

    # Edit invalidates passed evidence conservatively and marks it stale
    cl.invalidate('top', 'top:poly:0')
    t1_after = next(t for t in cl.read()['tasks'] if t['task_id'] == t1_id)
    assert t1_after['status'] != 'pass'
    assert t1_after['stale'] is True
    assert t1_after['evidence'][0]['stale'] is True

    # Atomic save and digest-bound restore
    path = tmp_path / 'checklist.json'
    cl.save(path)
    expected_digest = cl.digest()
    restored = Checklist().restore(path, expected_digest)
    assert restored.read()['tasks'] == cl.read()['tasks']

    # Wrong digest or corrupt/tampered file fails closed
    with pytest.raises(ValueError):
        Checklist().restore(path, 'wrong-digest')

    tampered = json.loads(path.read_text())
    tampered['tasks'][0]['status'] = 'pass'
    path.write_text(json.dumps(tampered))
    with pytest.raises(ValueError):
        Checklist().restore(path, expected_digest)


def test_checklist_tools_roundtrip(tmp_path):
    report = _sample_report()
    cl = Checklist().run(report, _proposed_for_sample(skip_density=True), goal='Ignore density.')
    services = type('Services', (), {'rundir': tmp_path, 'check_output_path': lambda self, p: None})()
    state = {'checklist': cl.read(), 'checklist_digest': cl.digest()}
    ctx = TaskContext(task='drc', inputs={}, state=state, services=services)

    read_res = read_checklist(ctx)
    assert len(read_res.payload['tasks']) == 7

    t1_id = read_res.payload['tasks'][0]['task_id']
    upd_res = update_checklist(
        ctx,
        task_id=t1_id,
        status='pass',
        work='Checked enclosure',
        reason='Margin OK',
        polygon_ids=['top:poly:0'],
        evidence=[{'tool': 'check_cover', 'status': 'pass'}],
    )
    assert upd_res.updates['checklist_digest'] != state['checklist_digest']
    assert (tmp_path / 'checklist.json').is_file()


def test_checklist_reconciles_partial_and_empty_model_suggestions():
    # 1. Two-ID rule with only the first task proposed
    report_two = {
        'rules': [
            {
                'name': 'R_EN',
                'reported_count': 2,
                'total_count': 2,
                'geometry': [{'id': 1}, {'id': 2}],
            }
        ]
    }
    proposed_partial = [
        {'rule': 'R_EN', 'violation_id': 1, 'occurrence': 0, 'work': 'Fix first', 'status': 'pending', 'reason': ''}
    ]
    cl_partial = Checklist().run(report_two, proposed_partial)
    tasks_partial = cl_partial.read()['tasks']
    assert len(tasks_partial) == 2
    assert tasks_partial[0]['task_id'] == 'task-1'
    assert tasks_partial[0]['rule'] == 'R_EN'
    assert tasks_partial[0]['violation_id'] == 1
    assert tasks_partial[0]['status'] == 'pending'
    assert tasks_partial[0]['work'] == 'Fix first'
    assert tasks_partial[0]['evidence'] == []
    assert tasks_partial[0]['stale'] is False

    assert tasks_partial[1]['task_id'] == 'task-2'
    assert tasks_partial[1]['rule'] == 'R_EN'
    assert tasks_partial[1]['violation_id'] == 2
    assert tasks_partial[1]['status'] == 'unresolved'
    assert tasks_partial[1]['reason'] != ''
    assert tasks_partial[1]['evidence'] == []
    assert tasks_partial[1]['stale'] is False

    # 2. Empty plan -> all report violations retained as unresolved in report order
    cl_empty = Checklist().run(report_two, [])
    tasks_empty = cl_empty.read()['tasks']
    assert len(tasks_empty) == 2
    assert [t['status'] for t in tasks_empty] == ['unresolved', 'unresolved']
    assert [t['violation_id'] for t in tasks_empty] == [1, 2]

    # 3. Duplicate IDs distinguished by occurrence
    report_dup = {
        'rules': [
            {
                'name': 'R_DUP',
                'reported_count': 2,
                'total_count': 2,
                'geometry': [{'id': 1}, {'id': 1}],
            }
        ]
    }
    proposed_occ1 = [
        {'rule': 'R_DUP', 'violation_id': 1, 'occurrence': 1, 'work': 'Fix second occ', 'status': 'pending', 'reason': ''}
    ]
    cl_dup = Checklist().run(report_dup, proposed_occ1)
    tasks_dup = cl_dup.read()['tasks']
    assert len(tasks_dup) == 2
    assert tasks_dup[0]['occurrence'] == 0
    assert tasks_dup[0]['status'] == 'unresolved'
    assert tasks_dup[1]['occurrence'] == 1
    assert tasks_dup[1]['status'] == 'pending'
    assert tasks_dup[1]['work'] == 'Fix second occ'

    # 4. Summary-only input (counts without geometry)
    report_summary = {'violations_by_rule': {'R_SUM': 2}}
    cl_sum = Checklist().run(report_summary, [])
    tasks_sum = cl_sum.read()['tasks']
    assert len(tasks_sum) == 2
    assert all(t['status'] == 'unresolved' for t in tasks_sum)
    assert all(t['violation_id'] is None for t in tasks_sum)

    # 5. Invalid proposals: unknown proposal, duplicate proposal, unauthorized skip
    with pytest.raises(ValueError, match='does not match'):
        Checklist().run(report_two, [{'rule': 'R_NONEXISTENT', 'violation_id': 1, 'occurrence': 0, 'work': 'w'}])

    with pytest.raises(ValueError, match='does not match'):
        Checklist().run(report_two, [
            {'rule': 'R_EN', 'violation_id': 1, 'occurrence': 0, 'work': 'w1'},
            {'rule': 'R_EN', 'violation_id': 1, 'occurrence': 0, 'work': 'w2'},
        ])

    with pytest.raises(ValueError, match='Skip not permitted'):
        Checklist().run(report_two, [
            {'rule': 'R_EN', 'violation_id': 1, 'occurrence': 0, 'work': 'w', 'status': 'skipped', 'reason': 'skip'}
        ], goal='Fix all errors')

    # Allowed skip with matching goal
    cl_skip = Checklist().run(report_two, [
        {'rule': 'R_EN', 'violation_id': 1, 'occurrence': 0, 'work': 'w', 'status': 'skipped', 'reason': 'skip R_EN'}
    ], goal='ignore R_EN')
    assert cl_skip.read()['tasks'][0]['status'] == 'skipped'
