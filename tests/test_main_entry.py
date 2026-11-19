import json
from pathlib import Path
import subprocess
import sys

import pytest
from langgraph.checkpoint.memory import MemorySaver

from FAGENT.engine.runner import GraphRunner
from test_drc_graph import FakeServices


class ReviewServices(FakeServices):
    def run(self):
        return self

    def interpret_feedback(self, feedback, state):
        self.calls.append(('interpret', feedback))
        return {'decision': self.decision}

    def store_knowledge(self, review):
        self.calls.append('store')


class Provider:
    def run(self):
        return next(self.values)

    def __init__(self, *values):
        self.values = iter(values)


@pytest.mark.parametrize('feedback', ['Please wrap it up', 'Mình dừng ở đây nhé', None])
def test_stop_preserves_feedback(tmp_path, feedback):
    services = ReviewServices(tmp_path)
    services.decision = 'stop'
    provider = Provider(feedback)
    result = GraphRunner(services, tmp_path, review_provider=provider.run).run([])
    assert result['review']['feedback'] == feedback
    assert result['review']['authorized'] is False
    assert 'store' not in services.calls
    assert services.calls[-1] == (('interpret', feedback) if feedback else 'validate')


def test_consent_separate_from_feedback(tmp_path):
    services = ReviewServices(tmp_path)
    services.decision = 'stop'
    result = GraphRunner(services, tmp_path, review_provider=Provider({
        'feedback': 'Wrap it up', 'storage_consent': True,
    }).run).run([])
    assert result['review']['authorized'] is True
    assert services.calls[-1] == 'store'


def test_resume_rejects_different_workflow(tmp_path):
    services = ReviewServices(tmp_path)
    runner = GraphRunner(services, tmp_path, checkpointer=MemorySaver())
    runner.prepare_graph()
    runner.graph.invoke({'messages': [], 'identity': runner.workflow_identity()}, runner.graph_config)
    services.workflow = services.workflow.model_copy(update={'digest': 'different'})
    with pytest.raises(ValueError, match='workflow'):
        runner.resume(None)


def test_lvs_cli(tmp_path):
    completed = subprocess.run([sys.executable, '-m', 'FAGENT', '--task', 'lvs',
                                '--output', str(tmp_path / 'lvs'), '--quiet'],
                               input='', capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    result = json.loads((tmp_path / 'lvs' / 'rundir' / 'result.json').read_text())
    assert result['task']['task'] == 'lvs'
    assert result['status'] == 'incomplete'


def test_invalid_drc_inputs(tmp_path):
    completed = subprocess.run([sys.executable, '-m', 'FAGENT', '--output', str(tmp_path)],
                               capture_output=True, text=True)
    assert completed.returncode == 2
    assert not (tmp_path / 'rundir').exists()


def test_drc_load_only_preserves_existing_artifacts(tmp_path):
    root = Path(__file__).resolve().parents[1]
    args = [sys.executable, '-m', 'FAGENT', '--load-only', '--quiet',
            '--gds', str(root / 'DRC/ROM_MCELL0.gds'), '--drc', str(root / 'DRC/ROM_MCELL0.drc.results'),
            '--layer-map', str(root / 'config/layer_mapping'), '--output', str(tmp_path)]
    first = subprocess.run(args, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)['status'] == 'loaded'
    artifact = tmp_path / 'rundir' / 'gds.json'
    artifact.write_text('previous result')
    second = subprocess.run(args, capture_output=True, text=True)
    assert second.returncode != 0
    assert artifact.read_text() == 'previous result'


def test_resume_matching_workflow(tmp_path):
    services = ReviewServices(tmp_path, invalid=True)
    services.decision = 'stop'
    runner = GraphRunner(services, tmp_path, checkpointer=MemorySaver())
    runner.prepare_graph()
    runner.graph.invoke({'messages': [], 'identity': runner.workflow_identity()}, runner.graph_config)
    calls = list(services.calls)
    resumed = runner.resume('Enough for now')
    assert resumed['review']['decision'] == 'stop'
    assert services.calls == calls + [('interpret', 'Enough for now')]


def test_ambiguous_feedback_represents_review_without_tools(tmp_path):
    services = ReviewServices(tmp_path)
    services.decision = 'unclear'
    provider = Provider('Maybe', None)
    runner = GraphRunner(services, tmp_path, review_provider=provider.run)
    runner.run([])
    assert services.calls.count('validate') == 1
    assert services.calls[-1] == ('interpret', 'Maybe')
    assert any(message.get('content') == 'Maybe' for message in runner.messages)


class CandidateServices(ReviewServices):
    def run(self):
        return self

    def checkpoint_candidate(self):
        path = Path(super().checkpoint_candidate())
        path.write_bytes(b'candidate')
        return str(path)

    def restore_candidate(self, path, validation):
        self.calls.append('restore')


def test_resume_rejects_changed_candidate(tmp_path):
    services = CandidateServices(tmp_path)
    runner = GraphRunner(services, tmp_path, checkpointer=MemorySaver())
    runner.prepare_graph()
    runner.graph.invoke({'messages': [], 'identity': runner.workflow_identity()}, runner.graph_config)
    (tmp_path / 'candidate.gds').write_bytes(b'changed')
    with pytest.raises(ValueError, match='candidate'):
        runner.resume(None)
    assert 'restore' not in services.calls


def test_setup_drc_load_only(tmp_path):
    import yaml

    root = Path(__file__).resolve().parents[1]
    setup = yaml.safe_load((root / 'env_setup.yaml').read_text())
    for key in ('list_cells', 'drc_folder', 'drc_files'):
        setup[key] = str(root / setup[key])
    setup['layer_map_files'] = [str(root / path) for path in setup['layer_map_files']]
    setup['output_folder'] = str(tmp_path / 'batch')
    file = tmp_path / 'setup.yaml'
    file.write_text(yaml.safe_dump(setup))
    command = [sys.executable, '-m', 'FAGENT', '--task', 'drc', '--load-only', '--quiet', '--setup', str(file)]
    completed = subprocess.run(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert all(json.loads(line)['status'] == 'loaded' for line in completed.stdout.splitlines())
    assert list((tmp_path / 'batch').glob('*/rundir/drc.json'))


def test_continue_returns_to_repair_and_drops_previous_consent(tmp_path):
    services = ReviewServices(tmp_path)
    services.decision = 'continue'
    provider = Provider({'feedback': 'Please repair the remaining issues', 'storage_consent': True}, None)
    result = GraphRunner(services, tmp_path, review_provider=provider.run).run([])
    assert services.calls.count('validate') == 2
    assert services.calls.count('read') == 1
    assert result['review']['authorized'] is False
    assert 'store' not in services.calls


def test_resume_restores_matching_candidate(tmp_path):
    services = CandidateServices(tmp_path)
    runner = GraphRunner(services, tmp_path, checkpointer=MemorySaver())
    runner.prepare_graph()
    runner.graph.invoke({'messages': [], 'identity': runner.workflow_identity()}, runner.graph_config)
    calls = list(services.calls)
    result = runner.resume(None)
    assert services.calls == calls + ['restore']
    assert result['candidate'] == str(tmp_path / 'candidate.gds')
