"""DRC backend boundary. The available Calibre implementation is a simulator."""

# ==============================================================================
# SECTION 1: Standard and Internal Module Imports
# ==============================================================================
from pathlib import Path
from tempfile import mkdtemp

from CALIBRE.calibre_simulator import CalibreSimulator
from ...capabilities.drc.drc_parser import DrcParser


# ==============================================================================
# SECTION 2: DRC Execution and Validation Runner Class
# ==============================================================================
class DrcRunner:
    """Invokes local simulated or real Calibre DRC backend on the candidate layout."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self):
        if self.backend != 'local':
            self.result = {'status': 'unavailable', 'reason': 'Real DRC invocation and report contract are unavailable'}
            return self.result
        if self.candidate != self.layout.candidate or not self.candidate.is_file():
            raise ValueError('Saved DRC candidate is required')
        deck = next((Path(path) for path in self.layout.drc_files if Path(path).is_file()), None)
        if deck is None:
            if self.layout.drc_files:
                raise ValueError('Configured DRC decks do not exist')
            self.result = {'status': 'pass', 'simulated': True, 'candidate': str(self.candidate), 'backend': 'mock'}
            return self.result

        top_cell = self.layout.drc_data.get('top_cell')
        outdir = mkdtemp(prefix='drc-validation-', dir=self.layout.rundir)
        simulator = CalibreSimulator(gds_path=self.candidate, drc_path=deck, outdir=outdir, top_cell=top_cell).run()
        if not simulator.results_path or not simulator.summary_path:
            raise ValueError('Simulated DRC backend did not produce both reports')
        results = DrcParser.read_drc(simulator.results_path)
        summary = DrcParser.read_drc(simulator.summary_path)
        if (
            results['top_cell'] != simulator.top_cell
            or summary['top_cell'] != simulator.top_cell
            or results['total_violations'] != simulator.total_violations
            or summary['total_violations'] != simulator.total_violations
        ):
            raise ValueError('Simulated DRC reports do not match candidate evidence')
        self.result = {
            'status': 'pass' if simulator.total_violations == 0 else 'failed',
            'simulated': True,
            'backend': 'calibre_simulator',
            'candidate': str(self.candidate),
            'deck': str(deck),
            'report': str(simulator.results_path),
            'summary': str(simulator.summary_path),
            'total_violations': simulator.total_violations,
            'violations_by_rule': {rule: len(items) for rule, items in simulator.violations.items()},
        }
        return self.result

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, layout, candidate, backend):
        self.layout = layout
        self.candidate = Path(candidate)
        self.backend = backend
        self.result = {}
