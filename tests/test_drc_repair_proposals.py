# === SECTION 1: Imports ===
from types import SimpleNamespace
from unittest.mock import Mock
import gdstk
import pytest

from FAGENT.capabilities.layout.polygon_ids import PolygonIds
from FAGENT.capabilities.layout.repair_proposals import RepairProposals
from FAGENT.engine.task_contracts import ToolResult
from FAGENT.tasks.drc.tools import measure_violation, propose_polygon_fix


# === SECTION 2: Test Suite Class ===
class TestDrcRepairProposals:
    """Test suite verifying DRC violation measurement and deterministic repair proposal generation."""

    def test_rectangle_enclosure_25dbu_and_18dbu_measures_7dbu_deficit(self):
        """Assert a 25 DBU requirement and 18 DBU measurement produce exactly 7 DBU deficit with contributing edges."""
        # Setup CT inner polygon at [1000, 1000] to [1500, 1500] in DBU (500x500 nm)
        # M1 outer polygon at [982, 950] to [1550, 1550] in DBU
        # Left enclosure: 1000 - 982 = 18 DBU. Required: 25 DBU -> Deficit: 7 DBU on left edge.
        # Right enclosure: 1550 - 1500 = 50 DBU (>= 25)
        # Bottom enclosure: 1000 - 950 = 50 DBU (>= 25)
        # Top enclosure: 1550 - 1500 = 50 DBU (>= 25)
        scale = 1000.0  # 1000 DBU per user unit (um)
        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('ROM_MCELL0')
        p_ct = gdstk.Polygon([(1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (1.0, 1.5)], layer=50, datatype=0)
        p_m1 = gdstk.Polygon([(0.982, 0.950), (1.550, 0.950), (1.550, 1.550), (0.982, 1.550)], layer=61, datatype=0)
        cell.add(p_ct, p_m1)
        pids = PolygonIds().run(lib)
        layout = SimpleNamespace(
            library=lib,
            polygon_ids=pids,
            top_cell='ROM_MCELL0',
            get_cell=Mock(return_value=cell),
            gds_data={'top_cell': 'ROM_MCELL0'},
        )

        task = {'rule': 'CT_EN_SUPPORTED', 'violation_id': 1, 'occurrence': 0, 'task_id': 'task-1'}
        rule_evidence = {
            'rule': 'CT_EN_SUPPORTED',
            'supported': True,
            'required_enclosure_dbu': 25,
            'layer_mapping': {'CT': {'layer': 50, 'datatype': 0}, 'M1': {'layer': 61, 'datatype': 0}},
        }
        geometry = {
            'status': 'matched',
            'matched_polygons': [
                {'layer_name': 'CT', 'polygon_id': 'ROM_MCELL0:poly:0', 'points_dbu': [[1000, 1000], [1500, 1000], [1500, 1500], [1000, 1500]]},
                {'layer_name': 'M1', 'polygon_id': 'ROM_MCELL0:poly:1', 'points_dbu': [[982, 950], [1550, 950], [1550, 1550], [982, 1550]]},
            ],
            'dbu_per_user_unit': 1000,
        }

        proposals_engine = RepairProposals()
        res = proposals_engine.run(
            task=task,
            rule_evidence=rule_evidence,
            geometry=geometry,
            layout=layout,
            candidate_digest='digest_rev1',
        )

        # Check measurement
        meas = res['measurement']
        assert meas['status'] == 'fail'
        assert meas['measured_enclosure_dbu'] == 18
        assert meas['required_enclosure_dbu'] == 25
        assert meas['deficit_dbu'] == 7
        assert meas['units'] == 'dbu'
        assert 'left' in meas['contributing_edges']
        assert meas['candidate_digest'] == 'digest_rev1'

        # Check proposal
        assert len(res['proposals']) == 1
        prop = res['proposals'][0]
        assert prop['repair_id'].startswith('repair-')
        assert prop['target_polygon_id'] == 'ROM_MCELL0:poly:1'
        assert prop['edge'] == 'left'
        assert prop['delta_dbu'] == 7
        assert prop['before_points_dbu'] == [[982, 950], [1550, 950], [1550, 1550], [982, 1550]]
        # Expanding left edge by 7 DBU moves x from 982 to 975 (1000 - 25 = 975)
        assert prop['after_points_dbu'] == [[975, 950], [1550, 950], [1550, 1550], [975, 1550]]
        assert prop['after_points_user'] == [[0.975, 0.950], [1.550, 0.950], [1.550, 1.550], [0.975, 1.550]]
        assert prop['post_measurement']['measured_enclosure_dbu'] == 25
        assert prop['post_measurement']['status'] == 'pass'

    def test_ct_r_11_does_not_assert_full_pass_or_propose_without_gt_aa_and_exclusions(self):
        """Assert CT_R_11 with only M1 coverage returns unknown/unsupported and does not claim full rule pass."""
        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('ROM_MCELL0')
        p_ct = gdstk.Polygon([(1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (1.0, 1.5)], layer=50, datatype=0)
        p_m1 = gdstk.Polygon([(0.8, 0.8), (1.7, 0.8), (1.7, 1.7), (0.8, 1.7)], layer=61, datatype=0)
        cell.add(p_ct, p_m1)
        pids = PolygonIds().run(lib)
        layout = SimpleNamespace(
            library=lib,
            polygon_ids=pids,
            top_cell='ROM_MCELL0',
            get_cell=Mock(return_value=cell),
            gds_data={'top_cell': 'ROM_MCELL0'},
        )

        task = {'rule': 'CT_R_11', 'violation_id': 1, 'occurrence': 0, 'task_id': 'task-1'}
        rule_evidence = {
            'rule': 'CT_R_11',
            'supported': True,
            'rule_text': '((CT NOT (M1 AND (GT OR AA))) NOT INSIDE INST) NOT INSIDE EXCLU',
            'layer_mapping': {'CT': {'layer': 50, 'datatype': 0}, 'M1': {'layer': 61, 'datatype': 0}},
        }
        geometry = {
            'status': 'matched',
            'matched_polygons': [
                {'layer_name': 'CT', 'polygon_id': 'ROM_MCELL0:poly:0', 'points_dbu': [[1000, 1000], [1500, 1000], [1500, 1500], [1000, 1500]]},
                {'layer_name': 'M1', 'polygon_id': 'ROM_MCELL0:poly:1', 'points_dbu': [[800, 800], [1700, 800], [1700, 1700], [800, 1700]]},
            ],
            'dbu_per_user_unit': 1000,
        }

        res = RepairProposals().run(task, rule_evidence, geometry, layout, candidate_digest='d1')
        assert res['measurement']['status'] == 'unknown'
        assert 'cannot pass full rule' in res['measurement']['reason'] or 'unproven' in res['measurement']['reason']
        assert res['proposals'] == []

    def test_empty_or_ambiguous_geometry_returns_unknown(self):
        """Assert ambiguous or empty geometry candidate produces unknown measurement and zero proposals."""
        task = {'rule': 'CT_EN_7a_7b', 'violation_id': 1, 'occurrence': 0, 'task_id': 'task-1'}
        rule_evidence = {'rule': 'CT_EN_7a_7b', 'supported': True}
        geometry = {'status': 'ambiguous', 'reason': 'Multiple polygons found', 'matched_polygons': []}

        res = RepairProposals().run(task, rule_evidence, geometry, layout=None, candidate_digest='d1')
        assert res['measurement']['status'] == 'unknown'
        assert res['proposals'] == []

    def test_measure_violation_and_propose_polygon_fix_adapters(self):
        """Assert measure_violation and propose_polygon_fix adapters return ToolResult."""
        scale = 1000.0
        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('ROM_MCELL0')
        p_ct = gdstk.Polygon([(1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (1.0, 1.5)], layer=50, datatype=0)
        p_m1 = gdstk.Polygon([(0.982, 0.950), (1.550, 0.950), (1.550, 1.550), (0.982, 1.550)], layer=61, datatype=0)
        cell.add(p_ct, p_m1)
        pids = PolygonIds().run(lib)
        layout = SimpleNamespace(
            library=lib,
            polygon_ids=pids,
            top_cell='ROM_MCELL0',
            get_cell=Mock(return_value=cell),
            gds_data={'top_cell': 'ROM_MCELL0'},
        )

        checklist_data = {
            'tasks': [
                {
                    'task_id': 'task-1',
                    'rule': 'CT_EN_SUPPORTED',
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
            'digest': 'digest_abc',
        }
        report = {
            'top_cell': 'ROM_MCELL0',
            'dbu_per_user_unit': 1000,
            'source': 'test.drc.results',
            'rules': [
                {
                    'name': 'CT_EN_SUPPORTED',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 101, 'type': 'p', 'points_dbu': [[1000, 1000], [1500, 1500]]}],
                }
            ],
        }
        context = SimpleNamespace(
            state={
                'checklist': checklist_data,
                'report': report,
                'candidate_digest': 'cand_123',
                'candidate_path': '/path/cand.gds',
            },
            services=SimpleNamespace(
                drc_report=report,
                layout=layout,
                drc_files=[],
                layer_maps=[],
                config={'drc_files': [], 'layer_maps': []},
            ),
        )

        meas_res = measure_violation(context, task_id='task-1')
        assert isinstance(meas_res, ToolResult)
        assert 'status' in meas_res.payload

        prop_res = propose_polygon_fix(context, task_id='task-1')
        assert isinstance(prop_res, ToolResult)
        assert 'proposals' in prop_res.payload
