# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from FAGENT.engine.graph_services import GraphServices

# === SECTION 2: Test Suite Class ===


class TestCompactDrcContext:
    """Test suite verifying compact DRC context and markers retention."""

    def test_compact_context_keeps_markers_and_constraints_without_gds_payload(self, tmp_path: Path):
        constraints = ['Minimize width while preserving design intent.']
        constraint_file = tmp_path / 'constraints.json'
        constraint_file.write_text(json.dumps(constraints))
        marker = {'type': 'p', 'id': 7, 'points_dbu': [[0, 0], [12, 3]]}
        report = {'top_cell': 'cell', 'dbu_per_user_unit': 1000, 'rules': [
            {'name': 'RULE_A', 'description': ['width rule'], 'reported_count': 1, 'total_count': 1, 'geometry': [marker], 'extra': 'unused'},
            {'name': 'CLEAN', 'reported_count': 0, 'total_count': 0, 'geometry': []}], 'source': 'large-report'}
        services = GraphServices.__new__(GraphServices)
        services.layout = SimpleNamespace(read_drc=Mock(return_value=report), gds_data={'large': 'layout'})
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[{'rule': 'RULE_A', 'content': 'reference'}]))
        services.config = {'goal': 'Repair', 'rag_limit': 2, 'constraints_file': str(constraint_file)}
        result = services.build_drc_context()
        assert 'gds' not in result and result['drc']['top_cell'] == 'cell'
        assert result['drc']['rules'][0]['geometry'] == [marker]
        assert len(result['drc']['rules']) == 1 and 'extra' not in result['drc']['rules'][0]
        assert result['knowledge']['constraints'] == constraints
        assert result['knowledge']['references'][0]['rule'] == 'RULE_A'
        assert constraints[0] in result['goal']
        assert services.rag.retrieve.call_count == 1
        assert report['rules'][0]['extra'] == 'unused'

    def test_missing_marker_geometry_is_retained_for_checklist(self, tmp_path: Path):
        constraint_file = tmp_path / 'constraints.json'
        constraint_file.write_text('[]')
        report = {'rules': [{'name': 'RULE_B', 'reported_count': 3, 'total_count': 3, 'geometry': []}]}
        services = GraphServices.__new__(GraphServices)
        services.layout = SimpleNamespace(read_drc=Mock(return_value=report))
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[]))
        services.config = {'goal': 'repair', 'rag_limit': 1, 'constraints_file': str(constraint_file)}
        context = services.build_drc_context()
        assert context['drc']['rules'][0]['geometry'] == []
        assert context['drc']['rules'][0]['reported_count'] == 3
        assert services.drc_report == report
