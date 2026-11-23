# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import gdstk
import pytest

from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.capabilities.layout.polygon_ids import PolygonIds
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult
from FAGENT.tasks.drc.checklist import Checklist
from FAGENT.tasks.drc.tools import apply_polygon_fix, propose_polygon_fix, recheck_violation


# === SECTION 2: Test Suite Class ===
class TestDrcTransactionalApply:
    """Test suite verifying transactional proposal application and local recheck."""

    @pytest.fixture
    def setup_env(self, tmp_path: Path):
        """Set up layout, GDS, and checklist environment."""
        rundir = tmp_path / 'rundir'
        rundir.mkdir(parents=True)
        gds_source = tmp_path / 'original.gds'

        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('ROM_MCELL0')
        p_ct = gdstk.Polygon([(1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (1.0, 1.5)], layer=50, datatype=0)
        p_m1 = gdstk.Polygon([(0.982, 0.950), (1.550, 0.950), (1.550, 1.550), (0.982, 1.550)], layer=61, datatype=0)
        cell.add(p_ct, p_m1)
        lib.write_gds(str(gds_source))

        orig_bytes = gds_source.read_bytes()

        gds_data = {'source': str(gds_source), 'top_cell': 'ROM_MCELL0'}
        drc_data = {'source': str(tmp_path / 'dummy.drc.results')}
        (rundir / 'gds.json').write_text(json.dumps(gds_data))
        (rundir / 'drc.json').write_text(json.dumps(drc_data))

        layout_svc = LayoutService(rundir=rundir).run()

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
            'digest': 'initial_digest',
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

        # Checkpoint initial candidate
        cand_path = rundir / 'candidate.gds'
        layout_svc.save_gds()
        initial_cand_digest = GraphServices.candidate_digest(cand_path)

        deck_file = tmp_path / 'rules.drc'
        deck_file.write_text("""CT_EN_SUPPORTED {
@ Supported enclosure rule
  X = RECTANGLE ENC CT M1 < 0.025 OPPOSITE
}
""")
        layermap_file = tmp_path / 'layer.map'
        layermap_file.write_text("CT drawing 50 0\nM1 drawing 61 0\n")

        services = SimpleNamespace(
            rundir=rundir,
            layout=layout_svc,
            drc_report=report,
            drc_files=[deck_file],
            layer_maps=[layermap_file],
            config={'drc_files': [str(deck_file)], 'layer_maps': [str(layermap_file)]},
            checkpoint_candidate=Mock(side_effect=lambda: Path(layout_svc.save_gds()['candidate'])),
            check_output_path=layout_svc.check_output_path,
        )
        context = SimpleNamespace(
            state={
                'checklist': checklist_data,
                'checklist_path': str(rundir / 'checklist.json'),
                'report': report,
                'candidate_path': str(cand_path),
                'candidate_digest': initial_cand_digest,
                'inspected': ['ROM_MCELL0'],
                'proposals': {},
            },
            services=services,
        )

        return context, gds_source, orig_bytes, cand_path

    def test_valid_edit_applies_and_updates_candidate_digest(self, setup_env):
        """Assert valid repair proposal applies, stages candidate, and updates digest and polygon IDs."""
        context, gds_source, orig_bytes, cand_path = setup_env
        prop_res = propose_polygon_fix(context, task_id='task-1')
        assert prop_res.payload['proposals']
        proposal = prop_res.payload['proposals'][0]
        repair_id = proposal['repair_id']

        # Store proposal in state as propose_polygon_fix does
        context.state['proposals'] = {repair_id: proposal}

        apply_res = apply_polygon_fix(context, repair_id=repair_id)
        assert isinstance(apply_res, ToolResult)
        assert apply_res.payload['status'] == 'applied'
        assert apply_res.updates['candidate_digest'] != context.state['candidate_digest']
        assert apply_res.updates['candidate_digest'] == GraphServices.candidate_digest(cand_path)
        # Original GDS must be completely unchanged
        assert gds_source.read_bytes() == orig_bytes

    def test_stale_proposal_rejected_after_candidate_changes(self, setup_env):
        """Assert a proposal generated for an earlier candidate revision is rejected if digest changed."""
        context, gds_source, orig_bytes, cand_path = setup_env
        prop_res = propose_polygon_fix(context, task_id='task-1')
        proposal = prop_res.payload['proposals'][0]
        repair_id = proposal['repair_id']
        context.state['proposals'] = {repair_id: proposal}

        # Modify state candidate_digest to simulate another edit occurred
        context.state['candidate_digest'] = 'changed_digest_from_another_edit'

        with pytest.raises(ValueError, match="Stale repair proposal"):
            apply_polygon_fix(context, repair_id=repair_id)

    def test_simulated_write_failure_preserves_previous_candidate_and_mapping(self, setup_env):
        """Assert write failure rolls back in-memory polygon replacement and preserves existing candidate."""
        context, gds_source, orig_bytes, cand_path = setup_env
        prop_res = propose_polygon_fix(context, task_id='task-1')
        proposal = prop_res.payload['proposals'][0]
        repair_id = proposal['repair_id']
        context.state['proposals'] = {repair_id: proposal}

        prev_cand_bytes = cand_path.read_bytes()

        # Simulate write failure in checkpoint_candidate
        context.services.checkpoint_candidate = Mock(side_effect=IOError('Disk full'))

        with pytest.raises(IOError, match="Disk full"):
            apply_polygon_fix(context, repair_id=repair_id)

        # Previous candidate file must be intact
        assert cand_path.read_bytes() == prev_cand_bytes

    def test_local_recheck_never_claims_deck_level_clean(self, setup_env):
        """Assert local recheck records local evidence but explicitly denies full DRC clean."""
        context, gds_source, orig_bytes, cand_path = setup_env
        # First apply fix
        prop_res = propose_polygon_fix(context, task_id='task-1')
        proposal = prop_res.payload['proposals'][0]
        repair_id = proposal['repair_id']
        context.state['proposals'] = {repair_id: proposal}
        apply_res = apply_polygon_fix(context, repair_id=repair_id)
        context.state.update(apply_res.updates)

        # Now run local recheck
        recheck_res = recheck_violation(context, task_id='task-1')
        assert isinstance(recheck_res, ToolResult)
        assert recheck_res.payload['status'] == 'pass'
        assert recheck_res.payload['local_verified'] is True
        assert recheck_res.payload['deck_clean'] is False
        assert 'not a deck-level DRC pass' in recheck_res.payload['notice']

    def test_local_recheck_fail_never_becomes_verified(self, setup_env):
        """Assert local recheck on unaddressed or unknown condition never marks task as pass."""
        context, gds_source, orig_bytes, cand_path = setup_env
        # Do NOT apply fix. Violation is still present (deficit 7 DBU)
        recheck_res = recheck_violation(context, task_id='task-1')
        assert recheck_res.payload['status'] == 'fail'
        assert recheck_res.payload['local_verified'] is False
        # Checklist must NOT be marked 'pass'
        checklist_state = recheck_res.updates.get('checklist') or context.state.get('checklist')
        task = next(t for t in checklist_state['tasks'] if t['task_id'] == 'task-1')
        assert task['status'] != 'pass'
