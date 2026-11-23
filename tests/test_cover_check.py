import math
from pathlib import Path

import gdstk
import pytest

from FAGENT.capabilities.layout.cover_check import CoverCheck
from FAGENT.capabilities.layout.input_loader import InputLoader
from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.engine.task_contracts import TaskContext
from FAGENT.tasks.drc.tools import check_cover


def _rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def test_cover_check_pass_equal_and_touching():
    outer = _rect(0.0, 0.0, 1.0, 1.0)
    # Strictly above threshold: 0.05 >= 0.03
    res = CoverCheck().run(_rect(0.05, 0.05, 0.95, 0.95), outer, minimum=0.03, precision=0.001)
    assert res['status'] == 'pass'
    assert res['measured_distance'] == pytest.approx(0.05, abs=1e-6)
    assert res['violations'] == []
    assert res['units'] == 'gds_user_units'

    # Equal to threshold: 0.03 == 0.03 (with float noise within half precision)
    res_eq = CoverCheck().run(_rect(0.03 - 1e-9, 0.03, 0.97, 0.97), outer, minimum=0.03, precision=0.001)
    assert res_eq['status'] == 'pass'
    assert res_eq['violations'] == []

    # Touching at zero with minimum=0.0
    res_zero = CoverCheck().run(_rect(0.0, 0.2, 0.5, 0.8), outer, minimum=0.0, precision=0.001)
    assert res_zero['status'] == 'pass'
    assert res_zero['measured_distance'] == pytest.approx(0.0, abs=1e-6)

    # Touching at zero with minimum=0.03 -> fail with shortage 0.03
    res_touch_fail = CoverCheck().run(_rect(0.0, 0.2, 0.5, 0.8), outer, minimum=0.03, precision=0.001)
    assert res_touch_fail['status'] == 'fail'
    assert len(res_touch_fail['violations']) >= 1
    assert max(v['shortage'] for v in res_touch_fail['violations']) == pytest.approx(0.03, abs=0.001)


def test_cover_check_inside_and_outside_shortage_examples():
    outer = _rect(0.0, 0.0, 1.0, 1.0)

    # 0.02 um inside with required 0.03 um -> fail, shortage 0.01 um
    res_in = CoverCheck().run(_rect(0.02, 0.1, 0.9, 0.9), outer, minimum=0.03, precision=0.001)
    assert res_in['status'] == 'fail'
    assert res_in['measured_distance'] == pytest.approx(0.02, abs=0.001)
    assert len(res_in['violations']) >= 1
    assert max(v['shortage'] for v in res_in['violations']) == pytest.approx(0.01, abs=0.001)
    assert any(v['outer_segment'] is not None for v in res_in['violations'])

    # 0.02 um outside with required 0.03 um -> fail, shortage 0.05 um
    res_out = CoverCheck().run(_rect(-0.02, 0.1, 0.9, 0.9), outer, minimum=0.03, precision=0.001)
    assert res_out['status'] == 'fail'
    assert max(v['shortage'] for v in res_out['violations']) == pytest.approx(0.05, abs=0.001)

    # Wholly outside: 0.02 um away from outer edge -> max shortage at far edge, min outside >= 0.05
    res_wholly_out = CoverCheck().run(_rect(1.02, 0.1, 1.2, 0.9), outer, minimum=0.03, precision=0.001)
    assert res_wholly_out['status'] == 'fail'
    assert len(res_wholly_out['violations']) >= 1
    assert min(v['shortage'] for v in res_wholly_out['violations']) >= 0.05 - 0.001


def test_cover_check_multiple_edges_concave_and_ambiguous_association():
    # Centered square with insufficient enclosure on all 4 edges -> 4 violations
    outer = _rect(0.0, 0.0, 1.0, 1.0)
    inner = _rect(0.01, 0.01, 0.99, 0.99)
    res_4 = CoverCheck().run(inner, outer, minimum=0.03, precision=0.001)
    assert res_4['status'] == 'fail'
    assert len(res_4['violations']) == 4
    assert all(v['shortage'] == pytest.approx(0.02, abs=0.001) for v in res_4['violations'])

    # Concave L-shaped outer polygon
    l_outer = [[0.0, 0.0], [2.0, 0.0], [2.0, 1.0], [1.0, 1.0], [1.0, 2.0], [0.0, 2.0]]
    l_inner_ok = [[0.1, 0.1], [1.9, 0.1], [1.9, 0.9], [0.9, 0.9], [0.9, 1.9], [0.1, 1.9]]
    assert CoverCheck().run(l_inner_ok, l_outer, minimum=0.05, precision=0.001)['status'] == 'pass'

    # Precision boundary: 1 DBU short (0.029 vs 0.030 at precision 0.001) -> fail
    res_1dbu = CoverCheck().run(_rect(0.029, 0.1, 0.9, 0.9), outer, minimum=0.03, precision=0.001)
    assert res_1dbu['status'] == 'fail'
    assert res_1dbu['violations'][0]['shortage'] == pytest.approx(0.001, abs=1e-6)


def test_cover_check_invalid_and_unknown_cases():
    outer = _rect(0.0, 0.0, 1.0, 1.0)
    # Non-finite or negative minimum
    assert CoverCheck().run(_rect(0.1, 0.1, 0.9, 0.9), outer, minimum=-0.01, precision=0.001)['status'] == 'unknown'
    assert CoverCheck().run(_rect(0.1, 0.1, 0.9, 0.9), outer, minimum=math.nan, precision=0.001)['status'] == 'unknown'
    # Self-intersecting bowtie polygon -> unknown
    bowtie = [[0.0, 0.0], [1.0, 1.0], [1.0, 0.0], [0.0, 1.0]]
    assert CoverCheck().run(bowtie, outer, minimum=0.03, precision=0.001)['status'] == 'unknown'


def test_check_cover_tool_resolves_polygon_ids(tmp_path):
    gds_path = tmp_path / 'cell.gds'
    drc_path = tmp_path / 'cell.drc.results'
    map_path = tmp_path / 'layers.map'
    rundir = tmp_path / 'runs' / 'r1' / 'rundir'

    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell('top')
    cell.add(gdstk.rectangle((0.02, 0.1), (0.9, 0.9), layer=1, datatype=0))
    cell.add(gdstk.rectangle((0.0, 0.0), (1.0, 1.0), layer=2, datatype=0))
    lib.write_gds(str(gds_path))
    drc_path.write_text('top 1000\nR1\n1 1 1 Nov 5 12:30:30 2026\nDesc\np 1 3\n0 0\n100 0\n100 100\n')
    map_path.write_text('1 0 M1 drawing\n2 0 M2 drawing\n')

    InputLoader(gds_path=gds_path, drc_path=drc_path, output_dir=rundir, layer_map_files=[map_path], verbose=False).run()
    layout = LayoutService(rundir, verbose=False).run()
    polygons = layout.read_gds('top')['polygons']
    inner_id = polygons[0]['id']
    outer_id = polygons[1]['id']

    services = type('Services', (), {'layout': layout})()
    ctx = TaskContext(task='drc', inputs={}, state={}, services=services)

    tool_res = check_cover(ctx, 'top', inner_id, outer_id, 0.03)
    assert tool_res.payload['status'] == 'fail'
    assert tool_res.payload['inner_id'] == inner_id
    assert tool_res.payload['outer_id'] == outer_id
    assert max(v['shortage'] for v in tool_res.payload['violations']) == pytest.approx(0.01, abs=0.001)

    unknown_res = check_cover(ctx, 'top', 'bad-id', outer_id, 0.03)
    assert unknown_res.payload['status'] == 'unknown'
