# === SECTION 1: Imports ===
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import gdstk
import pytest

from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.capabilities.layout.polygon_ids import PolygonIds
from FAGENT.capabilities.layout.violation_geometry import ViolationGeometry
from FAGENT.engine.task_contracts import ToolResult
from FAGENT.tasks.drc.checklist import Checklist
from FAGENT.tasks.drc.rule_evidence import RuleEvidence
from FAGENT.tasks.drc.tools import DrcTools, find_related_polygons, find_rule


# === SECTION 2: Test Suite Class ===
class TestDrcGeometryEvidence:
    """Test suite verifying rule extraction, layer map provenance, and polygon evidence."""

    @pytest.fixture
    def sample_deck_and_maps(self, tmp_path: Path):
        """Create representative DRC deck and layer mapping files."""
        deck_file = tmp_path / 'rules.drc'
        deck_content = """// Sample Deck
LAYER CT 1024
LAYER M1 61
LAYER GT 100

CT_EN_7a_7b {
@ CT_EN_7a: M1 enclosure of CT >= 0.025um
  X = RECTANGLE ENC CT M1 < 0.025 OPPOSITE
  (CT NOT M1) NOT INSIDE EXCLU
}

CT_R_11 {
@ CT must be fully covered by M1 and (AA OR GT)
  ((CT NOT (M1 AND (GT OR AA))) NOT INSIDE INST) NOT INSIDE EXCLU
}

UNKNOWN_DERIVED_RULE {
@ Check derived layers without physical map
  DERIVED_A NOT DERIVED_B
}
"""
        deck_file.write_text(deck_content)

        layermap_file = tmp_path / 'sample.layermap'
        layermap_content = """CT drawing 50 0
M1 drawing 61 0
GT drawing 100 0
"""
        layermap_file.write_text(layermap_content)

        return deck_file, layermap_file

    def test_exact_rule_and_source_location_retrieved_from_deck(self, sample_deck_and_maps):
        """Assert exact rule name, line numbers, and raw text are extracted from deck."""
        deck_file, layermap_file = sample_deck_and_maps
        task = {'rule': 'CT_EN_7a_7b', 'violation_id': 1, 'occurrence': 0}
        evidence = RuleEvidence().run(
            task=task,
            deck_paths=[deck_file],
            layer_maps=[layermap_file],
            references=[{'rule': 'CT_EN_7a_7b', 'content': 'RAG ref'}],
        )
        assert evidence['found'] is True
        assert evidence['rule'] == 'CT_EN_7a_7b'
        assert evidence['line_start'] == 6
        assert evidence['line_end'] == 10
        assert 'CT_EN_7a' in evidence['rule_text']
        assert evidence['deck_file'] == str(deck_file.resolve())
        assert evidence['references'] == [{'rule': 'CT_EN_7a_7b', 'content': 'RAG ref'}]

    def test_ct_m1_layer_mapping_is_physical_layer_specific(self, sample_deck_and_maps):
        """Assert CT and M1 layers resolve to specific physical layer numbers and datatypes."""
        deck_file, layermap_file = sample_deck_and_maps
        task = {'rule': 'CT_EN_7a_7b', 'violation_id': 1, 'occurrence': 0}
        evidence = RuleEvidence().run(
            task=task,
            deck_paths=[deck_file],
            layer_maps=[layermap_file],
        )
        mapping = evidence['layer_mapping']
        assert mapping['CT'] == {'layer': 50, 'datatype': 0}
        assert mapping['M1'] == {'layer': 61, 'datatype': 0}

    def test_real_workspace_deck_rule_extraction(self):
        """Assert real workspace DRC deck can extract CT_EN_7a_7b and CT_R_11 definitions."""
        deck_path = Path('DRC/1P7M_6Ic_1TKMc_ALPA2.drc')
        layermap_path = Path('config/smec55.layermap')
        if not deck_path.is_file() or not layermap_path.is_file():
            pytest.skip('Workspace DRC deck or layermap not available')

        task = {'rule': 'CT_R_11', 'violation_id': 1, 'occurrence': 0}
        evidence = RuleEvidence().run(
            task=task,
            deck_paths=[deck_path],
            layer_maps=[layermap_path],
        )
        assert evidence['found'] is True
        assert evidence['rule'] == 'CT_R_11'
        assert 'CT must be fully covered' in evidence['rule_text']
        assert evidence['line_start'] == 2680
        assert evidence['line_end'] == 2683
        assert evidence['layer_mapping']['CT'] == {'layer': 50, 'datatype': 0}
        assert evidence['layer_mapping']['M1'] == {'layer': 61, 'datatype': 0}

    def test_marker_matched_to_one_polygon_reports_cell_id_and_dbu_geometry(self, tmp_path: Path):
        """Assert marker overlapping exactly one polygon reports cell, stable ID, DBU points, and evidence."""
        # Create small test GDS with 1000 DBU per user unit (unit=1e-6, precision=1e-9)
        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('TEST_CELL')
        # Add CT polygon: [1.0, 1.0] to [1.5, 1.5] -> DBU [1000, 1000] to [1500, 1500]
        p_ct = gdstk.Polygon([(1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (1.0, 1.5)], layer=50, datatype=0)
        # Add M1 polygon: [0.8, 0.8] to [1.7, 1.7] -> DBU [800, 800] to [1700, 1700]
        p_m1 = gdstk.Polygon([(0.8, 0.8), (1.7, 0.8), (1.7, 1.7), (0.8, 1.7)], layer=61, datatype=0)
        cell.add(p_ct, p_m1)

        pids = PolygonIds().run(lib)
        layout = SimpleNamespace(
            library=lib,
            polygon_ids=pids,
            top_cell='TEST_CELL',
            get_cell=Mock(return_value=cell),
            gds_data={'top_cell': 'TEST_CELL'},
        )

        rule_evidence = {
            'rule': 'CT_EN_7a_7b',
            'supported': True,
            'layer_mapping': {'CT': {'layer': 50, 'datatype': 0}, 'M1': {'layer': 61, 'datatype': 0}},
        }
        # Marker inside CT polygon in DBU
        task = {
            'rule': 'CT_EN_7a_7b',
            'violation_id': 1,
            'occurrence': 0,
            'violation': {'id': 1, 'type': 'p', 'points_dbu': [[1100, 1100], [1400, 1400]]},
        }

        geom_res = ViolationGeometry().run(task, rule_evidence, layout, candidate_digest='digest123')
        assert geom_res['status'] == 'matched'
        assert geom_res['cell'] == 'TEST_CELL'
        assert geom_res['candidate_digest'] == 'digest123'
        matched = geom_res['matched_polygons']
        assert len(matched) == 2  # one CT, one M1
        ct_match = next(m for m in matched if m['layer_name'] == 'CT')
        assert ct_match['polygon_id'] == 'TEST_CELL:poly:0'
        assert ct_match['layer'] == 50
        assert ct_match['points_dbu'] == [[1000, 1000], [1500, 1000], [1500, 1500], [1000, 1500]]

    def test_multiple_polygon_matches_remain_ambiguous(self):
        """Assert multiple candidate polygons overlapping the marker region return ambiguous without choosing one."""
        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('TEST_CELL')
        # Add two CT polygons overlapping the marker
        p1 = gdstk.Polygon([(1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (1.0, 1.5)], layer=50, datatype=0)
        p2 = gdstk.Polygon([(1.2, 1.2), (1.6, 1.2), (1.6, 1.6), (1.2, 1.6)], layer=50, datatype=0)
        cell.add(p1, p2)

        pids = PolygonIds().run(lib)
        layout = SimpleNamespace(
            library=lib,
            polygon_ids=pids,
            top_cell='TEST_CELL',
            get_cell=Mock(return_value=cell),
            gds_data={'top_cell': 'TEST_CELL'},
        )

        rule_evidence = {
            'rule': 'CT_EN_7a_7b',
            'supported': True,
            'layer_mapping': {'CT': {'layer': 50, 'datatype': 0}},
        }
        task = {
            'rule': 'CT_EN_7a_7b',
            'violation_id': 1,
            'occurrence': 0,
            'violation': {'id': 1, 'type': 'p', 'points_dbu': [[1300, 1300], [1400, 1400]]},
        }

        geom_res = ViolationGeometry().run(task, rule_evidence, layout)
        assert geom_res['status'] == 'ambiguous'
        assert 'Multiple polygon candidates' in geom_res['reason']
        assert len(geom_res.get('matched_polygons', [])) == 0

    def test_unknown_layer_mapping_or_derived_semantics_returns_unsupported(self, sample_deck_and_maps):
        """Assert derived layers or unmapped physical layers produce unsupported without selecting a polygon."""
        deck_file, layermap_file = sample_deck_and_maps
        task = {'rule': 'UNKNOWN_DERIVED_RULE', 'violation_id': 9, 'occurrence': 0}
        evidence = RuleEvidence().run(
            task=task,
            deck_paths=[deck_file],
            layer_maps=[layermap_file],
        )
        assert evidence['supported'] is False
        assert 'Derived or unknown' in evidence['reason']

        # And ViolationGeometry rejects it as unsupported
        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('TEST_CELL')
        layout = SimpleNamespace(
            library=lib,
            polygon_ids=PolygonIds().run(lib),
            top_cell='TEST_CELL',
            get_cell=Mock(return_value=cell),
            gds_data={'top_cell': 'TEST_CELL'},
        )
        geom_res = ViolationGeometry().run(task, evidence, layout)
        assert geom_res['status'] == 'unsupported'

    def test_find_rule_and_find_related_polygons_tool_adapters(self, sample_deck_and_maps):
        """Assert find_rule and find_related_polygons tool adapters return ToolResult."""
        deck_file, layermap_file = sample_deck_and_maps
        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('ROM_MCELL0')
        p_ct = gdstk.Polygon([(1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (1.0, 1.5)], layer=50, datatype=0)
        p_m1 = gdstk.Polygon([(0.8, 0.8), (1.7, 0.8), (1.7, 1.7), (0.8, 1.7)], layer=61, datatype=0)
        cell.add(p_ct, p_m1)

        layout = SimpleNamespace(
            library=lib,
            polygon_ids=PolygonIds().run(lib),
            top_cell='ROM_MCELL0',
            get_cell=Mock(return_value=cell),
            gds_data={'top_cell': 'ROM_MCELL0'},
        )

        checklist_data = {
            'tasks': [
                {
                    'task_id': 'task-1',
                    'rule': 'CT_EN_7a_7b',
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
                    'name': 'CT_EN_7a_7b',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 101, 'type': 'p', 'points_dbu': [[1100, 1100], [1400, 1400]]}],
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
                drc_files=[deck_file],
                layer_maps=[layermap_file],
                config={'drc_files': [str(deck_file)], 'layer_maps': [str(layermap_file)]},
            ),
        )

        rule_res = find_rule(context, task_id='task-1')
        assert isinstance(rule_res, ToolResult)
        assert rule_res.payload['rule'] == 'CT_EN_7a_7b'

        poly_res = find_related_polygons(context, task_id='task-1')
        assert isinstance(poly_res, ToolResult)
        assert poly_res.payload['status'] == 'matched'
        assert poly_res.payload['cell'] == 'ROM_MCELL0'
