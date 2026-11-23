"""Deterministic workflow stages and constrained model tool calls."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
import json
from pathlib import Path
import sys

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

    # --------------------------------------------------------------------------
    # Message Context Helpers
    # --------------------------------------------------------------------------
    def run_task_step(self, state, step, tools=False):
        """Invoke a local step and capture serializable updates."""
        workflow = self.services.workflow
        node = f'{step.id}--tools' if tools else self.node_name(step.id)
        if self.trace:
            self.trace.node_start(node, step.id, step.action, tools=tools)
        inputs = state.get('inputs', {})
        task_state = state.get('task_state', {})
        context = TaskContext(workflow.task, inputs, {**task_state, **state}, self.services)
        missing = set(workflow.inputs) - inputs.keys()
        try:
            if missing:
                raise ValueError(f'Missing inputs: {", ".join(sorted(missing))}')
            if tools:
                return self.execute_tools(context, step)
            if step.action == 'plan':
                return self.execute_plan(context, step)
            if step.action == 'agent':
                return self.execute_agent(context, step)
            if step.action == 'review':
                return self.review(state)
            updates = self.execute_step(context, step)
            return {**updates, 'task_state': {**task_state, **updates}}
        except GraphInterrupt:
            if self.trace:
                self.trace.node_end(node, step.id, "waiting")
            raise
        except Exception as exc:
            reason = f'{workflow.task}/{step.id}: {type(exc).__name__}'
            if missing:
                reason += f' (missing inputs: {", ".join(sorted(missing))})'
            print(f'[ERROR] {reason}', file=sys.stderr)
            return {'failed': True, 'result': {'status': 'incomplete', 'reason': reason}}

    def execute_step(self, context, step):
        """Return the JSON-compatible mapping from a resolved step."""
        updates = step.function(context)
        if not isinstance(updates, dict) or any(not isinstance(key, str) for key in updates):
            raise TypeError('A step must return a mapping with string keys')
        json.dumps(updates, allow_nan=False)
        return updates

    def execute_plan(self, context, step):
        """Request a structured violation plan and build the run-scoped checklist."""
        state = context.state
        messages = list(state.get('messages', []))
        report = state.get('report')
        if not isinstance(report, dict):
            raise ValueError('DRC report must be read before planning')
        goal = self.services.config.get('goal', '') if hasattr(self.services, 'config') else ''
        plan_request = [
            *messages,
            {'role': 'user', 'content': json.dumps({'plan_instruction': step.prompt, 'goal': goal, 'drc': report})},
        ]
        message = self.services.chat(plan_request, [], response_schema=step.schema)
        if message.get('tool_calls'):
            raise ValueError('Plan step cannot invoke tools')
        plan = json.loads(message['content'])
        jsonschema.Draft202012Validator(step.schema).validate(plan)
        checklist = Checklist().run(report, plan['tasks'], goal=goal)
        snapshot = checklist.read()
        checklist_path = Path(self.services.rundir) / 'checklist.json'
        self.services.check_output_path(checklist_path)
        checklist.save(checklist_path)
        return {
            'messages': [*messages, message],
            'checklist': snapshot,
            'checklist_digest': snapshot['digest'],
            'checklist_path': str(checklist_path),
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
        if state.get('checklist') is not None:
            goal = self.services.config.get('goal', '') if hasattr(self.services, 'config') else ''
            request_messages.append({
                'role': 'user',
                'content': json.dumps({'current_checklist': state['checklist'], 'goal': goal}),
            })
        message = self.services.chat(
            request_messages,
            [spec.model_spec(name) for name, spec in step.tools.items()],
            response_schema=getattr(step, 'schema', None),
        )
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
        for call, call_id in zip(calls, ids):
            name = None
            try:
                name = call['function']['name']
                arguments = call['function']['arguments']
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
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
                else:
                    payload = json.dumps(result, allow_nan=False)
            except Exception as exc:
                tool_name = name if isinstance(name, str) and name in step.tools else 'unknown tool'
                error = f'{context.task}/{step.id}/{tool_name}: {type(exc).__name__}'
                print(f'[ERROR] {error}', file=sys.stderr)
                payload = json.dumps({'error': error})
            messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': payload})
        return {**updates, 'messages': messages, 'pending_tools': False}

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
        """Return to the caller unless the tool envelope failed."""
        destination = 'END' if state.get('failed') else self.node_name(step.id)
        return self.trace_route(f'{step.id}--tools', step.id, state, destination)

    def trace_route(self, node, step, state, destination):
        """Report the route after the graph chooses it."""
        if self.trace:
            if state.get('failed'):
                self.trace.node_end(node, step, 'failed')
            else:
                self.trace.node_end(node, step, 'completed', destination)
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
        update = {'result': result, 'messages': messages, 'review': review, 'save_requested': authorized}
        if decision == 'continue':
            update.update(iteration=0, repair_error=None, response=None)
        return update
