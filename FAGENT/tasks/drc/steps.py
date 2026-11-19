"""Task-local execution steps."""

# === SECTION 1: Imports ===
import json
from pathlib import Path

from FAGENT.capabilities.layout.input_loader import InputLoader
from FAGENT.engine.graph_services import GraphServices


# === SECTION 2: Steps ===
class Steps:
    """Execute the selected task stages."""

    def run(self, context):
        """Read the report for this task."""
        self.result = self.read_drc(context)
        return self.result

    def __init__(self):
        """Initialize step outputs."""
        self.result = None

    def append_context(self, state, payload):
        """Append a report to the model conversation."""
        return [*state.get('messages', []), {'role': 'user', 'content': json.dumps(payload)}]

    def read_drc(self, context):
        report = context.services.read_drc()
        return {'report': report, 'messages': self.append_context(context.state, {'drc': report})}

    def validate_drc(self, context):
        candidate = context.state.get('candidate_path')
        validation = {'status': 'unavailable', 'reason': 'No saved candidate'}
        if candidate:
            try:
                digest = context.state.get('candidate_digest')
                if not digest or GraphServices.candidate_digest(Path(candidate)) != digest:
                    raise ValueError('Saved DRC candidate is missing or changed')
                validation = context.services.validate_drc(candidate)
                if not isinstance(validation, dict) or validation.get('status') not in ('pass', 'failed', 'unavailable'):
                    raise ValueError('Missing backend result')
                if validation['status'] == 'pass' and (
                    context.services.workflow.backend != 'local' or validation.get('simulated') is not True
                ):
                    raise ValueError('DRC pass requires supported validation evidence')
                if validation.get('candidate') not in (None, candidate):
                    raise ValueError('Backend result refers to a different candidate')
            except (OSError, RuntimeError, ValueError) as exc:
                validation = {'status': 'error', 'reason': str(exc)}
        return {'validation': validation, 'messages': self.append_context(context.state, {'validation': validation})}

    def prepare(self, task_cfg, setup, base, output, config, verbose):
        """Prepare one isolated run per configured cell."""
        maps = setup['layer_map_files']
        if not isinstance(maps, list) or not maps or any(not isinstance(p, str) or not p.strip() for p in maps):
            raise ValueError('layer_map_files must contain paths')
        directory = base / task_cfg['drc_folder']
        decks = task_cfg['drc_files']
        if isinstance(decks, str):
            decks = [decks]
        if not isinstance(decks, list) or not decks:
            raise ValueError('DRC requires at least one rule deck')
        resolved_decks = [(base / path).resolve() for path in decks]
        for deck in resolved_decks:
            if not deck.is_file():
                raise FileNotFoundError(f'Missing DRC rule deck: {deck}')
        config = {**config, 'drc_files': [str(path) for path in resolved_decks]}
        cells = (base / setup['list_cells']).read_text().splitlines()
        for line in cells:
            cell = line.strip()
            if not cell or cell.startswith('#'):
                continue
            if cell in {'.', '..'} or Path(cell).name != cell:
                raise ValueError(f'Invalid cell name: {cell}')
            gds = directory / f'{cell}.gds'
            drc = directory / f'{cell}.drc.results'
            resolved_maps = [(base / path).resolve() for path in maps]
            rundir = output / cell / 'rundir'
            InputLoader(gds_path=gds, drc_path=drc, output_dir=rundir,
                        layer_map_files=resolved_maps, verbose=verbose).run()
            yield rundir, config, {**task_cfg, 'gds': str(gds), 'drc': str(drc),
                                   'layer_map': [str(path) for path in resolved_maps]}


_steps = Steps()
read_drc = _steps.read_drc
validate_drc = _steps.validate_drc
prepare = _steps.prepare
