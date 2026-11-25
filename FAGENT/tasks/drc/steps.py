"""Task-local execution steps."""

# === SECTION 1: Imports ===
from datetime import datetime
import json
from pathlib import Path

from FAGENT.capabilities.layout.input_loader import InputLoader
from FAGENT.engine.conversation_trace import ConversationTrace
from FAGENT.engine.graph_services import GraphServices
from FAGENT.tasks.drc.checklist import Checklist
from FAGENT.tasks.drc.rule_evidence import RuleEvidence


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
        goal = context.services.config.get('goal', '') if hasattr(context.services, 'config') else ''
        constraints = drc_context.get('knowledge', {}).get('constraints', [])
        sys_content = context.services.build_system_message(goal=goal, constraints=constraints)

        current_messages = list(context.state.get('messages', []))
        if current_messages and current_messages[0].get('role') == 'system':
            updated_messages = [{'role': 'system', 'content': sys_content}, *current_messages[1:]]
        else:
            updated_messages = [{'role': 'system', 'content': sys_content}, *current_messages]

        updated_messages.append({'role': 'user', 'content': json.dumps({'drc_context': drc_context})})

        # Initialize deterministic checklist from report
        report = context.services.drc_report
        evidence = None
        if isinstance(report, dict) and Checklist.excludes_density(goal):
            sources = getattr(context.services, 'input_sources', None) or {}
            decks = sources.get('decks') or context.services.config.get('drc_files', [])
            if isinstance(decks, (str, Path)):
                decks = [decks]
            names = {str(rule.get('name', '')).strip() for rule in (report.get('rules') or [])}
            names.update(report.get('violations_by_rule') or {})
            evidence = RuleEvidence().density_evidence(names - {''}, decks)
        checklist = Checklist().run(report=report, goal=goal, rule_evidence=evidence)
        snapshot = checklist.read()
        task_queue = checklist.queue_ids()
        result = {
            'report': report,
            'drc_context': drc_context,
            'messages': updated_messages,
            'checklist': snapshot,
            'checklist_digest': snapshot['digest'],
            'checklist_path': str(Path(context.services.rundir) / 'checklist.json'),
            'task_queue': task_queue,
            'current_task_id': task_queue[0] if task_queue else None,
        }
        context.services.check_output_path(Path(result['checklist_path']))
        checklist.save(Path(result['checklist_path']))
        return result

    def read_gds(self, context):
        """Read target cell geometry in GDS user units and attach it to drc_context."""
        report = context.state.get('report') or {}
        drc_context = dict(context.state.get('drc_context') or {})
        target_cell = (
            report.get('top_cell')
            or (drc_context.get('drc') or {}).get('top_cell')
            or context.inputs.get('cell')
        )
        if not isinstance(target_cell, str) or not target_cell.strip():
            raise ValueError('Missing target cell for read_gds')
        gds_payload = context.services.layout.read_gds(cell_name=target_cell)
        drc_context['gds'] = gds_payload
        drc_context['target_cell'] = target_cell
        drc_context['candidate_path'] = context.state.get('candidate_path')
        drc_context['candidate_digest'] = context.state.get('candidate_digest')
        inspected = list(dict.fromkeys([*(context.state.get('inspected') or []), target_cell]))
        return {'drc_context': drc_context, 'inspected': inspected}

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
            trace = ConversationTrace(rundir)
            op_id = trace.start_operation(
                'preparation',
                {'cell': cell, 'gds': str(gds), 'drc': str(drc), 'layer_map': [str(path) for path in resolved_maps]},
            )
            try:
                InputLoader(gds_path=gds, drc_path=drc, output_dir=rundir,
                            layer_map_files=resolved_maps, verbose=verbose).run()
                trace.finish_operation(
                    op_id,
                    'preparation',
                    status='completed',
                    outputs={'gds_json': str(rundir / 'gds.json'), 'drc_json': str(rundir / 'drc.json')},
                )
            except Exception as exc:
                trace.finish_operation(op_id, 'preparation', status='failed', error=exc)
                raise
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
read_gds = _steps.read_gds
validate_drc = _steps.validate_drc
prepare = _steps.prepare
