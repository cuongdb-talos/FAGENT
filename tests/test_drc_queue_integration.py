"""End-to-end integration tests for the DRC marker queue workflow."""

# === SECTION 1: Imports ===
import json
import sqlite3
from pathlib import Path

import gdstk

from FAGENT.capabilities.layout.input_loader import InputLoader
from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.graph_runner import GraphRunner
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.workflow_loader import WorkflowLoader
from FAGENT.tasks.drc.checklist import Checklist


# === SECTION 2: Module-Level Helpers ===
class ScriptedChatClient:
    """Deterministic chat client double that edits task-1 and fails task-2."""

    def run(self):
        return self

    def __init__(self):
        self.calls = 0
        self.tool_names_seen = []
        self.payloads = []
        self.requests = []
        self.task1_stage = 'init'

    def chat(self, messages, specs, response_schema):
        self.calls += 1
        self.requests.append((messages, specs, response_schema))
        self.tool_names_seen.append([spec['function']['name'] for spec in specs])
        last_msg = messages[-1]
        prev_tool = messages[-1] if messages and messages[-1].get('role') == 'tool' else None
        if prev_tool is not None:
            tool_out = json.loads(prev_tool['content'])
            if self.task1_stage == 'proposed' and tool_out.get('proposals'):
                self.task1_stage = 'applied'
                pid = tool_out['proposals'][0]['proposal_id']
                return {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [{
                        'id': 'call-app-1',
                        'type': 'function',
                        'function': {
                            'name': 'fix_cover',
                            'arguments': json.dumps({
                                'task_id': 'task-1',
                                'action': 'apply',
                                'proposal_id': pid,
                            }),
                        },
                    }],
                }
            if self.task1_stage == 'applied' and tool_out.get('status') == 'applied':
                self.task1_stage = 'checked'
                return {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [{
                        'id': 'call-chk-1',
                        'type': 'function',
                        'function': {
                            'name': 'check_cover',
                            'arguments': json.dumps({'task_id': 'task-1'}),
                        },
                    }],
                }
            return {
                'role': 'assistant',
                'content': json.dumps({
                    'decision': 'completed',
                    'status': 'incomplete',
                    'reason': 'Updated polygon for task-1',
                    'violations_analysis': [],
                    'recommendations': 'Run DRC deck validation',
                }),
            }
        if last_msg['role'] == 'user':
            payload = json.loads(last_msg['content'])
            self.payloads.append(payload)
            tid = payload['task_id']
            if tid == 'task-1' and self.task1_stage == 'init':
                self.task1_stage = 'proposed'
                layout_ctx = payload['layout']
                m1_id = next(p['id'] for p in layout_ctx['polygons'] if p['lpp'][0] == 61)
                ct_id = next(p['id'] for p in layout_ctx['polygons'] if p['lpp'][0] == 15)
                return {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [{
                        'id': 'call-prop-1',
                        'type': 'function',
                        'function': {
                            'name': 'fix_cover',
                            'arguments': json.dumps({
                                'task_id': 'task-1',
                                'action': 'propose',
                                'inner_id': ct_id,
                                'groups': [[m1_id]],
                            }),
                        },
                    }],
                }
            if tid == 'task-2':
                return {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [{
                        'id': 'call-fail-2',
                        'type': 'function',
                        'function': {
                            'name': 'fix_cover',
                            'arguments': json.dumps({
                                'task_id': 'task-2',
                                'action': 'propose',
                                'inner_id': 'NONEXISTENT:poly:999',
                                'groups': [['NONEXISTENT:poly:998']],
                            }),
                        },
                    }],
                }
        return {
            'role': 'assistant',
            'content': json.dumps({
                'decision': 'completed',
                'status': 'incomplete',
                'reason': 'Updated polygon for task-1',
                'violations_analysis': [],
                'recommendations': 'Run DRC deck validation',
            }),
        }


def create_sample_inputs(tmp_path: Path, cell_name: str = 'TEST_CELL') -> tuple[Path, Path, Path, Path]:
    """Create temporary GDS, Calibre DRC results, layermap, and rule deck."""
    gds_path = tmp_path / f'{cell_name}.gds'
    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell(cell_name)
    cell.add(gdstk.rectangle((0.0, 0.0), (0.2, 0.1), layer=61, datatype=0))
    cell.add(gdstk.rectangle((0.02, 0.02), (0.08, 0.08), layer=15, datatype=0))
    lib.write_gds(str(gds_path))

    drc_path = tmp_path / f'{cell_name}.drc.results'
    drc_text = '\n'.join([
        f'{cell_name} 1000',
        'CT_EN',
        '2 2 1 Nov 25 12:00:00 2026',
        'M1 enclosure of CT >= 0.025um',
        'p 1 4',
        '0 0',
        '100 0',
        '100 100',
        '0 100',
        'p 2 4',
        '50 50',
        '150 50',
        '150 150',
        '50 150',
        '',
    ])
    drc_path.write_text(drc_text, encoding='utf-8')

    layermap_path = tmp_path / 'test.layermap'
    layermap_path.write_text('M1 drawing 61 0\nCT drawing 15 0\n', encoding='utf-8')

    deck_path = tmp_path / 'rules.drc'
    deck_path.write_text('CT_EN {\n  @ M1 enclosure of CT >= 0.025um\n  ENC CT M1 < 0.025\n}\n', encoding='utf-8')
    return gds_path, drc_path, layermap_path, deck_path


def raise_disk_full():
    """Raise OSError to simulate a failed candidate checkpoint write."""
    raise OSError('Disk full')


# === SECTION 3: Tests ===
def test_drc_marker_queue_end_to_end_and_artifacts(tmp_path: Path, monkeypatch) -> None:
    """Exercise full DRC queue run with real GDS, InputLoader, WorkflowLoader, and SQLite checkpoint."""
    gds_path, drc_path, layermap_path, deck_path = create_sample_inputs(tmp_path, 'TEST_CELL')
    rundir = tmp_path / 'rundir'
    InputLoader(
        gds_path=gds_path,
        drc_path=drc_path,
        output_dir=rundir,
        layer_map_files=[layermap_path],
        verbose=False,
    ).run()

    workflow = WorkflowLoader(Path('FAGENT/tasks/drc/workflow.yaml'), Path('FAGENT/tasks').resolve()).run()
    config = {
        'goal': 'Fix all DRC errors.',
        'drc_files': [str(deck_path)],
        'max_iterations': 5,
        'rag_limit': 2,
        'rag_context_chars': 2000,
    }
    services = GraphServices(rundir, config, workflow, verbose=False)
    scripted = ScriptedChatClient()
    monkeypatch.setattr(services, 'build_chat_client', lambda: scripted)
    # Stub embedding client & rag to avoid loading local GGUF model files in unit tests
    services.rag = type('EmptyRag', (), {'retrieve': lambda self, q, r, l: []})()
    messages = services.prepare(resume=False)

    runner = GraphRunner(services, rundir)
    result = runner.run(messages, {'gds': str(gds_path), 'drc': str(drc_path), 'layer_map': [str(layermap_path)]})

    # 1. read_gds is not exposed to DRC agent tools
    assert all('read_gds' not in names for names in scripted.tool_names_seen)

    # 2. Queue exhausted and overall outcome is incomplete due to unresolved task-2
    assert result['status'] == 'incomplete'
    assert 'task-2' in result['reason']
    tasks = result['checklist']['tasks']
    assert [(t['task_id'], t['status']) for t in tasks] == [('task-1', 'pass'), ('task-2', 'unresolved')]

    # 3. Candidate GDS exists and contains task-1's updated polygon geometry
    candidate_path = rundir / 'candidate.gds'
    assert candidate_path.is_file()
    out_lib = gdstk.read_gds(str(candidate_path))
    out_polys = out_lib['TEST_CELL'].polygons
    assert len(out_polys) == 2
    assert any(min(float(pt[0]) for pt in poly.points) < 0.0 for poly in out_polys)

    # 4. Persisted checklist.json is valid for checkpoint restoration
    checklist_path = rundir / 'checklist.json'
    assert checklist_path.is_file()
    Checklist().restore(checklist_path, result['checklist_digest'])

    # 5. conversation.json and SQLite checkpoint exist and contain expected records
    conv_data = json.loads((rundir / 'conversation.json').read_text(encoding='utf-8'))
    assert conv_data['version'] == 2
    requests = [event for event in conv_data['events'] if event['type'] == 'model_request']
    assert len(requests) == len(scripted.requests)
    for event, (messages, specs, schema) in zip(requests, scripted.requests, strict=True):
        assert event['messages'] == messages
        assert event['tools'] == specs
        assert event['response_schema'] == schema
        assert [message['role'] for message in messages[:2]] == ['system', 'user']
        payload = json.loads(messages[1]['content'])
        if payload['task_id'] == 'task-2':
            assert not any(
                call['id'].endswith('-1')
                for message in messages[2:] for call in message.get('tool_calls', [])
            )
        assert {
            call['id'] for message in messages[2:] for call in message.get('tool_calls', [])
        } == {message['tool_call_id'] for message in messages[2:] if message['role'] == 'tool'}

    assert any(
        ev.get('type') == 'operation_start'
        and ev.get('name') == 'tool_call'
        and ev.get('inputs', {}).get('name') == 'fix_cover'
        for ev in conv_data['events']
    )

    ckpt_path = rundir / 'graph-checkpoints.sqlite3'
    assert ckpt_path.is_file()
    conn = sqlite3.connect(ckpt_path)
    try:
        rows = conn.execute('SELECT count(*) FROM checkpoints').fetchone()
        assert rows[0] > 0
    finally:
        conn.close()

    # 6. Task-2 received task-1's updated geometry, candidate digest, and task_history
    assert all('current_checklist' not in p and 'report' not in p for p in scripted.payloads)
    assert len(scripted.payloads[0]['layout']['polygons']) == 2
    task2_payload = next(p for p in scripted.payloads if p['task_id'] == 'task-2')
    assert task2_payload['task_history'] == []
    assert task2_payload['layout']['candidate_digest'] == result['candidate_digest']
    assert any(min(pt[0] for pt in p['points']) < 0.0 for p in task2_payload['layout']['polygons'])


def test_checkpoint_resume_binds_live_candidate_digest_and_geometry(tmp_path: Path, monkeypatch) -> None:
    """Resuming from a saved checkpoint restores live candidate GDS and binds its digest."""
    gds_path, drc_path, layermap_path, deck_path = create_sample_inputs(tmp_path, 'TEST_CELL')
    rundir = tmp_path / 'rundir'
    InputLoader(
        gds_path=gds_path,
        drc_path=drc_path,
        output_dir=rundir,
        layer_map_files=[layermap_path],
        verbose=False,
    ).run()

    workflow = WorkflowLoader(Path('FAGENT/tasks/drc/workflow.yaml'), Path('FAGENT/tasks').resolve()).run()
    config = {
        'goal': 'Fix all DRC errors.',
        'drc_files': [str(deck_path)],
        'max_iterations': 5,
        'rag_limit': 2,
        'rag_context_chars': 2000,
    }
    services1 = GraphServices(rundir, config, workflow, verbose=False)
    scripted1 = ScriptedChatClient()
    monkeypatch.setattr(services1, 'build_chat_client', lambda: scripted1)
    services1.rag = type('EmptyRag', (), {'retrieve': lambda self, q, r, l: []})()
    messages = services1.prepare(resume=False)
    runner1 = GraphRunner(services1, rundir)
    first_result = runner1.run(messages, {'gds': str(gds_path), 'drc': str(drc_path), 'layer_map': [str(layermap_path)]})
    saved_digest = first_result['candidate_digest']
    assert saved_digest

    # Fresh GraphServices + GraphRunner restoring candidate.gds and verifying live geometry
    services2 = GraphServices(rundir, config, workflow, verbose=False)
    services2.rag = type('EmptyRag', (), {'retrieve': lambda self, q, r, l: []})()
    services2.prepare(resume=True)
    services2.restore_candidate(rundir / 'candidate.gds', None, saved_digest)
    runner2 = GraphRunner(services2, rundir)
    state_snapshot = {
        'report': json.loads((rundir / 'drc.json').read_text(encoding='utf-8')),
        'checklist': json.loads((rundir / 'checklist.json').read_text(encoding='utf-8')),
        'candidate_path': str(rundir / 'candidate.gds'),
        'candidate_digest': saved_digest,
    }
    resumed_ctx = GraphNodes(services2).build_active_task_context(state_snapshot, 'task-2')
    assert resumed_ctx['layout']['candidate_digest'] == saved_digest
    assert any(min(pt[0] for pt in p['points']) < 0.0 for p in resumed_ctx['layout']['polygons'])


def test_failed_candidate_write_rolls_back_and_never_exposes_stale_digest(tmp_path: Path, monkeypatch) -> None:
    """When checkpoint_candidate fails, fix_cover rolls back and preserves candidate_digest."""
    gds_path, drc_path, layermap_path, deck_path = create_sample_inputs(tmp_path, 'TEST_CELL')
    rundir = tmp_path / 'rundir'
    InputLoader(
        gds_path=gds_path,
        drc_path=drc_path,
        output_dir=rundir,
        layer_map_files=[layermap_path],
        verbose=False,
    ).run()

    workflow = WorkflowLoader(Path('FAGENT/tasks/drc/workflow.yaml'), Path('FAGENT/tasks').resolve()).run()
    config = {
        'goal': 'Fix all DRC errors.',
        'drc_files': [str(deck_path)],
        'max_iterations': 5,
        'rag_limit': 2,
        'rag_context_chars': 2000,
    }
    services = GraphServices(rundir, config, workflow, verbose=False)
    scripted = ScriptedChatClient()
    monkeypatch.setattr(services, 'build_chat_client', lambda: scripted)
    services.rag = type('EmptyRag', (), {'retrieve': lambda self, q, r, l: []})()

    monkeypatch.setattr(services, 'checkpoint_candidate', raise_disk_full)
    messages = services.prepare(resume=False)
    runner = GraphRunner(services, rundir)
    result = runner.run(messages, {'gds': str(gds_path), 'drc': str(drc_path), 'layer_map': [str(layermap_path)]})

    assert result['candidate_digest'] is None
    assert not (rundir / 'candidate.gds').exists()
    # Second request for task-1 (after failed write) and task-2 must never see a non-None stale digest
    for payload in scripted.payloads[1:]:
        assert payload['layout']['candidate_digest'] is None
