"""DRC backend boundary. The available Calibre implementation is a simulator."""
from pathlib import Path
from tempfile import mkdtemp


def run_drc(layout, candidate, backend):
    if backend != 'local':
        return {'status': 'unavailable', 'reason': 'Real DRC invocation and report contract are unavailable'}
    candidate = Path(candidate)
    if candidate != layout.candidate or not candidate.is_file():
        raise ValueError('Saved DRC candidate is required')
    deck = next((Path(path) for path in layout.drc_files if Path(path).is_file()), None)
    if deck is None:
        if layout.drc_files:
            raise ValueError('Configured DRC decks do not exist')
        return {'status': 'pass', 'simulated': True, 'candidate': str(candidate), 'backend': 'mock'}
    from CALIBRE.calibre import CalibreSimulator

    from ...capabilities.drc.parser import DrcParser

    top_cell = layout.drc_data.get('top_cell')
    outdir = mkdtemp(prefix='drc-validation-', dir=layout.rundir)
    simulator = CalibreSimulator(gds_path=candidate, drc_path=deck, outdir=outdir, top_cell=top_cell).run()
    if not simulator.results_path or not simulator.summary_path:
        raise ValueError('Simulated DRC backend did not produce both reports')
    results = DrcParser.read_drc(simulator.results_path)
    summary = DrcParser.read_drc(simulator.summary_path)
    if (results['top_cell'] != simulator.top_cell or summary['top_cell'] != simulator.top_cell
            or results['total_violations'] != simulator.total_violations
            or summary['total_violations'] != simulator.total_violations):
        raise ValueError('Simulated DRC reports do not match candidate evidence')
    return {'status': 'pass' if simulator.total_violations == 0 else 'failed', 'simulated': True,
            'backend': 'calibre_simulator', 'candidate': str(candidate), 'deck': str(deck), 'report': str(simulator.results_path),
            'summary': str(simulator.summary_path), 'total_violations': simulator.total_violations,
            'violations_by_rule': {rule: len(items) for rule, items in simulator.violations.items()}}
