"""Deterministic workflow stages and constrained model tool calls."""
import json
from pathlib import Path

import jsonschema
from langgraph.types import interrupt

from .state import candidate_digest


class GraphNodes:
    def run(self):
        self.actions = {
            'read_drc': self.read_drc,
            'repair_drc': self.repair_drc,
            'validate_drc': self.validate_drc,
            'read_lvs': self.read_lvs,
            'repair_lvs': self.repair_lvs,
            'validate_lvs': self.validate_lvs,
            'review': self.review,
        }
        return self

    def __init__(self, services):
        self.services = services

    def read_drc(self, state):
        report = self.services.read_drc()
        return {'inputs': report, 'messages': append_context(state, {'drc': report})}

    def repair_drc(self, state):
        iteration = state.get('iteration', 0)
        if iteration >= self.services.max_iterations:
            return {'repair_error': 'Iteration limit reached'}
        try:
            message = self.services.chat(state['messages'])
            result = {'messages': state['messages'] + [message], 'iteration': iteration + 1}
            if not message.get('tool_calls'):
                result['response'] = json.loads(message['content'])
                jsonschema.validate(result['response'], self.services.workflow.response_schema)
            return result
        except (ValueError, RuntimeError, KeyError, TypeError, OSError, ImportError, jsonschema.ValidationError) as exc:
            return {'repair_error': str(exc)}

    def route_repair(self, state):
        return 'tools' if not state.get('repair_error') and state['messages'][-1].get('tool_calls') else 'done'

    def repair_tools(self, state):
        messages = list(state['messages'])
        candidate = state.get('candidate_path')
        digest = state.get('candidate_digest')
        inspected = list(state.get('inspected', []))
        specs = {tool['function']['name']: tool['function']['parameters'] for tool in self.services.workflow.tool_specs}
        for call in messages[-1]['tool_calls']:
            result = {}
            call_id = call.get('id') if isinstance(call, dict) else None
            if not isinstance(call_id, str) or not call_id:
                return {'repair_error': 'Tool call is missing its id'}
            try:
                name = call['function']['name']
                args = call['function']['arguments']
                if isinstance(args, str):
                    args = json.loads(args)
                jsonschema.validate(args, specs[name])
                cell = args['cell_name']
                if name == 'replace_polygon' and cell not in inspected:
                    raise ValueError('Read the target cell before replacing a polygon')
                result = self.services.invoke_tool(name, args)
                if name == 'read_gds':
                    inspected.append(cell)
                elif result.get('status') == 'modified':
                    candidate = None
                    digest = None
                    saved = self.services.checkpoint_candidate()
                    digest = candidate_digest(Path(saved))
                    candidate = saved
            except (ValueError, RuntimeError, OSError, KeyError, TypeError, jsonschema.ValidationError) as exc:
                result = {'error': str(exc)}
            messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(result)})
        return {'messages': messages, 'candidate_path': candidate, 'candidate_digest': digest, 'inspected': inspected}

    def validate_drc(self, state):
        candidate = state.get('candidate_path')
        validation = {'status': 'unavailable', 'reason': 'No saved candidate'}
        if candidate:
            try:
                validation = self.services.validate_drc(candidate)
                if not isinstance(validation, dict) or validation.get('status') not in ('pass', 'failed', 'unavailable'):
                    raise ValueError('Missing backend result')
                if validation['status'] == 'pass' and (
                    self.services.workflow.backend != 'local' or validation.get('simulated') is not True
                ):
                    raise ValueError('DRC pass requires supported validation evidence')
                if validation.get('candidate') not in (None, candidate):
                    raise ValueError('Backend result refers to a different candidate')
            except (OSError, RuntimeError, ValueError) as exc:
                validation = {'status': 'error', 'reason': str(exc)}
        return {'validation': validation, 'messages': append_context(state, {'validation': validation})}

    def read_lvs(self, state):
        report = self.services.read_lvs()
        return {'inputs': report, 'messages': append_context(state, {'lvs': report})}

    def repair_lvs(self, state):
        repair = {'status': 'unavailable', 'reason': 'No supported LVS repair capability'}
        return {'response': {'repair': repair}, 'messages': append_context(state, {'repair': repair})}

    def validate_lvs(self, state):
        try:
            evidence = self.services.validate_lvs()
            if not isinstance(evidence, dict) or evidence.get('status') not in ('pass', 'unavailable'):
                raise ValueError('Missing or invalid LVS backend result')
            if evidence['status'] == 'pass' and (evidence.get('simulated') is not True
                                                 or self.services.workflow.backend != 'local'
                                                 or not self.services.workflow.mock_pass):
                raise ValueError('LVS pass requires the configured simulated backend')
        except (OSError, RuntimeError, ValueError) as exc:
            evidence = {'status': 'error', 'reason': str(exc)}
        return {'validation': evidence, 'messages': append_context(state, {'validation': evidence})}

    def review(self, state):
        validation = state.get('validation', {})
        response = state.get('response')
        if isinstance(response, dict) and 'status' in response:
            response = {**response, 'status': 'incomplete'}
        result = {'status': 'incomplete', 'reason': state.get('repair_error') or validation.get('reason') or 'No verified tool evidence',
                  'response': response, 'candidate': state.get('candidate_path')}
        feedback = interrupt({'result': result, 'validation': validation})
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
        authorized = (decision == 'stop' and isinstance(feedback, str) and bool(feedback.strip())
                      and envelope.get('storage_consent') is True and self.services.workflow.task == 'drc')
        review = {'feedback': feedback, 'decision': decision, 'authorized': authorized,
                  'storage_consent': authorized, 'consent_source': 'human_review' if authorized else None}
        update = {'result': result, 'messages': messages, 'review': review, 'save_requested': authorized}
        if decision == 'continue':
            update.update(iteration=0, repair_error=None, response=None)
        return update

    def route_review(self, state):
        return state['review']['decision']


def append_context(state, context):
    return state.get('messages', []) + [{'role': 'user', 'content': json.dumps(context)}]
