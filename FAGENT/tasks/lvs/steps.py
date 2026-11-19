"""Task-local execution steps."""

# === SECTION 1: Imports ===
import json


# === SECTION 2: Steps ===
class Steps:
    """Execute the selected task stages."""

    def run(self, context):
        """Read the report for this task."""
        self.result = self.read_lvs(context)
        return self.result

    def __init__(self):
        """Initialize step outputs."""
        self.result = None

    def append_context(self, state, payload):
        """Append a report to the model conversation."""
        return [*state.get('messages', []), {'role': 'user', 'content': json.dumps(payload)}]

    def read_lvs(self, context):
        report = context.services.read_lvs()
        return {'report': report, 'messages': self.append_context(context.state, {'lvs': report})}

    def repair_lvs(self, context):
        repair = {'status': 'unavailable', 'reason': 'No supported LVS repair capability'}
        return {'response': {'repair': repair}, 'messages': self.append_context(context.state, {'repair': repair})}

    def validate_lvs(self, context):
        try:
            evidence = context.services.validate_lvs()
            if not isinstance(evidence, dict) or evidence.get('status') not in ('pass', 'unavailable'):
                raise ValueError('Missing or invalid LVS backend result')
            if evidence['status'] == 'pass' and (
                evidence.get('simulated') is not True
                or context.services.workflow.backend != 'local'
                or not context.services.workflow.mock_pass
            ):
                raise ValueError('LVS pass requires the configured simulated backend')
        except (OSError, RuntimeError, ValueError) as exc:
            evidence = {'status': 'error', 'reason': str(exc)}
        return {'validation': evidence, 'messages': self.append_context(context.state, {'validation': evidence})}


    def prepare(self, task_cfg, setup, base, output, config, verbose):
        """Resolve optional LVS paths from the setup directory."""
        for key in ('lvs_report', 'layout', 'netlist', 'deck'):
            if value := task_cfg.get(key):
                config[key] = str((base / value).resolve())
        yield output / 'lvs' / 'rundir', config, task_cfg


_steps = Steps()
read_lvs = _steps.read_lvs
validate_lvs = _steps.validate_lvs
repair_lvs = _steps.repair_lvs
prepare = _steps.prepare
