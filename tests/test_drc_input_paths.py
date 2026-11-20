import json
from pathlib import Path

import gdstk

from FAGENT.tasks.drc.steps import Steps


def test_prepare_reads_gds_and_drc_from_separate_folders(tmp_path):
    gds_dir = tmp_path / 'layouts'
    drc_dir = tmp_path / 'reports'
    gds_dir.mkdir()
    drc_dir.mkdir()
    cell = 'sample_cell'
    library = gdstk.Library()
    library.new_cell(cell)
    library.write_gds(str(gds_dir / f'{cell}.gds'))
    (drc_dir / f'{cell}.drc.results').write_text(
        f'{cell} 1000\nRULE_A\n0 0 1 Nov 5 12:30:30 2026\nEmpty rule\n'
        'RULE_B\n1 1 1 Nov 5 12:30:30 2026\nViolation\np 1 3\n0 0\n10 0\n10 10\n'
    )
    (drc_dir / 'rules.drc').write_text('RULE_A {}\n')
    (tmp_path / 'cells').write_text(cell + '\n')
    (tmp_path / 'layers').write_text('1 0 M1 drawing\n')
    setup = {'list_cells': 'cells', 'layer_map_files': ['layers']}
    task = {'gds_folder': 'layouts', 'drc_folder': 'reports', 'drc_files': 'reports/rules.drc'}

    existing = tmp_path / 'output' / cell / 'rundir'
    existing.mkdir(parents=True)
    for name in ('gds.json', 'drc.json', 'result.json'):
        (existing / name).write_text('stale output')

    rundir, _, inputs = next(Steps().prepare(task, setup, tmp_path, tmp_path / 'output', {}, False))

    assert Path(inputs['gds']) == gds_dir / f'{cell}.gds'
    report = json.loads((rundir / 'drc.json').read_text())
    assert report['source'] == str(drc_dir / f'{cell}.drc.results')
    assert [rule['name'] for rule in report['rules']] == ['RULE_B']
    assert report['total_violations'] == 1
    assert json.loads((rundir / 'gds.json').read_text())['source'] == str(gds_dir / f'{cell}.gds')
