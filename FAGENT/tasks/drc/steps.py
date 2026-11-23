"""Task-local execution steps."""

# === SECTION 1: Imports ===
from datetime import datetime
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
        drc_context = context.services.build_drc_context()
        return {'report': context.services.drc_report, 'drc_context': drc_context,
                'messages': self.append_context(context.state, {'drc_context': drc_context})}

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
        drc_directory = base / task_cfg['drc_folder']
        gds_directory = base / task_cfg['gds_folder']
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
            gds = gds_directory / f'{cell}.gds'
            drc = drc_directory / f'{cell}.drc.results'
            resolved_maps = [(base / path).resolve() for path in maps]
            task_name = task_cfg.get('task_name') or task_cfg.get('task') or 'drc'
            run_id, rundir = self.allocate_rundir(output, cell, task_name=task_name)
            InputLoader(gds_path=gds, drc_path=drc, output_dir=rundir,
                        layer_map_files=resolved_maps, verbose=verbose).run()
            yield rundir, config, {**task_cfg, 'cell': cell, 'run_id': run_id, 'rundir': str(rundir),
                                   'gds': str(gds), 'drc': str(drc),
                                   'layer_map': [str(path) for path in resolved_maps]}

    def allocate_rundir(self, output, cell, task_name='drc'):
        """Allocate a fresh unique run directory for one cell execution."""
        stamp = datetime.now().strftime('%H:%M:%S-%d%b%Y')
        base_id = f'{stamp}-{task_name}'
        run_id = base_id
        counter = 1
        while True:
            rundir = output / cell / 'runs' / run_id / 'rundir'
            if not rundir.parent.exists():
                return run_id, rundir
            run_id = f'{base_id}-{counter}'
            counter += 1


_steps = Steps()
read_drc = _steps.read_drc
validate_drc = _steps.validate_drc
prepare = _steps.prepare
