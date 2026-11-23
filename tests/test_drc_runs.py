import json
from pathlib import Path
from types import SimpleNamespace

import gdstk
import pytest

from FAGENT.engine.graph_runner import GraphRunner
from FAGENT.tasks.drc.steps import Steps


def _setup_cell(tmp_path, cell='cell_a'):
    gds_dir = tmp_path / 'DRC'
    gds_dir.mkdir(exist_ok=True)
    lib = gdstk.Library()
    lib.new_cell(cell)
    lib.write_gds(str(gds_dir / f'{cell}.gds'))
    (gds_dir / f'{cell}.drc.results').write_text(
        f'{cell} 1000\nRULE_1\n1 1 1 Nov 5 12:30:30 2026\nDesc\np 1 3\n0 0\n10 0\n10 10\n'
    )
    (gds_dir / 'rules.drc').write_text('RULE_1 {}\n')
    (tmp_path / 'cells').write_text(cell + '\n')
    (tmp_path / 'layers').write_text('1 0 M1 drawing\n')
    setup = {'list_cells': 'cells', 'layer_map_files': ['layers']}
    task = {'gds_folder': 'DRC', 'drc_folder': 'DRC', 'drc_files': 'DRC/rules.drc'}
    return setup, task


class _FakeModel:
    def chat_with_schema(self, messages, schema):
        return {'decision': 'stop', 'summary': 'done', 'save_requested': False}


class _FakeServices:
    def __init__(self, rundir):
        self.rundir = Path(rundir)
        self.max_iterations = 2
        self.config_digest = 'cfg1'
        self.input_sources = None
        self.model = _FakeModel()
        self.workflow = SimpleNamespace(
            task='drc',
            version='1',
            digest='wf1',
            knowledge_review=False,
            prompt='p',
            inputs=[],
            steps=[
                SimpleNamespace(
                    id='read',
                    action='steps:read_drc',
                    function=lambda ctx: {'report': {'clean': False}, 'messages': ctx.state.get('messages', [])},
                    next='human_review',
                    continue_to=None,
                    tools=[],
                    tool_map={},
                    schema=None,
                    prompt=None,
                ),
                SimpleNamespace(
                    id='human_review',
                    action='review',
                    function=None,
                    next='END',
                    continue_to='read',
                    tools=[],
                    tool_map={},
                    schema={'type': 'object', 'properties': {'decision': {'type': 'string'}}},
                    prompt='review',
                ),
            ],
        )

    def check_output_path(self, path):
        if not Path(path).resolve().is_relative_to(self.rundir.resolve()):
            raise ValueError('escapes rundir')

    def restore_candidate(self, candidate, validation=None):
        return None

    def store_knowledge(self, review):
        return None

    def review_prompt(self):
        return 'review'

    def review_schema(self):
        return {'type': 'object', 'properties': {'decision': {'type': 'string'}}}


def test_two_fresh_runs_are_isolated(tmp_path):
    setup, task = _setup_cell(tmp_path)
    out = tmp_path / 'output'

    rundir1, _, inputs1 = next(Steps().prepare(task, setup, tmp_path, out, {}, False))
    (rundir1 / 'result.json').write_text(json.dumps({'run': 1}))
    (tmp_path / 'DRC' / 'cell_a.drc.results').write_text(
        'cell_a 1000\nRULE_2\n0 2 1 Nov 5 12:30:30 2026\nNo geom\n'
    )
    rundir2, _, inputs2 = next(Steps().prepare(task, setup, tmp_path, out, {}, False))

    assert rundir1 != rundir2
    assert inputs1['run_id'] != inputs2['run_id']
    assert rundir1.parent.parent == out / 'cell_a' / 'runs'
    assert rundir2.parent.parent == out / 'cell_a' / 'runs'
    assert json.loads((rundir1 / 'result.json').read_text()) == {'run': 1}
    assert not (rundir2 / 'result.json').exists()
    assert json.loads((rundir1 / 'drc.json').read_text())['rules'][0]['name'] == 'RULE_1'
    assert json.loads((rundir2 / 'drc.json').read_text())['rules'][0]['name'] == 'RULE_2'


def test_resume_continues_only_selected_run(tmp_path):
    rundir1 = tmp_path / 'output' / 'cell_a' / 'runs' / 'run-1' / 'rundir'
    rundir2 = tmp_path / 'output' / 'cell_a' / 'runs' / 'run-2' / 'rundir'

    runner1 = GraphRunner(_FakeServices(rundir1), rundir1, thread_id='t1')
    runner1.prepare_graph()
    # Pause at human_review by invoking graph directly without finish()
    state1 = runner1.graph.invoke(
        {'messages': [], 'inputs': {'run_id': 'run-1'}, 'iteration': 0, 'identity': runner1.workflow_identity()},
        runner1.graph_config,
    )
    assert state1.get('__interrupt__')

    runner2 = GraphRunner(_FakeServices(rundir2), rundir2, thread_id='t2')
    runner2.prepare_graph()
    state2 = runner2.graph.invoke(
        {'messages': [], 'inputs': {'run_id': 'run-2'}, 'iteration': 0, 'identity': runner2.workflow_identity()},
        runner2.graph_config,
    )
    assert state2.get('__interrupt__')

    # Resuming run-1 restores only run-1
    res1 = GraphRunner(_FakeServices(rundir1), rundir1, thread_id='t1').resume({'feedback': 'stop'})
    assert res1['run_id'] == 'run-1'
    assert res1['thread_id'] == 't1'
    assert Path(res1['rundir']) == rundir1
    assert (rundir1 / 'result.json').is_file()
    assert not (rundir2 / 'result.json').exists()

    # Cannot silently resume a missing checkpoint or wrong thread
    missing_dir = tmp_path / 'output' / 'cell_a' / 'runs' / 'run-missing' / 'rundir'
    with pytest.raises(ValueError):
        GraphRunner(_FakeServices(missing_dir), missing_dir, thread_id='t1').resume({'feedback': 'stop'})
    assert not (missing_dir / 'graph-checkpoints.sqlite3').exists()

    with pytest.raises(ValueError):
        GraphRunner(_FakeServices(rundir2), rundir2).resume({'feedback': 'stop'})
