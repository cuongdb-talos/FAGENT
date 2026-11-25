"""Deterministic workflow stages and constrained model tool calls."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from html import escape
import inspect
import json
from pathlib import Path
import re
import sys
import tempfile
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
from ..tasks.drc.checklist import Checklist


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
            if step.action == 'plan':
                res = self.execute_plan(context, step)
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

    def execute_plan(self, context, step):
        """Construct deterministic planning context and obtain model plan."""
        state = context.state
        messages = list(state.get('messages', []))
        replan_count = state.get('replan_count', 0)
        replan_ctx = state.get('replan_context')
        if replan_ctx:
            replan_count += 1
            if replan_count >= self.services.max_iterations:
                reason = f'{context.task}/{step.id}: replanning iteration limit reached'
                print(f'[ERROR] {reason}', file=sys.stderr)
                return {'failed': True, 'replan_count': replan_count, 'result': {'status': 'incomplete', 'reason': reason}}

        checklist_data = state.get('checklist')
        checklist_obj = Checklist.from_dict(checklist_data) if checklist_data else None
        active_tasks = [t for t in checklist_obj.tasks if t.get('status') in ('pending', 'in_progress', 'stale')] if checklist_obj else []

        if checklist_obj and len(active_tasks) == 0:
            plan = {'summary': 'No active violations', 'steps': []}
            history = self.version_plan(state, plan)
            self.write_plan_artifact(history)
            return {
                'plan': plan,
                'plan_history': history,
                'replan_count': replan_count,
                'replan_context': None,
                'iteration': 0,
            }

        report = state.get('report')
        if not isinstance(report, dict) and hasattr(self.services, 'drc_report'):
            report = self.services.drc_report

        next_agent = next((node for node in self.services.workflow.steps if node.id == step.next), None)
        if next_agent is None or next_agent.action != 'agent' or not next_agent.tools:
            raise ValueError(f'Plan target {step.next!r} must be an agent step with tools')
        allowed_tools = {name: spec.description for name, spec in next_agent.tools.items()}
        report = report if isinstance(report, dict) else {}
        active_rules = list(dict.fromkeys(task['rule'] for task in active_tasks if task.get('rule')))
        existing_refs = list(state.get('drc_context', {}).get('knowledge', {}).get('references') or [])
        rag_refs, unavailable_evidence = self.retrieve_plan_evidence(active_rules, existing_refs)

        goal = state.get('drc_context', {}).get('goal', self.services.config.get('goal', ''))
        constraints = state.get('drc_context', {}).get('knowledge', {}).get('constraints', [])
        instruction = getattr(step, 'prompt', None) or self.services.node_prompt(step)

        plan_payload = {
            f'{step.id}_instruction': instruction,
            'goal': goal,
            'constraints': constraints,
            'report_source': report.get('source'),
            'dbu_per_user_unit': report.get('dbu_per_user_unit'),
            'active_tasks': self.plan_task_context(active_tasks, report),
            'allowed_tools': allowed_tools,
            'references': rag_refs,
            'unavailable_evidence': unavailable_evidence,
        }
        if replan_ctx:
            plan_payload['replan_context'] = replan_ctx
            plan_payload['candidate_digest'] = state.get('candidate_digest')
            plan_payload['candidate_path'] = state.get('candidate_path')
            plan_payload['current_plan'] = state.get('plan')

        sys_msg = next((m for m in messages if m.get('role') == 'system'), None)
        plan_request = [sys_msg] if sys_msg else []
        plan_request.append({'role': 'user', 'content': json.dumps(plan_payload)})

        schema = deepcopy(step.schema)
        if active_tasks:
            steps_schema = schema['properties']['steps']
            steps_schema['minItems'] = max(1, steps_schema.get('minItems', 0))
            fields = steps_schema['items']['properties']
            fields['tool']['enum'] = list(allowed_tools)
            fields['task_id']['enum'] = [task['task_id'] for task in active_tasks]

        max_retries = max(0, int(self.services.config.get('plan_validation_retries', 2)))
        plan = None
        message = None
        for attempt in range(max_retries + 1):
            message = self.services.chat(plan_request, [], response_schema=schema)
            self.last_conversation = [*plan_request, message]
            if message.get('tool_calls'):
                raise ValueError('Plan step cannot invoke tools')

            plan = json.loads(message['content'])
            if active_tasks and isinstance(plan, dict) and plan.get('steps') == []:
                reason = 'Empty plan returned while active checklist tasks remain'
                print(f'[ERROR] {reason}', file=sys.stderr)
                return {
                    'failed': True,
                    'replan_count': replan_count,
                    'replan_context': None,
                    'result': {'status': 'incomplete', 'reason': reason},
                }

            jsonschema.Draft202012Validator(schema).validate(plan)
            missing_tasks, rule_mismatches = self.validate_plan_coverage(active_tasks, plan)
            if not missing_tasks and not rule_mismatches:
                break

            issues = []
            if missing_tasks:
                issues.append(f'missing active tasks: {missing_tasks}')
            if rule_mismatches:
                issues.append(f'rule mismatches: {rule_mismatches}')
            reason = f'Plan validation failed ({"; ".join(issues)})'
            if attempt >= max_retries:
                print(f'[ERROR] {reason}', file=sys.stderr)
                return {
                    'failed': True,
                    'replan_count': replan_count,
                    'replan_context': None,
                    'result': {'status': 'incomplete', 'reason': reason},
                }
            plan_request = [
                *plan_request,
                message,
                {
                    'role': 'user',
                    'content': json.dumps({
                        'validation_error': reason,
                        'missing_task_ids': missing_tasks,
                        'rule_mismatches': rule_mismatches,
                        'instruction': (
                            f'You MUST include at least one step in "steps" for EVERY missing task_id: {missing_tasks}. '
                            'Keep your existing valid steps and add steps for each missing task_id.'
                        ),
                    }),
                },
            ]

        history = self.version_plan(state, plan)
        self.write_plan_artifact(history)
        return {
            'messages': [*messages, message],
            'plan': plan,
            'plan_history': history,
            'replan_count': replan_count,
            'replan_context': None,
            'iteration': 0,
        }

    def retrieve_plan_evidence(self, active_rules, existing_refs):
        """Deduplicate references, retrieve missing per-rule evidence, and report unavailable rules."""
        active_set = set(active_rules)
        deduped = []
        seen = set()
        for ref in existing_refs:
            if not isinstance(ref, dict):
                continue
            ref_rule = ref.get('rule')
            if ref_rule and active_set and ref_rule not in active_set:
                continue
            key = (ref.get('id'), ref_rule, ref.get('source'), ref.get('line'), ref.get('content'))
            if key not in seen:
                seen.add(key)
                deduped.append(ref)

        covered_rules = {ref.get('rule') for ref in deduped if ref.get('rule')}
        unavailable = []
        rag = getattr(self.services, 'rag', None)
        can_retrieve = rag is not None and hasattr(rag, 'retrieve')

        for rule_name in active_rules:
            if rule_name in covered_rules:
                continue
            if can_retrieve:
                try:
                    fetched = self.retrieve_for_rule(rag, rule_name)
                except Exception as exc:
                    print(f'[ERROR] RAG retrieval failed for rule {rule_name}: {exc}', file=sys.stderr)
                    unavailable.append({'rule': rule_name, 'status': 'retrieval_failed', 'reason': str(exc)})
                    continue
                for ref in fetched or []:
                    if not isinstance(ref, dict) or ref.get('rule') != rule_name:
                        continue
                    key = (ref.get('id'), ref.get('rule'), ref.get('source'), ref.get('line'), ref.get('content'))
                    if key not in seen:
                        seen.add(key)
                        deduped.append(ref)
                    covered_rules.add(rule_name)
            if rule_name not in covered_rules:
                unavailable.append({
                    'rule': rule_name,
                    'status': 'unavailable',
                    'reason': 'No exact rule reference found in RAG',
                })

        return deduped, unavailable

    def retrieve_for_rule(self, rag, rule_name):
        """Invoke configured RAG retriever with signature-appropriate arguments."""
        limit = max(1, int(self.services.config.get('rag_limit', 5)))
        params = inspect.signature(rag.retrieve).parameters.values()
        positional = [
            p for p in params
            if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        has_varargs = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
        if has_varargs or len(positional) >= 3:
            return rag.retrieve(rule_name, [rule_name], limit)
        return rag.retrieve(rule_name)

    @staticmethod
    def validate_plan_coverage(active_tasks, plan):
        """Verify every active task is covered and structured step rules match their task."""
        active_ids = [task['task_id'] for task in active_tasks]
        task_rules = {task['task_id']: task.get('rule') for task in active_tasks}
        steps = plan.get('steps', []) if isinstance(plan, dict) else []
        planned_ids = {step.get('task_id') for step in steps if isinstance(step, dict)}
        missing_tasks = [task_id for task_id in active_ids if task_id not in planned_ids]
        rule_mismatches = [
            {'task_id': step.get('task_id'), 'expected_rule': task_rules.get(step.get('task_id')), 'step_rule': step.get('rule')}
            for step in steps
            if isinstance(step, dict) and 'rule' in step and step.get('rule') != task_rules.get(step.get('task_id'))
        ]
        return missing_tasks, rule_mismatches

    @staticmethod
    def version_plan(state, plan):
        """Build a new active version while preserving earlier step outcomes."""
        history = [
            {**version, 'status': 'superseded'} if version['status'] == 'active' else version
            for version in state.get('plan_history', [])
        ]
        history.append({
            'version': len(history) + 1,
            'summary': plan['summary'],
            'status': 'active',
            'steps': [
                {**step, 'step_id': index, 'status': 'pending', 'call_id': None, 'outcome': None}
                for index, step in enumerate(plan['steps'], 1)
            ],
        })
        return history

    @staticmethod
    def active_plan(history):
        """Return the current version from chronological history."""
        return history[-1] if history and history[-1]['status'] == 'active' else None

    @staticmethod
    def record_plan_call(active_plan, name, arguments, call_id, result, error):
        """Record execution or explicit conditional skip on the first eligible step of the active plan."""
        if active_plan is None:
            return
        if error is None and isinstance(result, dict) and result.get('status') == 'skipped' and result.get('skipped_tool'):
            target_tool = result['skipped_tool']
            target_task = result.get('skipped_task_id')
            for planned in active_plan['steps']:
                if planned['status'] != 'pending' or planned['tool'] != target_tool:
                    continue
                if target_task is not None and planned['task_id'] != target_task:
                    continue
                planned.update(
                    status='skipped',
                    call_id=call_id,
                    outcome={'status': 'skipped', 'reason': str(result.get('reason') or '')},
                )
                return
            return

        for planned in active_plan['steps']:
            if planned['status'] != 'pending' or planned['tool'] != name:
                continue
            if isinstance(arguments, dict) and 'task_id' in arguments and planned['task_id'] != arguments['task_id']:
                continue
            if error is None:
                outcome = {}
                if isinstance(result, dict):
                    outcome = {key: result[key] for key in ('status', 'reason', 'supported', 'found') if key in result}
                    if 'proposals' in result and isinstance(result['proposals'], list):
                        outcome['proposal_count'] = len(result['proposals'])
                planned.update(status='done', call_id=call_id, outcome=outcome or {'summary': 'completed'})
            else:
                planned.update(status='failed', call_id=call_id, outcome={'error': error})
            break

    def write_plan_artifact(self, history):
        """Render plan history in the workflow run directory."""
        self.write_plan_markdown(self.services.rundir, history, getattr(self.services, 'check_output_path', None))

    def write_checklist_artifact(self, checklist):
        path = Path(self.services.rundir) / 'checklist.json'
        self.services.check_output_path(path)
        Checklist.from_dict(checklist).save(path)
        return str(path)

    @staticmethod
    def markdown_text(value):
        """Render dynamic values inline without allowing Markdown or HTML markup."""
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        inline = ' '.join(value.splitlines())
        return escape(re.sub(r'([\\`*\[\]])', r'\\\1', inline), quote=False)

    @classmethod
    def write_plan_markdown(cls, rundir, plan_history, check_output_path):
        """Atomically render all plan versions as a derived artifact."""
        target = Path(rundir) / 'plan.md'
        if check_output_path:
            check_output_path(target)
        marks = {'pending': ' ', 'done': 'x', 'failed': '!', 'skipped': '-'}
        lines = ['# DRC plan']
        for version in plan_history:
            lines.extend([
                '',
                f"## Plan {version['version']} ({version['status']})",
                '',
                f"Summary: {cls.markdown_text(version['summary'])}",
                '',
            ])
            for step in version['steps']:
                marker = marks[step['status']]
                tool = cls.markdown_text(step['tool'])
                purpose = cls.markdown_text(step['purpose'])
                task_id = cls.markdown_text(step['task_id'])
                label = f'task: {task_id}'
                if step['call_id'] is not None:
                    label += f"; call: {cls.markdown_text(step['call_id'])}"
                lines.append(f"{step['step_id']}. [{marker}] **{tool}** — {purpose} ({label})")
                outcome = step['outcome']
                if step['status'] == 'failed':
                    lines.append(f"   - Error: {cls.markdown_text(outcome['error'])}")
                elif step['status'] == 'skipped':
                    lines.append(f"   - Skipped: {cls.markdown_text(outcome.get('reason', ''))}")
                elif step['status'] == 'done':
                    details = '; '.join(f'{key}={cls.markdown_text(value)}' for key, value in outcome.items())
                    if outcome == {'summary': 'completed'}:
                        details = 'completed'
                    lines.append(f'   - Outcome: {details}')
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=target.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write('\n'.join(lines) + '\n')
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target

    @staticmethod
    def plan_task_context(tasks, report):
        markers = defaultdict(list)
        for rule in report.get('rules') or []:
            occurrences = defaultdict(int)
            name = str(rule.get('name', '')).strip()
            for marker in rule.get('geometry') or []:
                violation_id = marker.get('id')
                occurrence = occurrences[violation_id]
                occurrences[violation_id] += 1
                markers[name, violation_id, occurrence].append(marker)

        task_context = []
        for task in tasks:
            key = (task['rule'], task['violation_id'], task.get('occurrence', 0))
            matches = markers.get(key, [])
            marker = matches[0] if len(matches) == 1 else {}
            points = marker.get('points_dbu')
            geometry_status = 'ambiguous' if len(matches) > 1 else 'matched' if points else 'missing'
            item = {
                'task_id': task['task_id'],
                'rule': key[0],
                'violation_id': key[1],
                'occurrence': key[2],
                'status': task['status'],
                'type': marker.get('type'),
                'points_dbu': points,
                'geometry_status': geometry_status,
            }
            if task.get('reason'):
                item['reason'] = task['reason']
            task_context.append(item)
        return task_context

    def advance_queue_item(self, state, status: str, reason: str, response=None) -> dict:
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
        updates.update({
            'task_queue': queue,
            'current_task_id': next_id,
            'task_history': history,
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
        request_messages = list(messages)
        node_prompt = self.services.node_prompt(step)
        agent_payload = {}
        if node_prompt:
            agent_payload[f'{step.id}_instruction'] = node_prompt
        if state.get('checklist') is not None:
            goal = state.get('drc_context', {}).get('goal', self.services.config.get('goal', ''))
            agent_payload['current_checklist'] = state['checklist']
            agent_payload['goal'] = goal
        if state.get('current_task_id') is not None:
            agent_payload['current_task_id'] = state['current_task_id']
        if state.get('plan') is not None:
            agent_payload['plan'] = state['plan']
        active_plan = self.active_plan(state.get('plan_history', []))
        if active_plan is not None:
            agent_payload['plan_progress'] = active_plan
        report = state.get('report') or {}
        target_cell = report.get('top_cell') if context.task == 'drc' else None
        if target_cell:
            agent_payload['target_cell'] = target_cell
        if agent_payload:
            request_messages.append({
                'role': 'user',
                'content': json.dumps(agent_payload),
            })
        try:
            message = self.services.chat(
                request_messages,
                self.agent_tool_specs(step, context.task, target_cell),
                response_schema=getattr(step, 'schema', None),
            )
            self.last_conversation = [*request_messages, message]
            calls = message.get('tool_calls')
            updates = {'messages': [*messages, message], 'iteration': iteration + 1, 'pending_tools': bool(calls)}
            if not calls:
                response = json.loads(message['content'])
                schema = getattr(step, 'schema', None) or self.services.workflow.response_schema
                jsonschema.Draft202012Validator(schema).validate(response)
                if queue_mode:
                    item_status = 'unresolved' if (response.get('decision') == 'stop' or response.get('status') == 'incomplete' and response.get('decision') != 'completed') else 'pass'
                    adv = self.advance_queue_item(state, item_status, response.get('reason', ''), response=response)
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
        history = deepcopy(context.state.get('plan_history', []))
        active_plan = self.active_plan(history)
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
                        self.record_plan_call(active_plan, name, arguments, call_id, result_payload, error)
                        messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': payload})
                        self.last_conversation = list(messages)
                        adv = self.advance_queue_item({**context.state, **updates}, 'unresolved', error)
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
                    self.record_plan_call(active_plan, name, arguments, call_id, result_payload, error)
                    messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': payload})
                    self.last_conversation = list(messages)
                    adv = self.advance_queue_item({**context.state, **updates}, 'unresolved', error)
                    return {**updates, **adv, 'messages': messages}
                call_args = arguments if isinstance(arguments, dict) else {}
                relevant_vids = [call_args['task_id']] if 'task_id' in call_args else []
                updates['tool_review'] = {
                    'tool': tool_name,
                    'tool_call_id': call_id,
                    'arguments': call_args,
                    'task_id': call_args.get('task_id'),
                    'error': error,
                    'candidate_digest': context.state.get('candidate_digest'),
                }
                updates['replan_context'] = None
                """updates['replan_context'] = {
                    'failed_tool': tool_name,
                    'tool_call_id': call_id,
                    'arguments': call_args,
                    'error': error,
                    'conversation': [
                        messages[-1],
                        {'role': 'tool', 'tool_call_id': call_id, 'content': payload},
                    ],
                    'relevant_violations': relevant_vids,
                }"""
            self.record_plan_call(active_plan, name, arguments, call_id, result_payload, error)
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
        if history:
            updates['plan_history'] = history
            try:
                self.write_plan_artifact(history)
            except (OSError, ValueError) as exc:
                # Dispatched tools may already have persisted changes.
                reason = f'{context.task}/{step.id}: cannot write plan.md: {exc}'
                print(f'[ERROR] {reason}', file=sys.stderr)
                updates.update(failed=True, result={'status': 'incomplete', 'reason': reason})
        if updates.get('checklist'):
            try:
                updates['checklist_path'] = self.write_checklist_artifact(updates['checklist'])
            except (OSError, ValueError) as exc:
                reason = f'{context.task}/{step.id}: cannot write checklist.json: {exc}'
                print(f'[ERROR] {reason}', file=sys.stderr)
                updates.update(failed=True, result={'status': 'incomplete', 'reason': reason})
        return {**updates, 'messages': messages, 'pending_tools': False}

    def save_conversation(self, messages):
        """Keep in-memory reference to the latest conversation messages."""
        self.last_conversation = list(messages)

    def node_name(step_id):
        """Keep workflow identifiers that overlap LangGraph state channels."""
        return f'step--{step_id}' if step_id in FagentState.__annotations__ else step_id

    node_name = staticmethod(node_name)

    def route_task_step(self, state, step):
        """Follow the successful route or terminate after a failure."""
        destination = self.node_name(step.next)
        if state.get('failed'):
            destination = 'END'
        elif state.get('pending_tools'):
            destination = f'{step.id}--tools'
        elif step.action not in ('agent', 'review') and state.get('task_queue') is not None and len(state['task_queue']) == 0:
            destination = 'END'
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
        """Return to caller or route to plan on tool failure."""
        if state.get('failed'):
            destination = 'END'
        elif state.get('tool_review'):
            destination = f'{step.id}--tool-review'
        elif state.get('replan_context'):
            destination = self.node_name('plan')
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
        if name == 'measure_violation' and result.get('status') != 'measured':
            return f"unexpected measurement status {result.get('status')!r}"
        if name == 'propose_polygon_fix' and not result.get('proposals') and result.get('reason'):
            return result['reason']
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
        feedback = interrupt({'result': result, 'validation': validation, 'checklist': state.get('checklist')})
        envelope = feedback if isinstance(feedback, dict) else {'feedback': feedback}
        feedback = envelope.get('feedback')
        messages = list(state.get('messages', []))
        decision = 'stop'
        if isinstance(feedback, str) and feedback.strip():
            messages.append({'role': 'user', 'content': feedback})
            try:
                decision = self.services.interpret_feedback(feedback, state)['decision']
                if decision not in ('stop', 'continue', 'unclear'):
                    decision = 'unclear'
            except (ValueError, RuntimeError, KeyError, TypeError, OSError, ImportError):
                decision = 'unclear'
        authorized = (
            decision == 'stop'
            and isinstance(feedback, str)
            and bool(feedback.strip())
            and envelope.get('storage_consent') is True
            and getattr(self.services.workflow, 'knowledge_review', False)
        )
        review = {
            'feedback': feedback,
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
            update.update(iteration=0, repair_error=None, response=None)
        return update


write_plan_markdown = GraphNodes.write_plan_markdown
