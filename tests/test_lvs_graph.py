import json

import pytest

from FAGENT.engine.runner import GraphRunner
from FAGENT.engine.services import GraphServices
from FAGENT.engine.workflow import load_workflow
from test_drc_graph import ASSETS, end_review


@pytest.mark.parametrize('backend,status', [('local', 'pass'), ('real', 'unavailable')])
def test_lvs_evidence(tmp_path, backend, status):
    workflow = load_workflow(ASSETS / 'workflows' / 'lvs.yaml').model_copy(update={'backend': backend})
    services = GraphServices(tmp_path, {'lvs_report': 'unknown-format'}, workflow)
    runner = GraphRunner(services, tmp_path, review_provider=end_review)
    result = runner.run(services.prepare())

    assert result['status'] == 'incomplete'
    assert result['validation']['status'] == status
    if backend == 'local':
        assert result['validation']['simulated'] is True
    assert not (tmp_path / 'candidate.gds').exists()
    messages = json.dumps(runner.messages)
    assert 'unknown-format' in messages
    assert 'repair' in messages


class FailedLvsServices(GraphServices):
    def run(self):
        return self

    def validate_lvs(self):
        if self.config.get('raise_error'):
            raise RuntimeError('mock unavailable')
        return None


@pytest.mark.parametrize('raise_error', [False, True])
def test_failed_mock_is_not_pass(tmp_path, raise_error):
    workflow = load_workflow(ASSETS / 'workflows' / 'lvs.yaml')
    services = FailedLvsServices(tmp_path, {'raise_error': raise_error}, workflow)
    result = GraphRunner(services, tmp_path, review_provider=end_review).run(services.prepare())
    assert result['status'] == 'incomplete'
    assert result['validation']['status'] == 'error'
