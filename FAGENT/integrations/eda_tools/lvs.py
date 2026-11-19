"""Local mock for exercising the LVS workflow."""


def run_lvs(backend, mock_pass):
    if backend != 'local':
        return {'status': 'unavailable', 'reason': 'Real LVS invocation and report contract are unavailable'}
    if mock_pass is not True:
        return {'status': 'unavailable', 'reason': 'LVS mock pass is not configured'}
    return {'status': 'pass', 'simulated': True, 'backend': 'mock'}
