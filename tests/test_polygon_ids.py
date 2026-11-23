import json
from pathlib import Path

import gdstk
import pytest

from FAGENT.capabilities.layout.input_loader import InputLoader
from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.capabilities.layout.polygon_ids import PolygonIds


def _make_sample_gds(gds_path: Path):
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    sub = lib.new_cell('sub_cell')
    sub.add(gdstk.rectangle((0, 0), (1, 1), layer=2, datatype=0))

    top = lib.new_cell('top_cell')
    top.add(gdstk.rectangle((0, 0), (2, 2), layer=1, datatype=0))
    top.add(gdstk.rectangle((5, 5), (7, 7), layer=1, datatype=0))
    # Identical polygons in top_cell to test ambiguous source matching
    top.add(gdstk.rectangle((10, 10), (11, 11), layer=3, datatype=0))
    top.add(gdstk.rectangle((10, 10), (11, 11), layer=3, datatype=0))
    # Path and reference (not direct editable polygons)
    top.add(gdstk.FlexPath([(0, 10), (5, 10)], 0.5, layer=4, datatype=0, simple_path=True))
    top.add(gdstk.Reference(sub, origin=(20, 20)))
    lib.write_gds(str(gds_path))
    return lib


def test_stable_ids_across_edits_and_restore(tmp_path):
    gds_path = tmp_path / 'sample.gds'
    drc_path = tmp_path / 'sample.drc.results'
    map_path = tmp_path / 'layers.map'
    rundir = tmp_path / 'runs' / 'r1' / 'rundir'
    _make_sample_gds(gds_path)
    drc_path.write_text('top_cell 1000\nR1\n1 1 1 Nov 5 12:30:30 2026\nDesc\np 1 3\n0 0\n1000 0\n1000 1000\n')
    map_path.write_text('1 0 M1 drawing\n2 0 M2 drawing\n3 0 M3 drawing\n4 0 M4 drawing\n')

    InputLoader(gds_path=gds_path, drc_path=drc_path, output_dir=rundir, layer_map_files=[map_path], verbose=False).run()
    gds_json = json.loads((rundir / 'gds.json').read_text())
    top_poly_entries = gds_json['database']['structs']['top_cell']['poly']

    # Direct unique polygons have deterministic IDs; identical ones are marked ambiguous/uneditable;
    # path-derived entries are marked kind='path' and editable=False.
    unique_direct = [e for e in top_poly_entries if e.get('layer') == 1]
    assert len(unique_direct) == 2
    assert all(e.get('editable') is True and isinstance(e.get('id'), str) for e in unique_direct)

    identical_direct = [e for e in top_poly_entries if e.get('layer') == 3]
    assert len(identical_direct) == 2
    assert all(e.get('editable') is False and 'id' not in e and e.get('match_status') == 'ambiguous' for e in identical_direct)

    path_entries = [e for e in top_poly_entries if e.get('layer') == 4]
    assert len(path_entries) == 1
    assert path_entries[0]['kind'] == 'path'
    assert path_entries[0]['editable'] is False
    assert 'id' not in path_entries[0]

    layout = LayoutService(rundir, verbose=False).run()
    top_read = layout.read_gds('top_cell')
    sub_read = layout.read_gds('sub_cell')
    assert len(top_read['polygons']) == 4
    assert all(p['kind'] == 'polygon' and p['editable'] is True and isinstance(p['id'], str) for p in top_read['polygons'])
    assert len(top_read['paths']) == 1 and top_read['paths'][0]['editable'] is False
    assert len(top_read['references']) == 1 and top_read['references'][0]['editable'] is False

    ids_before = [p['id'] for p in top_read['polygons']]
    first_id = ids_before[0]
    second_id = ids_before[1]
    sub_id = sub_read['polygons'][0]['id']

    # Replace first polygon; gdstk moves it to the end of cell.polygons, but its ID and all other IDs stay bound!
    res = layout.replace_polygon('top_cell', first_id, [[0, 0], [3, 0], [3, 3], [0, 3]])
    assert res['polygon_id'] == first_id
    top_after = layout.read_gds('top_cell')
    by_id = {p['id']: p for p in top_after['polygons']}
    assert set(by_id.keys()) == set(ids_before)
    assert by_id[first_id]['points'] == [[0, 0], [3, 0], [3, 3], [0, 3]]
    assert by_id[second_id]['points'] == [[5, 5], [7, 5], [7, 7], [5, 7]]

    # Cross-cell and unknown IDs fail closed
    with pytest.raises(ValueError):
        layout.replace_polygon('sub_cell', first_id, [[0, 0], [1, 0], [1, 1]])
    with pytest.raises(ValueError):
        layout.replace_polygon('top_cell', sub_id, [[0, 0], [1, 0], [1, 1]])
    with pytest.raises(ValueError):
        layout.replace_polygon('top_cell', 'nonexistent-id', [[0, 0], [1, 0], [1, 1]])

    # Save candidate + polygon_ids map, reload into fresh library, and verify restoration
    saved = layout.save_gds()
    candidate = Path(saved['candidate'])
    digest = 'abc123'
    map_file = rundir / 'polygon_ids.json'
    layout.polygon_ids.save(map_file, digest)

    reloaded_lib = gdstk.read_gds(str(candidate))
    restored = PolygonIds().restore(map_file, reloaded_lib, digest)
    assert restored.resolve('top_cell', first_id).points.tolist() == [[0, 0], [3, 0], [3, 3], [0, 3]]
    assert restored.resolve('top_cell', second_id).points.tolist() == [[5, 5], [7, 5], [7, 7], [5, 7]]

    # Wrong digest or tampered map fails closed
    with pytest.raises(ValueError):
        PolygonIds().restore(map_file, reloaded_lib, 'wrong-digest')

    tampered = json.loads(map_file.read_text())
    tampered['cells']['top_cell'][0]['id'] = tampered['cells']['top_cell'][1]['id']
    map_file.write_text(json.dumps(tampered))
    with pytest.raises(ValueError):
        PolygonIds().restore(map_file, reloaded_lib, digest)
