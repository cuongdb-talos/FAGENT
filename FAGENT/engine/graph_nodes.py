"""Deterministic workflow stages and constrained model tool calls."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import traceback

import jsonschema
from langgraph.graph import END
from langgraph.errors import GraphInterrupt
from langgraph.types import interrupt

# ==============================================================================
# SECTION 2: Internal Module Imports
# ==============================================================================
from .fagent_state import FagentState
from .task_contracts import TaskContext, ToolResult
from ..capabilities.layout.violation_geometry import ViolationGeometry
from ..tasks.drc.checklist import Checklist
from ..tasks.drc.rule_evidence import RuleEvidence


# ==============================================================================
# SECTION 3: Graph Node Actions and Tool Execution Class
# ==============================================================================
class GraphNodes:
    """Implements individual node handlers, routing logic, and tool dispatch for LangGraph."""

    # --------------------------------------------------------------------------
    # Main Setup / Registration Entry Point
    # --------------------------------------------------------------------------
    def run(self):
        return self

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, services):
        self.services = services
        self.trace = getattr(services, "trace", None)
        self.last_conversation = None

    # --------------------------------------------------------------------------
    # Message Context Helpers
    # --------------------------------------------------------------------------
    def run_task_step(self, state, step, tools=False):
        """Invoke a local step and capture serializable updates."""
        self.last_conversation = None
        workflow = self.services.workflow
        node = f'{step.id}--tools' if tools else self.node_name(step.id)
        if self.trace:
            self.trace.node_start(node, step.id, step.action, tools=tools)
        trace_inst = getattr(self.services, 'conversation_trace', None)
        node_op = None
        if trace_inst:
            node_op = trace_inst.start_operation(
                'graph_node',
                {'node': node, 'step_id': step.id, 'action': step.action, 'tools': tools},
            )
        inputs = state.get('inputs', {})
        task_state = state.get('task_state', {})
        context = TaskContext(workflow.task, inputs, {**task_state, **state}, self.services)
        missing = set(workflow.inputs) - inputs.keys()
        try:
            if missing:
                raise ValueError(f'Missing inputs: {", ".join(sorted(missing))}')
            if tools:
                res = self.execute_tools(context, step)
                if trace_inst and node_op:
                    trace_inst.finish_operation(node_op, 'graph_node', status='completed')
                return res
            if step.action == 'agent':
                res = self.execute_agent(context, step)
                if trace_inst and node_op:
                    trace_inst.finish_operation(node_op, 'graph_node', status='completed')
                return res
            if step.action == 'review':
                res = self.review(state)
                if trace_inst and node_op:
                    trace_inst.finish_operation(node_op, 'graph_node', status='completed')
                return res
            updates = self.execute_step(context, step)
            if trace_inst and node_op:
                trace_inst.finish_operation(node_op, 'graph_node', status='completed', outputs={'updates_keys': list(updates.keys())})
            return {**updates, 'task_state': {**task_state, **updates}}
        except GraphInterrupt:
            if self.trace:
                self.trace.node_end(node, step.id, "waiting")
            if trace_inst and node_op:
                trace_inst.finish_operation(node_op, 'graph_node', status='waiting')
            raise
        except Exception as exc:
            if trace_inst and node_op:
                trace_inst.finish_operation(node_op, 'graph_node', status='failed', error=exc)
            detail = str(exc).strip()
            msg = f': {detail}' if detail else ''
            reason = f'{workflow.task}/{step.id}: {type(exc).__name__}{msg}'
            if missing:
                reason += f' (missing inputs: {", ".join(sorted(missing))})'
            print(f'[ERROR] {reason}', file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            update = {'failed': True, 'result': {'status': 'incomplete', 'reason': reason}}
            if self.last_conversation is not None:
                update['messages'] = self.last_conversation
            return update

    def execute_step(self, context, step):
        """Return the JSON-compatible mapping from one or more ordered step callables."""
        functions = getattr(step, 'functions', None)
        if functions is None:
            functions = (step.function,) if getattr(step, 'function', None) else ()
        merged = {}
        for fn in functions:
            step_context = TaskContext(
                context.task,
                context.inputs,
                {**context.state, **merged},
                context.services,
            )
            updates = fn(step_context)
            if not isinstance(updates, dict) or any(not isinstance(key, str) for key in updates):
                raise TypeError('A step must return a mapping with string keys')
            json.dumps(updates, allow_nan=False)
            merged.update(updates)
        return merged

    def write_checklist_artifact(self, checklist):
        path = Path(self.services.rundir) / 'checklist.json'
        self.services.check_output_path(path)
        Checklist.from_dict(checklist).save(path)
        return str(path)

    def advance_queue_item(self, state, status: str, reason: str, response=None, message_count: int | None = None) -> dict:
        """Record terminal item outcome, pop current_task_id once, reset iteration, and advance."""
        current_id = state.get('current_task_id')
        queue = list(state.get('task_queue') or [])
        if current_id in queue:
            queue.remove(current_id)
        elif queue:
            queue.pop(0)

        updates = {}
        final_status = status
        final_reason = reason
        checklist_data = state.get('checklist')
        if checklist_data is not None and current_id:
            checklist = Checklist.from_dict(checklist_data)
            task = next((t for t in checklist.tasks if t.get('task_id') == current_id), None)
            if task is not None:
                if task.get('status') in ('pending', 'in_progress') or status == 'unresolved':
                    snapshot = checklist.update(task_id=current_id, status=status, reason=reason)
                else:
                    final_status = task.get('status', status)
                    final_reason = task.get('reason') or reason
                    snapshot = checklist.read()
                updates['checklist'] = snapshot
                updates['checklist_digest'] = snapshot['digest']
                updates['checklist_path'] = self.write_checklist_artifact(snapshot)
                checklist_data = snapshot

        history = list(state.get('task_history') or [])
        if current_id:
            history.append({
                'sequence': len(history) + 1,
                'task_id': current_id,
                'status': final_status,
                'reason': final_reason,
            })

        next_id = queue[0] if queue else None
        msg_start = len(state.get('messages') or []) if message_count is None else int(message_count)
        updates.update({
            'task_queue': queue,
            'current_task_id': next_id,
            'task_history': history,
            'task_message_start': msg_start,
            'iteration': 0,
            'pending_tools': False,
            'response': None if next_id else (response or {'decision': 'completed', 'status': 'incomplete', 'reason': final_reason}),
        })
        if next_id is None and checklist_data is not None:
            unresolved = [t['task_id'] for t in checklist_data.get('tasks', []) if t.get('status') == 'unresolved']
            if unresolved:
                updates['result'] = {
                    'status': 'incomplete',
                    'reason': f'Unresolved DRC tasks: {", ".join(unresolved)}',
                }
        return updates

    def execute_agent(self, context, step):
        """Request a tool call or schema-valid response."""
        state = context.state
        queue_mode = state.get('task_queue') is not None and state.get('current_task_id') is not None
        iteration = state.get('iteration', 0)
        if iteration >= self.services.max_iterations:
            if queue_mode:
                reason = f'{context.task}/{step.id}: iteration limit reached for {state["current_task_id"]}'
                return self.advance_queue_item(state, 'unresolved', reason)
            reason = f'{context.task}/{step.id}: iteration limit reached'
            print(f'[ERROR] {reason}', file=sys.stderr)
            return {'failed': True, 'result': {'status': 'incomplete', 'reason': reason}}
        messages = list(state.get('messages', []))
        node_prompt = self.services.node_prompt(step)
        report = state.get('report') or {}
        target_cell = report.get('top_cell') if context.task == 'drc' else None
        try:
            tool_specs = self.agent_tool_specs(step, context.task, target_cell)
            response_schema = getattr(step, 'schema', None)
            requires_check = queue_mode and context.task == 'drc' and any(
                f'fix_{kind}' in step.tools and f'check_{kind}' in step.tools
                for kind in ('area', 'cover', 'width')
            )
            verified_pass = False
            if requires_check:
                task_id = state['current_task_id']
                candidate_digest = state.get('candidate_digest')
                check = (state.get('task_checks') or {}).get(task_id) or {}
                verified_pass = bool(
                    candidate_digest and check.get('status') == 'pass'
                    and check.get('candidate_digest') == candidate_digest
                )
                tool_specs, response_schema = self.drc_repair_schemas(
                    state, tool_specs, response_schema or self.services.workflow.response_schema,
                    verified_pass,
                )
            request_messages, _agent_payload = self.build_model_request(
                state, node_prompt=node_prompt, step_id=step.id, task_name=context.task,
            )
            message = self.services.chat(request_messages, tool_specs, response_schema=response_schema)
            self.last_conversation = [*request_messages, message]
            calls = message.get('tool_calls')
            updates = {'messages': [*messages, message], 'iteration': iteration + 1, 'pending_tools': bool(calls)}
            if not calls:
                response = json.loads(message['content'])
                schema = self.services.workflow.response_schema if response_schema is None else response_schema
                jsonschema.Draft202012Validator(schema).validate(response)
                if queue_mode:
                    agent_stopped = response.get('decision') == 'stop' or (
                        response.get('status') == 'incomplete' and response.get('decision') != 'completed'
                    )
                    if requires_check:
                        item_status = 'pass' if (verified_pass and not agent_stopped) else 'unresolved'
                        item_reason = response.get('reason', '') if (item_status == 'pass' or agent_stopped) else (
                            'Unverified completion: missing passing local check tied to current candidate'
                        )
                    else:
                        item_status = 'unresolved' if agent_stopped else 'pass'
                        item_reason = response.get('reason', '')
                    adv = self.advance_queue_item(
                        state,
                        item_status,
                        item_reason,
                        response=response,
                        message_count=len(messages) + 1,
                    )
                    return {**updates, **adv}
                if state.get('checklist') is not None and response.get('decision') == 'completed':
                    summary = Checklist.from_dict(state['checklist']).summary()
                    counts = summary['counts']
                    if counts['pending'] > 0 or counts['in_progress'] > 0 or summary['stale'] > 0:
                        updates['messages'].append({
                            'role': 'user',
                            'content': json.dumps({
                                'error': 'Cannot complete repair while pending, in_progress, or stale checklist tasks remain.',
                                'checklist_summary': summary,
                            }),
                        })
                        updates['response'] = None
                        return updates
                updates.update(response=response, iteration=0)
            return updates
        except (ValueError, json.JSONDecodeError, jsonschema.ValidationError) as exc:
            if not queue_mode:
                raise
            return self.advance_queue_item(state, 'unresolved', f'Invalid model response: {exc}')

    def agent_tool_specs(self, step, task, target_cell):
        specs = {name: spec.model_spec(name) for name, spec in step.tools.items()}
        if task != 'drc' or 'read_gds' not in specs:
            return list(specs.values())

        layout = getattr(self.services, 'layout', None)
        gds_data = getattr(layout, 'gds_data', None) or {}
        cells = (gds_data.get('database') or {}).get('structs') or {}
        if not cells:
            return list(specs.values())

        names = sorted(cells)
        if target_cell in cells:
            names.remove(target_cell)
            names.insert(0, target_cell)
        read_gds = deepcopy(specs['read_gds'])
        read_gds['function']['parameters']['properties']['cell_name']['enum'] = names
        specs['read_gds'] = read_gds
        return list(specs.values())

    def drc_repair_schemas(self, state, tool_specs, response_schema, verified_pass):
        """Expose operations for the current proposal, apply, or verification stage."""
        task_id = state['current_task_id']
        response_schema = deepcopy(response_schema)
        review_context = ((state.get('review') or {}).get('context') or {})
        review_repair = (state.get('review') or {}).get('decision') == 'continue' and review_context.get('task_id') == task_id
        verified_pass = verified_pass and not review_repair
        response_schema['properties']['decision']['enum'] = ['completed'] if verified_pass else ['stop']
        response_schema['properties']['status']['enum'] = ['verified'] if verified_pass else ['incomplete']
        if verified_pass:
            return [], response_schema

        specs = {spec['function']['name']: spec for spec in deepcopy(tool_specs)}
        transaction = (state.get('task_transactions') or {}).get(task_id)
        if transaction:
            name = f"check_{transaction['kind']}"
            spec = specs[name]
            spec['function']['parameters']['properties']['task_id']['const'] = task_id
            return [spec], False

        proposals = state.get('proposals') or {}
        candidate_digest = state.get('candidate_digest')
        if proposals and not candidate_digest:
            # Initial proposals may be based on the original input before a candidate exists.
            candidate_digest = self.services.candidate_digest(Path(self.services.layout.gds_path))
        proposal_ids = {}
        for proposal_id, proposal in proposals.items():
            if (not review_repair and candidate_digest and proposal.get('task_id') == task_id
                    and proposal.get('candidate_digest') == candidate_digest):
                proposal_ids.setdefault(proposal['kind'], []).append(proposal_id)

        repairs = []
        propose_fields = {
            'area': (('polygon_id',), ('minimum_area', 'cover_context')),
            'cover': (('inner_id', 'groups', 'minimum_enclosure'), ()),
            'width': (('polygon_id', 'minimum_width'), ('preserve_cover', 'cover_context', 'resize_mode')),
        }
        for kind, (required_fields, optional_fields) in propose_fields.items():
            spec = specs.get(f'fix_{kind}')
            if spec is None or (proposal_ids and kind not in proposal_ids):
                continue
            applying = kind in proposal_ids
            parameters = spec['function']['parameters']
            properties = parameters['properties']
            required = ['task_id', 'action', *(['proposal_id'] if applying else required_fields)]
            allowed = required if applying else ['task_id', 'action', *required_fields, *optional_fields]
            parameters['properties'] = {key: properties[key] for key in allowed if key in properties}
            parameters['required'] = required
            properties['task_id']['const'] = task_id
            properties['action']['enum'] = ['apply'] if applying else ['propose']
            if applying:
                properties['proposal_id']['enum'] = proposal_ids[kind]
            repairs.append(spec)
        return repairs, response_schema

    def build_model_request(
        self,
        state: dict,
        node_prompt: str = '',
        step_id: str = 'repair',
        task_name: str = 'drc',
    ) -> tuple[list[dict], dict]:
        """Build model-facing messages and compact payload without mutating stored state history."""
        messages = list(state.get('messages') or [])
        queue_mode = state.get('task_queue') is not None and state.get('current_task_id') is not None

        builder = getattr(getattr(self.services, 'workflow', None), 'request_builder', None)
        if builder is not None:
            focused = None
            if task_name == 'drc' and state.get('current_task_id') is not None:
                focused = self.build_active_task_context(state, state['current_task_id'])
            request = builder(state, node_prompt, self.services, focused)
            if not isinstance(request, dict) or set(request) != {'system', 'user'}:
                raise ValueError('Request builder must return system and user fields')
            if not isinstance(request['system'], str) or not isinstance(request['user'], dict):
                raise ValueError('Request builder must return system text and a user object')
            payload = request['user']
            history = self._select_task_messages(messages, state.get('task_message_start', 0), state.get('current_task_id'))
            return [
                {'role': 'system', 'content': request['system']},
                {'role': 'user', 'content': json.dumps(payload, allow_nan=False)},
                *(message for message in history if message.get('role') in ('assistant', 'tool')),
            ], payload

        agent_payload = {}
        if node_prompt:
            agent_payload[f'{step_id}_instruction'] = node_prompt

        drc_context = state.get('drc_context') or {}
        goal = drc_context.get('goal', self.services.config.get('goal', ''))
        constraints = (drc_context.get('knowledge') or {}).get('constraints', [])

        if queue_mode:
            agent_payload['goal'] = goal
            agent_payload['constraints'] = list(constraints)
            agent_payload['current_task_id'] = state['current_task_id']
            agent_payload['active_task'] = self.build_active_task_context(state, state['current_task_id'])
            agent_payload['task_history'] = list(state.get('task_history') or [])
            request_messages = self._select_task_messages(messages, state.get('task_message_start', 0), state.get('current_task_id'))
        else:
            if state.get('checklist') is not None:
                agent_payload['current_checklist'] = state['checklist']
                agent_payload['goal'] = goal
            if state.get('current_task_id') is not None:
                agent_payload['current_task_id'] = state['current_task_id']
                agent_payload['active_task'] = self.build_active_task_context(state, state['current_task_id'])
                agent_payload['task_history'] = list(state.get('task_history') or [])
            request_messages = list(messages)

        report = state.get('report') or {}
        target_cell = report.get('top_cell') if task_name == 'drc' else None
        if target_cell:
            agent_payload['target_cell'] = target_cell
        if agent_payload:
            request_messages.append({
                'role': 'user',
                'content': json.dumps(agent_payload),
            })
        return request_messages, agent_payload

    def _select_task_messages(self, messages: list[dict], task_message_start: int, task_id: str | None = None) -> list[dict]:
        """Keep shared system instructions and paired assistant/tool messages for the current item."""
        system_msgs = [m for m in messages if m.get('role') == 'system']
        start_idx = max(0, int(task_message_start or 0))
        item_msgs = [m for m in messages[start_idx:] if m.get('role') != 'system']
        for message in messages:
            if message.get('role') != 'user' or message in item_msgs:
                continue
            try:
                content = json.loads(message.get('content', ''))
            except (TypeError, ValueError):
                continue
            if (isinstance(content, dict) and 'user_review' in content
                    and (task_id is None or content.get('task_id') == task_id)):
                item_msgs.insert(0, message)

        available_tool_ids = {
            m.get('tool_call_id')
            for m in item_msgs
            if m.get('role') == 'tool' and m.get('tool_call_id')
        }
        included_call_ids = set()
        selected_item_msgs = []
        for msg in item_msgs:
            role = msg.get('role')
            if role == 'assistant' and msg.get('tool_calls'):
                call_ids = [
                    call.get('id')
                    for call in msg['tool_calls']
                    if isinstance(call, dict) and call.get('id')
                ]
                if call_ids and all(cid in available_tool_ids for cid in call_ids):
                    included_call_ids.update(call_ids)
                    selected_item_msgs.append(msg)
                continue
            if role == 'tool':
                if msg.get('tool_call_id') in included_call_ids:
                    selected_item_msgs.append(msg)
                continue
            selected_item_msgs.append(msg)
        return [*system_msgs, *selected_item_msgs]

    def build_active_task_context(self, state, task_id: str) -> dict:
        """Build per-marker context with DBU-converted coordinates, rule evidence, and bounded region geometry."""
        checklist = state.get('checklist') or {}
        task = next((t for t in checklist.get('tasks', []) if t.get('task_id') == task_id), {})
        report = state.get('report') or {}
        rule_name = task.get('rule')
        vid = task.get('violation_id')
        occ = task.get('occurrence', 0)

        marker = self._resolve_exact_marker(report, rule_name, vid, occ)
        scale = report.get('dbu_per_user_unit')
        valid_scale = isinstance(scale, (int, float)) and not isinstance(scale, bool) and scale > 0
        points_dbu = marker.get('points_dbu') if isinstance(marker, dict) else None
        points_user = None
        if isinstance(points_dbu, list) and points_dbu and valid_scale:
            points_user = [[pt[0] / scale, pt[1] / scale] for pt in points_dbu]

        margin_dbu = self._configured_margin_dbu()
        margin_user = (margin_dbu / scale) if valid_scale else None
        bounds_user = self._marker_bounds_user(points_user, margin_user) if (points_user and margin_user is not None) else None

        rule_evidence = self._build_rule_evidence(task)
        rule_description = next(
            (rule.get('description') for rule in report.get('rules', []) or [] if rule.get('name') == rule_name),
            None,
        )
        drc_available = bool(marker is not None and valid_scale and rule_evidence.get('found'))
        drc_reasons = []
        if marker is None:
            drc_reasons.append('Missing violation marker geometry')
        if not valid_scale:
            drc_reasons.append('Missing or invalid dbu_per_user_unit')
        if not rule_evidence.get('found'):
            drc_reasons.append(rule_evidence.get('reason') or 'Unavailable rule evidence')

        layout_section = self._build_layout_region_context(state, bounds_user, margin_user, margin_dbu)

        return {
            'task': {
                'task_id': task_id,
                'rule': rule_name,
                'violation_id': vid,
                'occurrence': occ,
                'status': task.get('status'),
            },
            'drc': {
                'rule': rule_name,
                'dbu_per_user_unit': scale,
                'marker_dbu': points_dbu,
                'marker_user': points_user,
                'rule_evidence': rule_evidence,
                'rule_description': rule_description,
                'status': 'available' if drc_available else 'unavailable',
                'reason': '; '.join(drc_reasons),
            },
            'layout': layout_section,
        }

    def _resolve_exact_marker(self, report: dict, rule_name, vid, occ: int):
        """Resolve exact marker occurrence matching rule + violation_id + occurrence."""
        if not rule_name or vid is None:
            return None
        for rule in report.get('rules') or []:
            if rule.get('name') != rule_name:
                continue
            seen = 0
            for geom in rule.get('geometry') or []:
                if geom.get('id') == vid:
                    if seen == occ:
                        return geom
                    seen += 1
        return None

    def _configured_margin_dbu(self) -> float:
        """Read nonnegative drc_context_margin_dbu from service configuration, defaulting to 0.0."""
        config = getattr(self.services, 'config', None) or {}
        raw = config.get('drc_context_margin_dbu', 0.0)
        if raw is None:
            return 0.0
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise ValueError(f'Invalid drc_context_margin_dbu: {raw!r}')
        return float(raw)

    def _marker_bounds_user(self, points_user: list, margin_user: float) -> list[float]:
        """Compute [min_x, min_y, max_x, max_y] user bounds from marker points and margin."""
        xs = [float(pt[0]) for pt in points_user]
        ys = [float(pt[1]) for pt in points_user]
        return [
            round(min(xs) - margin_user, 12),
            round(min(ys) - margin_user, 12),
            round(max(xs) + margin_user, 12),
            round(max(ys) + margin_user, 12),
        ]

    def _build_rule_evidence(self, task: dict) -> dict:
        """Resolve rule text and layer mapping from configured decks and layer maps."""
        rule_name = str(task.get('rule') or '').strip()
        if not rule_name:
            return {
                'rule': None,
                'found': False,
                'deck_file': None,
                'line_start': None,
                'line_end': None,
                'rule_text': '',
                'layer_mapping': {},
                'layer_mapping_provenance': {},
                'mapping_diagnostics': {'missing_sources': [], 'ambiguous_layers': {}, 'non_drawing_entries': {}},
                'reason': 'Missing rule name for active task',
            }
        sources = getattr(self.services, 'input_sources', None) or {}
        config = getattr(self.services, 'config', None) or {}
        decks = sources.get('decks') or config.get('drc_files') or []
        if isinstance(decks, (str, Path)):
            decks = [decks]
        layer_maps = sources.get('layer_maps') or config.get('layer_map_files') or []
        if isinstance(layer_maps, (str, Path)):
            layer_maps = [layer_maps]
        return RuleEvidence().run({'rule': rule_name}, deck_paths=list(decks), layer_maps=list(layer_maps))

    def _build_layout_region_context(self, state: dict, bounds_user, margin_user, margin_dbu: float) -> dict:
        """Query live candidate layout geometry for bounds_user or return explicit unavailable status."""
        drc_context = state.get('drc_context') or {}
        report = state.get('report') or {}
        target_cell = drc_context.get('target_cell') or report.get('top_cell')
        candidate_path = state.get('candidate_path')
        candidate_digest = state.get('candidate_digest')
        search_region = {
            'bounds_user': bounds_user,
            'margin_user': margin_user,
            'margin_dbu': margin_dbu,
        }
        layout = getattr(self.services, 'layout', None)
        if bounds_user is None or not target_cell or layout is None or not hasattr(layout, 'get_cell'):
            reason = (
                'Missing or invalid marker bounds/scale'
                if bounds_user is None
                else 'Missing target cell or layout service'
            )
            return {
                'cell': target_cell,
                'candidate_path': candidate_path,
                'candidate_digest': candidate_digest,
                'search_region': search_region,
                'polygons': [],
                'references_in_region': [],
                'paths_in_region': [],
                'complete': False,
                'status': 'unavailable',
                'reason': reason,
            }
        region = ViolationGeometry().query_region(
            layout,
            target_cell,
            bounds_user,
            candidate_digest=candidate_digest,
        )
        return {
            'cell': region['cell'],
            'candidate_path': candidate_path,
            'candidate_digest': region['candidate_digest'],
            'search_region': search_region,
            'polygons': region['polygons'],
            'references_in_region': region['references_in_region'],
            'paths_in_region': region['paths_in_region'],
            'complete': region['complete'],
            'status': 'available',
            'reason': region['reason'],
        }

    def refresh_candidate_context(self, context, updates: dict) -> None:
        """Refresh live cell geometry in drc_context when a tool produces a new candidate digest."""
        new_digest = updates.get('candidate_digest')
        if not new_digest or new_digest == context.state.get('candidate_digest'):
            return
        drc_context = dict(updates.get('drc_context') or context.state.get('drc_context') or {})
        report = context.state.get('report') or {}
        target_cell = drc_context.get('target_cell') or report.get('top_cell') or context.inputs.get('cell')
        layout = getattr(context.services, 'layout', None)
        if target_cell and layout is not None and hasattr(layout, 'read_gds'):
            drc_context['gds'] = layout.read_gds(cell_name=target_cell)
            drc_context['target_cell'] = target_cell
        drc_context['candidate_path'] = updates.get('candidate_path', context.state.get('candidate_path'))
        drc_context['candidate_digest'] = new_digest
        updates['drc_context'] = drc_context

    def execute_tools(self, context, step):
        """Validate calls before dispatch and correlate each response."""
        messages = list(context.state['messages'])
        calls = messages[-1]['tool_calls']
        if not isinstance(calls, list):
            raise ValueError('Expected a list of tool calls')
        ids = [call.get('id') if isinstance(call, dict) else None for call in calls]
        if any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
            raise ValueError('Missing or duplicate tool call ID')
        updates = {}
        trace_inst = getattr(self.services, 'conversation_trace', None)
        for call, call_id in zip(calls, ids):
            name = arguments = result_payload = error = None
            tool_op = None
            try:
                name = call['function']['name']
                arguments = call['function']['arguments']
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                if trace_inst:
                    tool_op = trace_inst.start_operation(
                        'tool_call',
                        {'name': name, 'call_id': call_id, 'arguments': arguments},
                    )
                spec = step.tools[name]
                jsonschema.Draft202012Validator(spec.parameters).validate(arguments)
                if self.trace:
                    self.trace.tool(name, step.id)
                call_context = TaskContext(context.task, context.inputs, {**context.state, **updates}, self.services)
                result = spec.function(call_context, **arguments)
                if isinstance(result, ToolResult):
                    result_payload = result.payload
                    payload = json.dumps(result_payload, allow_nan=False)
                    json.dumps(result.updates, allow_nan=False)
                    updates.update(result.updates)
                    if trace_inst and tool_op:
                        trace_inst.finish_operation(tool_op, 'tool_call', status='completed', outputs={'result': result.payload})
                else:
                    result_payload = result
                    payload = json.dumps(result_payload, allow_nan=False)
                    if trace_inst and tool_op:
                        trace_inst.finish_operation(tool_op, 'tool_call', status='completed', outputs={'result': result})
                invalid_reason = self.invalid_tool_result(name, result_payload)
                if invalid_reason:
                    error = f'{context.task}/{step.id}/{name}: invalid result: {invalid_reason}'
                    if context.state.get('task_queue') is not None and context.state.get('current_task_id') is not None:
                        messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': payload})
                        self.last_conversation = list(messages)
                        adv = self.advance_queue_item({**context.state, **updates}, 'unresolved', error, message_count=len(messages))
                        return {**updates, **adv, 'messages': messages}
                    updates['tool_review'] = {
                        'tool': name, 'tool_call_id': call_id, 'arguments': arguments,
                        'task_id': arguments.get('task_id') if isinstance(arguments, dict) else None,
                        'result': result_payload, 'error': error,
                        'candidate_digest': updates.get('candidate_digest', context.state.get('candidate_digest')),
                    }
            except Exception as exc:
                tool_name = name if isinstance(name, str) and name in step.tools else 'unknown tool'
                detail = str(exc).strip()
                msg = f': {detail}' if detail else ''
                error = f'{context.task}/{step.id}/{tool_name}: {type(exc).__name__}{msg}'
                print(f'[ERROR] {error}', file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                payload = json.dumps({'error': error})
                if trace_inst and tool_op:
                    trace_inst.finish_operation(tool_op, 'tool_call', status='failed', error=exc, outputs={'error': error})
                if context.state.get('task_queue') is not None and context.state.get('current_task_id') is not None:
                    messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': payload})
                    self.last_conversation = list(messages)
                    adv = self.advance_queue_item({**context.state, **updates}, 'unresolved', error, message_count=len(messages))
                    return {**updates, **adv, 'messages': messages}
                call_args = arguments if isinstance(arguments, dict) else {}
                updates['tool_review'] = {
                    'tool': tool_name,
                    'tool_call_id': call_id,
                    'arguments': call_args,
                    'task_id': call_args.get('task_id'),
                    'error': error,
                    'candidate_digest': context.state.get('candidate_digest'),
                }
            messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': payload})
            if trace_inst:
                trace_inst.append({
                    'type': 'tool_result',
                    'message': {'role': 'tool', 'tool_call_id': call_id, 'content': payload},
                })
            self.last_conversation = list(messages)
            if updates.get('tool_review'):
                if trace_inst:
                    trace_inst.append({'type': 'tool_review_requested', **updates['tool_review']})
                break
        if updates.get('checklist'):
            try:
                updates['checklist_path'] = self.write_checklist_artifact(updates['checklist'])
            except (OSError, ValueError) as exc:
                reason = f'{context.task}/{step.id}: cannot write checklist.json: {exc}'
                print(f'[ERROR] {reason}', file=sys.stderr)
                updates.update(failed=True, result={'status': 'incomplete', 'reason': reason})
        self.refresh_candidate_context(context, updates)
        return {**updates, 'messages': messages, 'pending_tools': False}

    def save_conversation(self, messages):
        """Keep in-memory reference to the latest conversation messages."""
        self.last_conversation = list(messages)

    def node_name(step_id):
        """Keep workflow identifiers that overlap LangGraph state channels."""
        return f'step--{step_id}' if step_id in FagentState.__annotations__ else step_id

    node_name = staticmethod(node_name)

    def configured_destination(self, state, step):
        context = TaskContext(
            self.services.workflow.task, state.get('inputs', {}),
            {**state.get('task_state', {}), **state}, self.services,
        )
        key = step.router(context)
        if not isinstance(key, str) or key not in step.routes:
            raise ValueError(f'{step.id}: unknown route key {key!r}')
        return self.node_name(step.routes[key])

    def route_task_step(self, state, step):
        """Follow the successful route or terminate after a failure."""
        destination = self.node_name(step.next)
        if state.get('failed'):
            destination = 'END'
        elif state.get('pending_tools'):
            destination = f'{step.id}--tools'
        elif getattr(step, 'router', None):
            destination = self.configured_destination(state, step)
        elif step.id != 'save' and step.action not in ('agent', 'review') and state.get('task_queue') is not None and len(state['task_queue']) == 0:
            has_save = any(getattr(s, 'id', None) == 'save' for s in (getattr(self.services.workflow, 'steps', None) or ()))
            destination = self.node_name('save') if (has_save) else 'END'
        elif step.action == 'agent' and state.get('response') is None:
            destination = self.node_name(step.id)
        elif step.action == 'review':
            decision = state['review']['decision']
            if decision == 'unclear':
                destination = self.node_name(step.id)
            elif decision == 'continue':
                destination = self.node_name(step.continue_to)
        return self.trace_route(self.node_name(step.id), step.id, state, destination)

    def route_task_tools(self, state, step):
        """Return to caller or surface a tool error for human review."""
        if state.get('failed'):
            destination = 'END'
        elif state.get('tool_review'):
            destination = f'{step.id}--tool-review'
        elif getattr(step, 'router', None):
            destination = self.configured_destination(state, step)
        elif state.get('task_queue') is not None and state.get('current_task_id') is None:
            destination = self.node_name(step.next)
        else:
            destination = self.node_name(step.id)
        return self.trace_route(f'{step.id}--tools', step.id, state, destination)

    @staticmethod
    def invalid_tool_result(name, result):
        if not isinstance(result, dict):
            return 'tool result must be an object'
        if result.get('status') in {'unknown', 'unsupported', 'ambiguous', 'error', 'failed'}:
            return result.get('reason') or f"status={result['status']}"
        return None

    def review_tool(self, state):
        review = state['tool_review']
        decision = interrupt({'kind': 'tool_review', **review})
        # GraphRunner's interactive prompt wraps entered text as
        # {"feedback": "..."}; API callers may send a decision object directly.
        if isinstance(decision, dict) and isinstance(decision.get('feedback'), str):
            feedback = decision['feedback'].strip()
            try:
                parsed = json.loads(feedback)
            except (TypeError, ValueError):
                parsed = None
            decision = parsed if isinstance(parsed, dict) else {'action': feedback}
        action = decision.get('action') if isinstance(decision, dict) else None
        reason = decision.get('reason', '') if isinstance(decision, dict) else ''
        if action == 'stop':
            updates = {'failed': True, 'result': {'status': 'incomplete', 'reason': review['error']}}
        elif action in {'continue', 'skip_task'}:
            if action == 'skip_task' and not reason.strip():
                reason = review.get('error') or review.get('result', {}).get('reason') or 'Skipped after tool review'
            message = {'role': 'user', 'content': json.dumps({'tool_review': review, 'decision': decision})}
            updates = {'messages': [*state['messages'], message]}
            if action == 'skip_task' and review.get('task_id'):
                checklist = Checklist.from_dict(state['checklist'])
                snapshot = checklist.update(task_id=review['task_id'], status='unresolved', reason=reason)
                checklist_path = self.write_checklist_artifact(snapshot)
                updates.update(
                    checklist=snapshot,
                    checklist_digest=snapshot['digest'],
                    checklist_path=checklist_path,
                )
        else:
            return {}
        trace = getattr(self.services, 'conversation_trace', None)
        if trace:
            trace.append({'type': 'tool_review_decision', 'tool_call_id': review['tool_call_id'], 'action': action, 'reason': reason})
        return {**updates, 'tool_review': None}

    def route_tool_review(self, state, step):
        if state.get('failed'):
            return 'END'
        if state.get('tool_review'):
            return f'{step.id}--tool-review'
        return self.node_name(step.id)

    def trace_route(self, node, step, state, destination):
        """Report the route after the graph chooses it."""
        if self.trace:
            if state.get('failed'):
                self.trace.node_end(node, step, 'failed')
            else:
                self.trace.node_end(node, step, 'completed', destination)
        if hasattr(self.services, 'conversation_trace') and self.services.conversation_trace:
            self.services.conversation_trace.append({
                'type': 'route_transition',
                'node': node,
                'step_id': step,
                'destination': str(destination),
                'failed': bool(state.get('failed')),
                'timestamp': datetime.now(timezone.utc).isoformat(),
            })
        return END if destination == 'END' else destination

    def review(self, state):
        validation = state.get('validation') or {}
        response = state.get('response')
        if isinstance(response, dict) and 'status' in response:
            response = {**response, 'status': 'incomplete'}
        result = {
            'status': 'incomplete',
            'reason': state.get('repair_error') or validation.get('reason') or 'No verified tool evidence',
            'response': response,
            'candidate': state.get('candidate_path'),
            'checklist': state.get('checklist'),
        }
        trace_inst = getattr(self.services, 'conversation_trace', None)
        review_op = None
        if trace_inst:
            review_op = trace_inst.start_operation(
                'human_review',
                {'candidate': state.get('candidate_path'), 'validation': validation},
            )
        raw_feedback = interrupt({'result': result, 'validation': validation, 'checklist': state.get('checklist')})
        envelope = raw_feedback if isinstance(raw_feedback, dict) else {'feedback': raw_feedback}
        feedback = next((envelope.get(key) for key in ('feedback', 'review', 'text')
                         if isinstance(envelope.get(key), str)), None)
        messages = list(state.get('messages', []))
        explicit_stop = isinstance(envelope, dict) and (envelope.get('decision') == 'stop' or envelope.get('action') == 'stop')
        decision = 'stop' if explicit_stop or not isinstance(feedback, str) or not feedback.strip() else 'continue'
        if decision == 'continue':
            try:
                interpret = getattr(self.services, 'interpret_feedback', None)
                interpreted = interpret(feedback, state) if callable(interpret) else None
                if ((isinstance(interpreted, dict) and interpreted.get('decision') == 'stop')
                        or (isinstance(interpreted, str) and interpreted.strip().lower() == 'stop')):
                    decision = 'stop'
            except (ValueError, RuntimeError, KeyError, TypeError, OSError, ImportError):
                pass
        task_id = state.get('current_task_id')
        if not task_id:
            history = state.get('task_history') or []
            task_id = next((item.get('task_id') for item in reversed(history) if item.get('task_id')), None)
        if decision == 'continue':
            active_task = self.build_active_task_context(state, task_id) if task_id else None
            if active_task:
                active_task.setdefault(
                    'layer_map', active_task.get('drc', {}).get('rule_evidence', {}).get('layer_mapping') or {},
                )
            last_tool = self._last_tool_context(state)
            review_context = {
                'task_id': task_id,
                'user_review': feedback.strip(),
                'last_tool': last_tool,
                'candidate_path': state.get('candidate_path'),
                'candidate_digest': state.get('candidate_digest'),
                'task_checks': state.get('task_checks') or {},
                'active_task': active_task,
            }
            message_start = len(messages)
            messages.append({'role': 'user', 'content': json.dumps(review_context, allow_nan=False)})
        else:
            review_context = None
            message_start = state.get('task_message_start', 0)
        authorized = (
            decision == 'stop'
            and isinstance(feedback, str)
            and bool(feedback.strip())
            and envelope.get('storage_consent') is True
            and getattr(self.services.workflow, 'knowledge_review', False)
        )
        review = {
            'feedback': feedback,
            'context': review_context,
            'decision': decision,
            'authorized': authorized,
            'storage_consent': authorized,
            'consent_source': 'human_review' if authorized else None,
        }
        if trace_inst and review_op:
            trace_inst.finish_operation(
                review_op,
                'human_review',
                status='completed',
                outputs={'feedback': feedback, 'decision': decision, 'authorized': authorized},
            )
        update = {'result': result, 'messages': messages, 'review': review, 'save_requested': authorized}
        if decision == 'continue':
            queue = list(state.get('task_queue') or [])
            current_task_id = state.get('current_task_id')
            current_task_id = current_task_id or task_id
            if current_task_id and current_task_id not in queue:
                queue.insert(0, current_task_id)
            update.update(
                current_task_id=current_task_id,
                task_queue=queue,
                task_message_start=message_start,
                iteration=0,
                repair_error=None,
                response=None,
                pending_tools=False,
                failed=False,
            )
        return update

    def _last_tool_context(self, state):
        """Return the latest tool call with parsed arguments and result or error."""
        tool_review = state.get('tool_review')
        if tool_review:
            return {
                'name': tool_review.get('tool'), 'arguments': tool_review.get('arguments'),
                'result': tool_review.get('result'), 'error': tool_review.get('error'),
            }
        messages = state.get('messages') or []
        for tool_message in reversed(messages):
            if tool_message.get('role') != 'tool' or not tool_message.get('tool_call_id'):
                continue
            call_id = tool_message['tool_call_id']
            assistant = next((message for message in reversed(messages)
                              if message.get('role') == 'assistant'
                              and any(call.get('id') == call_id for call in message.get('tool_calls') or [])), None)
            if not assistant:
                continue
            call = next(call for call in assistant.get('tool_calls') or [] if call.get('id') == call_id)
            arguments = call.get('function', {}).get('arguments')
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    pass
            try:
                outcome = json.loads(tool_message.get('content', '{}'))
            except (TypeError, ValueError):
                outcome = tool_message.get('content')
            return {'name': call.get('function', {}).get('name'), 'arguments': arguments, 'result': outcome,
                    'error': outcome.get('error') if isinstance(outcome, dict) else None}
        return None
