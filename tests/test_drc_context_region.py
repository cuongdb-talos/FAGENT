"""Tests for ViolationGeometry.query_region bounded geometry queries."""

# === SECTION 1: Imports ===
import json
from pathlib import Path

import gdstk
import pytest

from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.capabilities.layout.violation_geometry import ViolationGeometry


# === SECTION 2: Module-Level Helpers ===
def build_test_layout(tmp_path: Path, with_ref: bool = False, with_path: bool = False) -> LayoutService:
    """Create a real temporary GDS and initialized LayoutService."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    top = lib.new_cell('TOP_CELL')

    # Two CT polygons (layer 15, datatype 0) inside [0.9, 1.9, 1.2, 2.2]
    top.add(gdstk.rectangle((0.95, 1.95), (1.05, 2.05), layer=15, datatype=0))
    top.add(gdstk.rectangle((1.08, 1.95), (1.18, 2.05), layer=15, datatype=0))
    # Unrelated M1 polygon (layer 16, datatype 0) overlapping the region
    top.add(gdstk.rectangle((0.80, 1.90), (1.30, 2.25), layer=16, datatype=0))
    # Far-away polygon outside the region
    top.add(gdstk.rectangle((10.0, 10.0), (11.0, 11.0), layer=15, datatype=0))

    if with_ref:
        sub = lib.new_cell('SUB_CELL')
        sub.add(gdstk.rectangle((0.0, 0.0), (0.2, 0.2), layer=16, datatype=0))
        top.add(gdstk.Reference(sub, origin=(1.0, 2.0)))

    if with_path:
        top.add(gdstk.FlexPath([(0.8, 2.0), (1.3, 2.0)], 0.05, layer=16, datatype=0, simple_path=True))

    gds_path = tmp_path / 'sample.gds'
    lib.write_gds(str(gds_path))

    (tmp_path / 'gds.json').write_text(json.dumps({'source': str(gds_path), 'top_cell': 'TOP_CELL'}))
    (tmp_path / 'drc.json').write_text(json.dumps({'rules': []}))
    service = LayoutService(rundir=tmp_path, verbose=False).run()
    service.load_library()
    return service


class UnknownBboxRef:
    """Reference double whose bounding_box raises an error."""

    def __init__(self, cell_name: str):
        self.cell_name = cell_name
        self.cell = None

    def bounding_box(self):
        raise RuntimeError('Cannot compute bounding box')


class UnknownBboxPath:
    """Path double whose bounding_box returns None."""

    def __init__(self, layer: int, datatype: int):
        self.layers = [layer]
        self.datatypes = [datatype]

    def bounding_box(self):
        return None


class CellDouble:
    """Minimal cell double for testing unknown reference and path bounds."""

    def __init__(self, name: str, references: list, paths: list):
        self.name = name
        self.polygons = []
        self.references = references
        self.paths = paths


class LayoutDouble:
    """Minimal layout double returning CellDouble."""

    def __init__(self, cell: CellDouble):
        self._cell = cell
        self.polygon_ids = None

    def get_cell(self, name: str):
        if name != self._cell.name:
            raise ValueError(f'Unknown cell: {name}')
        return self._cell


# === SECTION 3: Tests ===
def test_query_region_returns_all_overlapping_polygons_and_stable_ids(tmp_path: Path):
    """All overlapping polygons across layers are returned with stable IDs."""
    service = build_test_layout(tmp_path)
    vg = ViolationGeometry()

    res1 = vg.query_region(service, 'TOP_CELL', [0.9, 1.9, 1.2, 2.2], candidate_digest='sha256-abc')
    res2 = vg.query_region(service, 'TOP_CELL', [0.9, 1.9, 1.2, 2.2], candidate_digest='sha256-abc')

    assert res1['cell'] == 'TOP_CELL'
    assert res1['bounds_user'] == [0.9, 1.9, 1.2, 2.2]
    assert res1['candidate_digest'] == 'sha256-abc'
    assert res1['coordinate_units'] == 'user'
    assert res1['complete'] is True
    assert res1['reason'] == ''
    assert res1['references_in_region'] == []
    assert res1['paths_in_region'] == []

    polys = res1['polygons']
    assert len(polys) == 3
    assert [p['id'] for p in polys] == [p['id'] for p in res2['polygons']]
    assert all(p['editable'] is True for p in polys)
    assert sum(1 for p in polys if p['layer'] == 15 and p['datatype'] == 0) == 2
    assert sum(1 for p in polys if p['layer'] == 16 and p['datatype'] == 0) == 1


def test_query_region_layer_filter_and_empty_region(tmp_path: Path):
    """Physical layer filter restricts polygons; empty region returns complete=True and empty list."""
    service = build_test_layout(tmp_path)
    vg = ViolationGeometry()

    filtered = vg.query_region(
        service,
        'TOP_CELL',
        [0.9, 1.9, 1.2, 2.2],
        candidate_digest='sha256-abc',
        layer_specs=[{'layer': 15, 'datatype': 0}],
    )
    assert len(filtered['polygons']) == 2
    assert all(p['layer'] == 15 and p['datatype'] == 0 for p in filtered['polygons'])

    empty = vg.query_region(service, 'TOP_CELL', [50.0, 50.0, 51.0, 51.0], candidate_digest='sha256-abc')
    assert empty['polygons'] == []
    assert empty['complete'] is True
    assert empty['reason'] == ''


@pytest.mark.parametrize(
    'bad_bounds',
    [
        [0.0, 0.0, 1.0],
        [2.0, 0.0, 1.0, 1.0],
        [0.0, 2.0, 1.0, 1.0],
        [0.0, 0.0, float('nan'), 1.0],
        [True, 0.0, 1.0, 1.0],
        'not-a-list',
    ],
)
def test_query_region_rejects_invalid_bounds(tmp_path: Path, bad_bounds):
    """Invalid bounds fail fast with ValueError."""
    service = build_test_layout(tmp_path)
    vg = ViolationGeometry()
    with pytest.raises(ValueError):
        vg.query_region(service, 'TOP_CELL', bad_bounds, candidate_digest=None)


def test_query_region_flags_overlapping_reference_and_path_as_incomplete(tmp_path: Path):
    """Overlapping references and paths mark complete=False with explicit reason."""
    ref_service = build_test_layout(tmp_path / 'ref', with_ref=True)
    vg = ViolationGeometry()
    ref_res = vg.query_region(ref_service, 'TOP_CELL', [0.9, 1.9, 1.2, 2.2], candidate_digest='d1')
    assert ref_res['complete'] is False
    assert ref_res['reason']
    assert len(ref_res['references_in_region']) == 1
    assert ref_res['references_in_region'][0]['cell'] == 'SUB_CELL'

    path_service = build_test_layout(tmp_path / 'path', with_path=True)
    path_res = vg.query_region(path_service, 'TOP_CELL', [0.9, 1.9, 1.2, 2.2], candidate_digest='d2')
    assert path_res['complete'] is False
    assert path_res['reason']
    assert len(path_res['paths_in_region']) == 1


def test_query_region_flags_unknown_reference_and_path_bounds_conservatively():
    """Unknown reference or path bounds are conservatively included and mark complete=False."""
    cell = CellDouble(
        'TOP_CELL',
        references=[UnknownBboxRef('OPAQUE_SUB')],
        paths=[UnknownBboxPath(16, 0)],
    )
    layout = LayoutDouble(cell)
    vg = ViolationGeometry()

    res = vg.query_region(layout, 'TOP_CELL', [0.9, 1.9, 1.2, 2.2], candidate_digest='d3')
    assert res['complete'] is False
    assert res['reason']
    assert len(res['references_in_region']) == 1
    assert res['references_in_region'][0]['bounds_known'] is False
    assert len(res['paths_in_region']) == 1
    assert res['paths_in_region'][0]['bounds_known'] is False
