import json
import subprocess
import sys


def test_lvs_can_rerun_in_same_output_directory(tmp_path):
    config = tmp_path / 'config.json'
    config.write_text('{}')
    setup = tmp_path / 'setup.yaml'
    setup.write_text('output_folder: output\ntasks:\n  - lvs: {}\n')
    command = [sys.executable, '-m', 'FAGENT', '--config', str(config), '--setup', str(setup)]
    first = subprocess.run(command, input='', capture_output=True, text=True)
    assert first.returncode == 0, first.stderr

    rundir = tmp_path / 'output' / 'lvs' / 'rundir'
    result = rundir / 'result.json'
    conversation = rundir / 'conversation.json'
    for artifact in (result, conversation):
        artifact.write_text('stale output')
    second = subprocess.run(command, input='', capture_output=True, text=True)
    assert second.returncode == 0, second.stderr
    assert json.loads(result.read_text())['status'] == 'incomplete'
    assert isinstance(json.loads(conversation.read_text()), list)
