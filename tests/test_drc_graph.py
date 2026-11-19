import json
from pathlib import Path

import pytest

from FAGENT.engine.runner import GraphRunner
from FAGENT.engine.workflow import load_workflow


ASSETS = Path(__file__).resolve().parents[1] / 'FAGENT'


class FakeServices:
    def run(self):
        return self

    max_iterations = 2

    def __init__(self, rundir, invalid=False, failure=False, task='drc_repair'):
        self.rundir = rundir
        self.workflow = load_workflow(ASSETS / 'workflows' / f'{task}.yaml')
        self.calls = []
        self.invalid = invalid
        self.failure = failure

    def read_drc(self):
        self.calls.append('read')
        return {'source': 'old.results'}

    def chat(self, messages):
        self.calls.append('model')
        if messages[-1]['role'] == 'tool':
            return {'role': 'assistant', 'content': json.dumps({
                'decision': 'completed', 'status': 'verified', 'reason': 'done',
                'violations_analysis': [], 'recommendations': ''})}
        return {'role': 'assistant', 'tool_calls': [
            {'id': 'inspect', 'function': {'name': 'read_gds', 'arguments': {'cell_name': 'target'}}},
            {'id': 'edit', 'function': {'name': 'replace_polygon', 'arguments': {
                'cell_name': 'target', 'polygon_index': 'bad' if self.invalid else 0,
                'points': [[0, 0], [1, 0], [0, 1]]}}},
        ]}

    def invoke_tool(self, name, args):
        self.calls.append(name)
        return {'status': 'modified' if name == 'replace_polygon' else 'read'}

    def checkpoint_candidate(self):
        self.calls.append('save')
        return str(self.rundir / 'candidate.gds')

    def validate_drc(self, candidate):
        self.calls.append('validate')
        if self.failure:
            raise RuntimeError('backend failed')
        return {'status': 'pass', 'simulated': True, 'candidate': candidate, 'report': 'new.results'}

    def check_output_path(self, path):
        assert path.parent == self.rundir


def end_review():
    return None


@pytest.mark.parametrize('invalid,failure,limit', [(False, False, 2), (True, False, 2), (False, True, 1)])
def test_drc_order_and_evidence(tmp_path, invalid, failure, limit):
    services = FakeServices(tmp_path, invalid, failure)
    services.max_iterations = limit
    runner = GraphRunner(services, tmp_path, review_provider=end_review)
    result = runner.run([])
    assert services.calls.count('read') == 1
    assert result['status'] == 'incomplete'
    if invalid:
        assert 'replace_polygon' not in services.calls
        assert 'validate' not in services.calls
    else:
        assert services.calls[:5] == ['read', 'model', 'read_gds', 'replace_polygon', 'save']
        assert services.calls.count('validate') == 1
        if failure:
            assert result['validation']['status'] == 'error'
        else:
            assert result['validation']['report'] == 'new.results'
            assert result['response']['status'] == 'incomplete'
            assert result['validation']['simulated'] is True
            assert 'new.results' in json.dumps(runner.messages)


def test_drc_simulator_adapter_uses_new_candidate_and_report(tmp_path):
    from shutil import copyfile
    from types import SimpleNamespace

    from FAGENT.integrations.eda_tools.drc import run_drc

    sample = ASSETS.parent / 'DRC'
    source = next(sample.glob('*.gds'))
    candidate = tmp_path / 'candidate.gds'
    copyfile(source, candidate)
    deck = next(sample.glob('*.drc'))
    layout = SimpleNamespace(candidate=candidate, rundir=tmp_path, drc_files=[deck],
                             drc_data={'top_cell': source.stem})
    result = run_drc(layout, candidate, 'local')

    assert result['simulated'] is True
    assert result['backend'] == 'calibre_simulator'
    assert Path(result['report']).is_file()
    assert Path(result['report']).parent.parent == tmp_path
    assert result['candidate'] == str(candidate)
