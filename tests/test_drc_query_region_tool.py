"""Tests for the query_layout_region DRC tool, schema, and workflow registration."""

# === SECTION 1: Imports ===
import json
from pathlib import Path

import gdstk
import jsonschema
import pytest
import yaml

from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.engine.task_contracts import TaskContext, ToolResult
from FAGENT.tasks.drc import tools as drc_tools


# === SECTION 2: Module-Level Helpers ===
class QueryToolServices:
    """Minimal services double providing a real LayoutService."""

    def run(self):
        return self

    def __init__(self, layout: LayoutService):
        self.layout = layout


def make_layout_service(tmp_path: Path, with_ref: bool = False) -> LayoutService:
    """Create a real temporary GDS and initialized LayoutService."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    top = lib.new_cell('TOP_CELL')
    top.add(gdstk.rectangle((0.95, 1.95), (1.05, 2.05), layer=15, datatype=0))
    top.add(gdstk.rectangle((1.50, 2.50), (1.70, 2.70), layer=15, datatype=0))
    top.add(gdstk.rectangle((1.40, 2.40), (1.80, 2.80), layer=16, datatype=0))

    if with_ref:
        sub = lib.new_cell('SUB_CELL')
        sub.add(gdstk.rectangle((0.0, 0.0), (0.2, 0.2), layer=16, datatype=0))
        top.add(gdstk.Reference(sub, origin=(1.5, 2.5)))

    gds_path = tmp_path / 'sample.gds'
    lib.write_gds(str(gds_path))
    (tmp_path / 'gds.json').write_text(json.dumps({'source': str(gds_path), 'top_cell': 'TOP_CELL'}))
    (tmp_path / 'drc.json').write_text(json.dumps({'rules': []}))
    service = LayoutService(rundir=tmp_path, verbose=False).run()
    service.load_library()
    return service


# === SECTION 3: Tests ===
def test_query_layout_region_expands_region_with_layer_filter_and_digest_binding(tmp_path: Path):
    """query_layout_region returns larger region polygons, candidate digest, and empty updates."""
    layout = make_layout_service(tmp_path)
    ctx = TaskContext('drc', {}, {'candidate_digest': 'sha-live-123'}, QueryToolServices(layout))

    res = drc_tools.query_layout_region(ctx, 'TOP_CELL', [0.9, 1.9, 1.9, 2.9])
    assert isinstance(res, ToolResult)
    assert res.updates == {}
    assert res.payload['candidate_digest'] == 'sha-live-123'
    assert res.payload['complete'] is True
    assert len(res.payload['polygons']) == 3

    filtered = drc_tools.query_layout_region(
        ctx,
        'TOP_CELL',
        [0.9, 1.9, 1.9, 2.9],
        layers=[{'layer': 15, 'datatype': 0}],
    )
    assert len(filtered.payload['polygons']) == 2
    assert all(p['layer'] == 15 and p['datatype'] == 0 for p in filtered.payload['polygons'])


def test_query_layout_region_rejects_malformed_bounds_and_unknown_cell(tmp_path: Path):
    """Malformed bounds and unknown cells fail fast."""
    layout = make_layout_service(tmp_path)
    ctx = TaskContext('drc', {}, {'candidate_digest': 'sha-live-123'}, QueryToolServices(layout))

    with pytest.raises(ValueError):
        drc_tools.query_layout_region(ctx, 'TOP_CELL', [2.0, 0.0, 1.0, 1.0])

    with pytest.raises(ValueError):
        drc_tools.query_layout_region(ctx, 'MISSING_CELL', [0.0, 0.0, 1.0, 1.0])


def test_query_layout_region_surfaces_overlapping_hierarchy_as_incomplete(tmp_path: Path):
    """Overlapping references in the queried region mark complete=False."""
    layout = make_layout_service(tmp_path, with_ref=True)
    ctx = TaskContext('drc', {}, {'candidate_digest': 'sha-live-456'}, QueryToolServices(layout))

    res = drc_tools.query_layout_region(ctx, 'TOP_CELL', [1.4, 2.4, 1.8, 2.8])
    assert res.payload['complete'] is False
    assert res.payload['candidate_digest'] == 'sha-live-456'
    assert len(res.payload['references_in_region']) == 1


def test_query_layout_region_schema_and_workflow_enabled():
    """ToolSpec has strict schema and workflow.yaml enables tools:query_layout_region."""
    spec = drc_tools.query_layout_region.task_tool
    assert spec.parameters['additionalProperties'] is False
    jsonschema.Draft202012Validator(spec.parameters).validate({
        'cell_name': 'TOP_CELL',
        'bounds_user': [0.0, 0.0, 1.0, 1.0],
        'layers': [{'layer': 15, 'datatype': 0}],
    })
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(spec.parameters).validate({
            'cell_name': 'TOP_CELL',
            'bounds_user': [0.0, 0.0, 1.0],
        })

    wf = yaml.safe_load(Path('FAGENT/tasks/drc/workflow.yaml').read_text(encoding='utf-8'))
    repair_node = next(n for n in wf['nodes'] if n['id'] == 'repair')
    assert 'tools:query_layout_region' in repair_node['tools']
