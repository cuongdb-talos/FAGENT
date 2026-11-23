import json
from pathlib import Path

import gdstk
import pytest

from FAGENT.capabilities.layout.cover_check import CoverCheck
from FAGENT.capabilities.layout.input_loader import InputLoader
from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.capabilities.layout.polygon_ids import PolygonIds
from FAGENT.engine.graph_services import GraphServices
from FAGENT.tasks.drc.checklist import Checklist
from FAGENT.tasks.drc.steps import Steps


def test_drc_integrated_functional_pipeline(tmp_path):
    # 1. Setup synthetic layout and DRC files
    drc_dir = tmp_path / 'DRC'
    drc_dir.mkdir(parents=True)
    gds_path = drc_dir / 'ROM_MCELL0.gds'
    drc_path = drc_dir / 'ROM_MCELL0.drc.results'
    deck_path = drc_dir / 'rules.drc'
    map_path = tmp_path / 'layers.map'

    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell('ROM_MCELL0')
    # Inner polygon: [0.02, 0.1] to [0.9, 0.9] on layer 1 (violates minimum enclosure 0.03 from outer x=0)
    cell.add(gdstk.rectangle((0.02, 0.1), (0.9, 0.9), layer=1, datatype=0))
    # Outer polygon: [0.0, 0.0] to [1.0, 1.0] on layer 2
    cell.add(gdstk.rectangle((0.0, 0.0), (1.0, 1.0), layer=2, datatype=0))
    lib.write_gds(str(gds_path))

    drc_text = (
        'ROM_MCELL0 1000\n'
        'R_EN\n'
        '2 2 1 Nov 5 12:30:30 2026\n'
        'Enclosure violation\n'
        'p 1 3\n20 100\n900 100\n900 900\n'
        'p 2 3\n20 100\n900 100\n900 900\n'
        'R_DENSITY\n'
        '1 1 1 Nov 5 12:30:30 2026\n'
        'Metal density\n'
        'p 1 3\n0 0\n100 0\n100 100\n'
        'R_UNSUPPORTED\n'
        '0 1 1 Nov 5 12:30:30 2026\n'
        'No geometry record\n'
    )
    drc_path.write_text(drc_text)
    deck_path.write_text('R_EN { ENC M1 M2 < 0.03 }\nR_DENSITY { DENSITY M1 < 0.2 }\nR_UNSUPPORTED { }\n')
    map_path.write_text('1 0 M1 drawing\n2 0 M2 drawing\n')

    # 2. Run isolation and input preparation
    setup_cfg = {'list_cells': 'cells', 'layer_map_files': [str(map_path)]}
    (tmp_path / 'cells').write_text('ROM_MCELL0\n')
    task_cfg = {'drc_folder': 'DRC', 'gds_folder': 'DRC', 'drc_files': str(deck_path)}
    output_base = tmp_path / 'output'

    steps_iter = Steps().prepare(task_cfg, setup_cfg, tmp_path, output_base, {}, False)
    rundir, config, inputs = next(steps_iter)

    assert 'runs' in str(rundir)
    assert (rundir / 'gds.json').exists()
    assert (rundir / 'drc.json').exists()

    drc_data = json.loads((rundir / 'drc.json').read_text())
    assert len(drc_data['rules']) == 3
    en_rule = next(r for r in drc_data['rules'] if r['name'] == 'R_EN')
    assert en_rule['total_count'] == 2
    assert len(en_rule['geometry']) == 2

    # 3. Stable Polygon IDs initialization
    poly_ids = PolygonIds().run(lib)
    poly_map_path = rundir / 'polygon_ids.json'
    poly_ids.save(poly_map_path, candidate_digest='initial')
    assert poly_ids.id_for('ROM_MCELL0', cell.polygons[0]) == 'ROM_MCELL0:poly:0'
    assert poly_ids.id_for('ROM_MCELL0', cell.polygons[1]) == 'ROM_MCELL0:poly:1'

    # 4. Geometric Cover Check
    inner_poly = cell.polygons[0].points.tolist()
    outer_poly = cell.polygons[1].points.tolist()
    cover_res = CoverCheck().run(inner_poly, outer_poly, minimum=0.03, precision=0.001)
    assert cover_res['status'] == 'fail'
    assert cover_res['measured_distance'] == pytest.approx(0.02, abs=0.001)
    assert len(cover_res['violations']) >= 1
    assert cover_res['violations'][0]['shortage'] == pytest.approx(0.01, abs=0.001)
    assert cover_res['units'] == 'gds_user_units'

    # 5. Checklist creation with goal skipping density and retaining unsupported rule
    goal = 'Fix all DRC errors but ignore error about density.'
    proposed_tasks = [
        {'rule': 'R_EN', 'violation_id': 1, 'occurrence': 0, 'work': 'Fix enclosure 1', 'status': 'pending', 'reason': ''},
        {'rule': 'R_EN', 'violation_id': 2, 'occurrence': 0, 'work': 'Fix enclosure 2', 'status': 'pending', 'reason': ''},
        {'rule': 'R_DENSITY', 'violation_id': 1, 'occurrence': 0, 'work': 'Skip density', 'status': 'skipped', 'reason': 'Goal excludes density'},
    ]
    checklist = Checklist().run(drc_data, proposed_tasks, goal=goal)
    summary = checklist.summary()
    assert summary['counts']['pending'] == 2
    assert summary['counts']['skipped'] == 1
    assert summary['counts']['unresolved'] == 1  # R_UNSUPPORTED placed in unresolved

    # Update checklist task-1 with evidence
    checklist.update('task-1', 'pass', 'Verified cover', 'Cover check passed after inspection', ['ROM_MCELL0:poly:0'], [cover_res])
    assert checklist.read()['summary']['counts']['pass'] == 1

    # 6. Polygon repair and stale invalidation
    layout_service = LayoutService(rundir, verbose=False, drc_files=[str(deck_path)]).run()
    # Repair: expand inner rectangle to [0.05, 0.1] to [0.9, 0.9] -> enclosure distance now 0.05 >= 0.03
    fixed_inner = [[0.05, 0.1], [0.9, 0.1], [0.9, 0.9], [0.05, 0.9]]
    fixed_res = CoverCheck().run(fixed_inner, outer_poly, minimum=0.03, precision=0.001)
    assert fixed_res['status'] == 'pass'

    layout_service.replace_polygon('ROM_MCELL0', 'ROM_MCELL0:poly:0', fixed_inner)
    layout_service.save_gds()
    assert (rundir / 'candidate.gds').exists()
    candidate_digest = GraphServices.candidate_digest(rundir / 'candidate.gds')

    # Invalidate passed tasks associated with edited polygon
    checklist.invalidate('ROM_MCELL0', 'ROM_MCELL0:poly:0')
    assert checklist.read()['summary']['stale'] == 1

    # Checklist persistence and digest verification
    checklist_path = rundir / 'checklist.json'
    checklist.save(checklist_path)
    cur_digest = checklist.digest()
    restored = Checklist().restore(checklist_path, expected_digest=cur_digest)
    assert restored.digest() == cur_digest

    # Tampered checklist restoration must fail fail-fast
    checklist_path.write_text(checklist_path.read_text().replace('Verified cover', 'Tampered'))
    with pytest.raises(ValueError, match='Checklist digest mismatch'):
        Checklist().restore(checklist_path, expected_digest=cur_digest)
