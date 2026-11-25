"""Task-local execution steps."""

# === SECTION 1: Imports ===
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from FAGENT.capabilities.layout.input_loader import InputLoader
from FAGENT.engine.conversation_trace import ConversationTrace
from FAGENT.engine.graph_services import GraphServices
from FAGENT.tasks.drc.checklist import Checklist
from FAGENT.tasks.drc.tools import DrcTools


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

        report = context.services.drc_report
        checklist = Checklist()
        violations = checklist.rule_summary(report)
        selected_rules = self._select_checklist_rules(context.services, violations)
        checklist.run(report=report, selected_rules=selected_rules)
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

    def _select_checklist_rules(self, services, violations):
        """Exclude violations close to the explicit skip query in embedding space."""
        selected = {entry['rule'] for entry in violations}
        query = services.config.get('skip_prompt', '')
        threshold = services.config.get('skip_distance_threshold', 0.5)
        if not isinstance(query, str):
            raise ValueError('skip_prompt must be a string')
        if type(threshold) not in (float, int) or not 0 <= threshold <= 2:
            raise ValueError('skip_distance_threshold must be between 0 and 2')
        if not selected or not query.strip():
            return selected

        texts = []
        for entry in violations:
            description = entry['description']
            if isinstance(description, list):
                description = '\n'.join(description)
            texts.append((description or '').strip() or entry['rule'])

        query_vector, *rule_vectors = services.embed_texts([query.strip(), *texts])
        query_norm = math.hypot(*query_vector)
        if not query_norm or len(rule_vectors) != len(violations):
            raise ValueError('Invalid checklist embedding response')
        distances = {}
        for entry, vector in zip(violations, rule_vectors, strict=True):
            norm = math.hypot(*vector)
            if not norm or len(vector) != len(query_vector):
                raise ValueError(f"Invalid embedding for rule {entry['rule']}")
            cosine = sum((x / query_norm) * (y / norm) for x, y in zip(query_vector, vector, strict=True))
            distances[entry['rule']] = 1 - max(-1.0, min(1.0, cosine))
        selected = {rule for rule, distance in distances.items() if distance >= threshold}
        trace = getattr(services, 'conversation_trace', None)
        if trace:
            op = trace.start_operation('checklist_embedding_filter', {'query': query, 'threshold': threshold})
            trace.finish_operation(op, 'checklist_embedding_filter', status='completed',
                                   outputs={'distances': distances, 'excluded_rules': sorted(distances.keys() - selected)})
        return selected

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
        candidate_path = context.state.get('candidate_path')
        candidate_digest = context.state.get('candidate_digest')
        if not candidate_path:
            candidate_path = str(context.services.checkpoint_candidate())
            candidate_digest = context.services.candidate_digest(Path(candidate_path))
            if not candidate_digest:
                raise ValueError('Missing candidate digest after initial GDS checkpoint')
        drc_context['candidate_path'] = candidate_path
        drc_context['candidate_digest'] = candidate_digest
        inspected = list(dict.fromkeys([*(context.state.get('inspected') or []), target_cell]))
        return {'drc_context': drc_context, 'inspected': inspected,
                'candidate_path': candidate_path, 'candidate_digest': candidate_digest}

    def save_gds(self, context):
        """Save final candidate GDS once after queue completion, preserving original GDS on error."""
        if context.state.get('save_completed'):
            return {}
        try:
            res = DrcTools.save_gds(context)
            return dict(res.updates or {})
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            reason = f'save_gds failed: {exc}'
            print(f'[ERROR] {reason}', file=sys.stderr)
            return {'failed': True, 'result': {'status': 'incomplete', 'reason': reason}}

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
        for key in ('skip_prompt', 'skip_distance_threshold'):
            if key in task_cfg:
                config[key] = task_cfg[key]
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
            run_id = f'{datetime.now():%H:%M:%S-%d%b%Y}-{task_name}'
            rundir = output / cell
            self.prepare_cell_output(rundir, cell, gds, drc, resolved_maps, resolved_decks)
            yield rundir, config, {**task_cfg, 'cell': cell, 'run_id': run_id, 'rundir': str(rundir),
                                   'gds': str(gds), 'drc': str(drc),
                                   'layer_map': [str(path) for path in resolved_maps]}

    def prepare_cell_output(self, rundir, cell, gds, drc, layer_maps, decks):
        """Stage fresh input artifacts and replace the previous cell output after ingestion."""
        if rundir.is_symlink():
            raise ValueError(f'Cell output must not be a symbolic link: {rundir}')
        if any(path.resolve().is_relative_to(rundir.resolve()) for path in (gds, drc, *layer_maps, *decks)):
            raise ValueError(f'Cell output must not contain configured inputs: {rundir}')
        rundir.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=f'.{cell}-', dir=rundir.parent) as workspace:
            staged = Path(workspace) / 'current'
            previous = Path(workspace) / 'previous'
            trace = ConversationTrace(staged)
            op_id = trace.start_operation(
                'preparation',
                {'cell': cell, 'gds': str(gds), 'drc': str(drc), 'layer_maps': [str(path) for path in layer_maps]},
            )
            InputLoader(gds_path=gds, drc_path=drc, output_dir=staged,
                        layer_map_files=layer_maps, verbose=False).run()
            trace.finish_operation(
                op_id, 'preparation', status='completed',
                outputs={'gds_json': str(rundir / 'gds.json'), 'drc_json': str(rundir / 'drc.json')},
            )
            if rundir.exists():
                if not rundir.is_dir():
                    raise ValueError(f'Cell output must be a directory: {rundir}')
                rundir.rename(previous)
            try:
                staged.rename(rundir)
            except OSError:
                if previous.exists():
                    previous.rename(rundir)
                raise


_steps = Steps()
read_drc = _steps.read_drc
read_gds = _steps.read_gds
save_gds = _steps.save_gds
validate_drc = _steps.validate_drc
prepare = _steps.prepare


def repair_route(context):
    state = context.state
    task_id = state.get('current_task_id')
    if task_id is None:
        return 'finished'
    transaction = (state.get('task_transactions') or {}).get(task_id)
    check = (state.get('task_checks') or {}).get(task_id) or {}
    if transaction and (check.get('status'), check.get('candidate_digest')) != (
        'pass', state.get('candidate_digest'),
    ):
        return 'verify'

    messages = state.get('messages', [])[state.get('task_message_start', 0):]
    if messages and messages[-1].get('role') == 'tool':
        results = {
            message['tool_call_id']: json.loads(message['content'])
            for message in messages if message.get('role') == 'tool'
        }
        seen = set()
        for message in messages:
            for call in message.get('tool_calls') or ():
                function = call['function']
                args = function['arguments']
                if isinstance(args, str):
                    args = json.loads(args)
                result = results.get(call['id'], {})
                if args.get('action') == 'propose' and not result.get('proposals'):
                    key = function['name'], json.dumps(args, sort_keys=True)
                    if key in seen:
                        return 'repeated'
                    seen.add(key)
    return 'continue'


def verify_current_task(context):
    task_id = context.state['current_task_id']
    transaction = context.state['task_transactions'][task_id]
    check = {'area': DrcTools.check_area, 'cover': DrcTools.check_cover}[transaction['kind']]
    result = check(context, task_id)
    updates = dict(result.updates)
    if result.payload.get('status') != 'pass' and not result.payload.get('rolled_back'):
        return {
            'failed': True,
            'result': {'status': 'incomplete', 'reason': result.payload.get('reason', 'Local check unavailable')},
        }
    if 'candidate_digest' in updates:
        gds_context = dict(context.state.get('drc_context') or {})
        gds_context.update(
            gds=context.services.layout.read_gds(cell_name=transaction['cell_name']),
            candidate_path=updates['candidate_path'],
            candidate_digest=updates['candidate_digest'],
        )
        updates['drc_context'] = gds_context
    return updates


def record_repeated_proposal(context):
    from FAGENT.engine.graph_nodes import GraphNodes

    return GraphNodes(context.services).advance_queue_item(
        context.state, 'unresolved', 'Repeated proposal call without a geometry change',
    )


def queue_route(context):
    return 'finished' if context.state.get('current_task_id') is None else 'continue'


def human_review_route(context):
    return context.state['review']['decision']


def resume_repair(context):
    """Retry unresolved markers on the saved candidate with the user's feedback."""
    state = context.state
    checklist = Checklist.from_dict(state['checklist'])
    queue = [task['task_id'] for task in checklist.tasks if task['status'] == 'unresolved']
    if not queue:
        return {}
    for task_id in queue:
        checklist.update(task_id, 'pending')
    snapshot = checklist.read()
    path = Path(context.services.rundir) / 'checklist.json'
    context.services.check_output_path(path)
    checklist.save(path)
    return {
        'checklist': snapshot,
        'checklist_digest': snapshot['digest'],
        'checklist_path': str(path),
        'task_queue': queue,
        'current_task_id': queue[0],
        'task_message_start': len(state.get('messages') or []),
        'task_transactions': {},
        'task_checks': {},
        'proposals': {},
        'iteration': 0,
        'result': None,
        'save_completed': False,
    }
