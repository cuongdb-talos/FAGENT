# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from FAGENT.engine.task_contracts import ToolResult
from FAGENT.tasks.drc.checklist import Checklist
from FAGENT.tasks.drc.steps import Steps
from FAGENT.tasks.drc.tools import DrcTools, get_violation


# === SECTION 2: Test Suite Class ===
class TestDrcReportChecklist:
    """Test suite verifying report-driven checklist creation and get_violation lookup."""

    def test_two_report_violations_yield_pending_items_no_invented_work(self):
        """Assert two report violations yield two unique pending items with original rule/ID/occurrence, no invented work/evidence."""
        report = {
            'top_cell': 'ROM_MCELL0',
            'dbu_per_user_unit': 1000,
            'source': 'test.drc.results',
            'rules': [
                {
                    'name': 'CT_R_11',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 101, 'type': 'p', 'points_dbu': [[0, 0], [100, 100]]}],
                },
                {
                    'name': 'M1_EN_1',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 202, 'type': 'e', 'points_dbu': [[200, 200], [300, 300]]}],
                },
            ],
        }
        checklist = Checklist().run(report, proposed_tasks=None)
        tasks = checklist.tasks
        assert len(tasks) == 2
        assert tasks[0]['task_id'] == 'task-1'
        assert tasks[0]['rule'] == 'CT_R_11'
        assert tasks[0]['violation_id'] == 101
        assert tasks[0]['occurrence'] == 0
        assert tasks[0]['status'] == 'pending'
        assert tasks[0]['work'] == ''
        assert tasks[0]['reason'] == ''
        assert tasks[0]['evidence'] == []
        assert tasks[0]['polygon_ids'] == []

        assert tasks[1]['task_id'] == 'task-2'
        assert tasks[1]['rule'] == 'M1_EN_1'
        assert tasks[1]['violation_id'] == 202
        assert tasks[1]['occurrence'] == 0
        assert tasks[1]['status'] == 'pending'
        assert tasks[1]['work'] == ''
        assert tasks[1]['evidence'] == []

    def test_duplicated_ids_use_distinct_occurrence(self):
        """Assert duplicated violation IDs within a rule receive distinct incremental occurrence counts."""
        report = {
            'rules': [
                {
                    'name': 'CT_R_11',
                    'reported_count': 2,
                    'total_count': 2,
                    'geometry': [
                        {'id': 101, 'type': 'p', 'points_dbu': [[0, 0], [10, 10]]},
                        {'id': 101, 'type': 'p', 'points_dbu': [[20, 20], [30, 30]]},
                    ],
                }
            ]
        }
        checklist = Checklist().run(report, proposed_tasks=None)
        tasks = checklist.tasks
        assert len(tasks) == 2
        assert tasks[0]['violation_id'] == 101
        assert tasks[0]['occurrence'] == 0
        assert tasks[1]['violation_id'] == 101
        assert tasks[1]['occurrence'] == 1

    def test_empty_report_yields_empty_checklist(self):
        """Assert empty report rules or clean rules yield an empty checklist."""
        report = {'rules': []}
        checklist = Checklist().run(report, proposed_tasks=None)
        assert checklist.tasks == []
        summary = checklist.summary()
        assert summary['total'] == 0
        assert summary['all_resolved'] is True

    def test_missing_task_in_get_violation_raises_clear_error(self):
        """Assert requesting an unknown task_id from get_violation raises an explicit error."""
        checklist_data = {
            'tasks': [
                {
                    'task_id': 'task-1',
                    'rule': 'CT_R_11',
                    'violation_id': 101,
                    'occurrence': 0,
                    'status': 'pending',
                    'work': '',
                    'reason': '',
                    'polygon_ids': [],
                    'evidence': [],
                    'stale': False,
                }
            ],
            'summary': {'total': 1},
            'digest': 'fake_digest',
        }
        context = SimpleNamespace(
            state={'checklist': checklist_data, 'candidate_path': None, 'candidate_digest': None},
            services=SimpleNamespace(drc_report={'rules': []}),
        )
        with pytest.raises(ValueError, match="Unknown checklist task_id: task-999"):
            get_violation(context, task_id='task-999')

    def test_get_violation_returns_source_record_task_and_candidate_identity(self):
        """Assert get_violation returns source record, marker coordinates, task, and candidate identity."""
        report = {
            'top_cell': 'ROM_MCELL0',
            'dbu_per_user_unit': 1000,
            'source': 'test.drc.results',
            'rules': [
                {
                    'name': 'CT_R_11',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 101, 'type': 'p', 'points_dbu': [[100, 200], [300, 400]]}],
                }
            ],
        }
        checklist = Checklist().run(report, proposed_tasks=None)
        snapshot = checklist.read()
        context = SimpleNamespace(
            state={
                'checklist': snapshot,
                'report': report,
                'candidate_path': '/path/to/candidate.gds',
                'candidate_digest': 'abc123digest',
            },
            services=SimpleNamespace(drc_report=report),
        )
        result = get_violation(context, task_id='task-1')
        assert isinstance(result, ToolResult)
        payload = result.payload
        assert payload['task']['task_id'] == 'task-1'
        assert payload['task']['rule'] == 'CT_R_11'
        assert payload['violation']['id'] == 101
        assert payload['violation']['type'] == 'p'
        assert payload['violation']['points_dbu'] == [[100, 200], [300, 400]]
        assert payload['owning_cell'] == 'ROM_MCELL0'
        assert payload['candidate_path'] == '/path/to/candidate.gds'
        assert payload['candidate_digest'] == 'abc123digest'
        assert payload['source'] == 'test.drc.results'
        assert payload['dbu_per_user_unit'] == 1000

    def test_steps_read_drc_initializes_and_persists_checklist(self, tmp_path: Path):
        """Assert steps.read_drc deterministically initializes checklist and saves checklist.json in rundir."""
        report = {
            'top_cell': 'ROM_MCELL0',
            'dbu_per_user_unit': 1000,
            'source': 'test.drc.results',
            'rules': [
                {
                    'name': 'CT_R_11',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 101, 'type': 'p', 'points_dbu': [[0, 0], [10, 10]]}],
                }
            ],
        }
        services = SimpleNamespace(
            rundir=str(tmp_path),
            drc_report=report,
            build_drc_context=Mock(return_value={'drc': report, 'knowledge': {'constraints': []}}),
            build_system_message=Mock(return_value='System message'),
            check_output_path=Mock(),
            config={'goal': ''},
        )
        context = SimpleNamespace(state={'messages': []}, services=services)
        step_result = Steps().read_drc(context)

        assert 'checklist' in step_result
        checklist_snapshot = step_result['checklist']
        assert len(checklist_snapshot['tasks']) == 1
        assert checklist_snapshot['tasks'][0]['status'] == 'pending'
        assert checklist_snapshot['tasks'][0]['work'] == ''
        assert step_result['checklist_digest'] == checklist_snapshot['digest']

        checklist_file = tmp_path / 'checklist.json'
        assert checklist_file.is_file()
        restored = Checklist().restore(checklist_file, step_result['checklist_digest'])
        assert len(restored.tasks) == 1
        assert restored.tasks[0]['rule'] == 'CT_R_11'
