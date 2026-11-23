"""Deterministic workflow stages and constrained model tool calls."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
from datetime import datetime, timezone
import json
from pathlib import Path
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
        """Return the JSON-compatible mapping from a resolved step."""
        updates = step.function(context)
        if not isinstance(updates, dict) or any(not isinstance(key, str) for key in updates):
            raise TypeError('A step must return a mapping with string keys')
        json.dumps(updates, allow_nan=False)
        return updates

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
            return {
                'plan': plan,
                'replan_count': replan_count,
                'replan_context': None,
                'iteration': 0,
            }

        report = state.get('report')
        if not isinstance(report, dict) and hasattr(self.services, 'drc_report'):
            report = self.services.drc_report

        active_rules = {t['rule'] for t in active_tasks}
        active_violations = []
        if report and isinstance(report.get('rules'), list):
            for r in report['rules']:
                if r.get('name') in active_rules:
                    for geom in r.get('geometry', []):
                        active_violations.append({
                            'rule': r.get('name'),
                            'id': geom.get('id'),
                            'type': geom.get('type'),
                            'points_dbu': geom.get('points_dbu'),
                        })

        rag_refs = list(state.get('drc_context', {}).get('knowledge', {}).get('references') or [])
        if not rag_refs and hasattr(self.services, 'rag') and hasattr(self.services.rag, 'retrieve'):
            try:
                for rule_name in active_rules:
                    rag_refs.extend(self.services.rag.retrieve(rule_name))
            except Exception:
                pass

        goal = state.get('drc_context', {}).get('goal', self.services.config.get('goal', ''))
        constraints = state.get('drc_context', {}).get('knowledge', {}).get('constraints', [])
        instruction = getattr(step, 'prompt', None) or self.services.node_prompt(step)

        plan_payload = {
            f'{step.id}_instruction': instruction,
            'goal': goal,
            'constraints': constraints,
            'active_violations': active_violations,
            'references': rag_refs,
            'checklist': state.get('checklist'),
        }
        if replan_ctx:
            plan_payload['replan_context'] = replan_ctx
            plan_payload['candidate_digest'] = state.get('candidate_digest')
            plan_payload['candidate_path'] = state.get('candidate_path')
            plan_payload['current_plan'] = state.get('plan')

        sys_msg = next((m for m in messages if m.get('role') == 'system'), None)
        plan_request = [sys_msg] if sys_msg else []
        plan_request.append({'role': 'user', 'content': json.dumps(plan_payload)})

        message = self.services.chat(plan_request, [], response_schema=step.schema)
        self.last_conversation = [*plan_request, message]
        if message.get('tool_calls'):
            raise ValueError('Plan step cannot invoke tools')

        plan = json.loads(message['content'])
        jsonschema.Draft202012Validator(step.schema).validate(plan)

        if len(active_tasks) > 0 and len(plan.get('steps', [])) == 0:
            reason = 'Empty plan returned while active checklist tasks remain'
            print(f'[ERROR] {reason}', file=sys.stderr)
            return {
                'failed': True,
                'plan': plan,
                'replan_count': replan_count,
                'replan_context': None,
                'result': {'status': 'incomplete', 'reason': reason},
            }

        return {
            'messages': [*messages, message],
            'plan': plan,
            'replan_count': replan_count,
            'replan_context': None,
            'iteration': 0,
        }

    def execute_agent(self, context, step):
        """Request a tool call or schema-valid response."""
        state = context.state
        iteration = state.get('iteration', 0)
        if iteration >= self.services.max_iterations:
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
        if state.get('plan') is not None:
            agent_payload['plan'] = state['plan']
        if agent_payload:
            request_messages.append({
                'role': 'user',
                'content': json.dumps(agent_payload),
            })
        message = self.services.chat(
            request_messages,
            [spec.model_spec(name) for name, spec in step.tools.items()],
            response_schema=getattr(step, 'schema', None),
        )
        self.last_conversation = [*request_messages, message]
        calls = message.get('tool_calls')
        updates = {'messages': [*messages, message], 'iteration': iteration + 1, 'pending_tools': bool(calls)}
        if not calls:
            response = json.loads(message['content'])
            schema = getattr(step, 'schema', None) or self.services.workflow.response_schema
            jsonschema.Draft202012Validator(schema).validate(response)
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
            name = None
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
                    payload = json.dumps(result.payload, allow_nan=False)
                    json.dumps(result.updates, allow_nan=False)
                    updates.update(result.updates)
                    if trace_inst and tool_op:
                        trace_inst.finish_operation(tool_op, 'tool_call', status='completed', outputs={'result': result.payload})
                else:
                    payload = json.dumps(result, allow_nan=False)
                    if trace_inst and tool_op:
                        trace_inst.finish_operation(tool_op, 'tool_call', status='completed', outputs={'result': result})
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
                call_args = arguments if 'arguments' in locals() and isinstance(arguments, dict) else {}
                relevant_vids = [call_args['task_id']] if 'task_id' in call_args else []
                updates['replan_context'] = {
                    'failed_tool': tool_name,
                    'tool_call_id': call_id,
                    'arguments': call_args,
                    'error': error,
                    'conversation': [
                        messages[-1],
                        {'role': 'tool', 'tool_call_id': call_id, 'content': payload},
                    ],
                    'relevant_violations': relevant_vids,
                }
            messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': payload})
            if trace_inst:
                trace_inst.append({
                    'type': 'tool_result',
                    'message': {'role': 'tool', 'tool_call_id': call_id, 'content': payload},
                })
            self.last_conversation = list(messages)
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
        elif state.get('replan_context'):
            destination = self.node_name('plan')
        else:
            destination = self.node_name(step.id)
        return self.trace_route(f'{step.id}--tools', step.id, state, destination)

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
