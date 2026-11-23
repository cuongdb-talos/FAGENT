# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from FAGENT.engine.conversation_trace import ConversationTrace
from FAGENT.engine.graph_nodes import GraphNodes
from FAGENT.engine.graph_runner import GraphRunner
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult, ToolSpec
from FAGENT.tasks.drc.steps import Steps as DrcSteps

# === SECTION 2: Helpers ===


def bad_tool_handler(ctx, **kwargs):
    """Module-level failing tool callable for tests."""
    raise ValueError('Geometry check failed')


# === SECTION 3: Test Suite ===


class TestFlowBoundariesTrace:
    """Integration test suite for flow boundaries, preparation, nodes, routing, tools, and resume tracing."""

    def test_preparation_trace_success_and_failure(self, tmp_path: Path):
        """Verify preparation emits start/finish operations and captures errors on failure."""
        base = tmp_path / 'base'
        base.mkdir()
        output = tmp_path / 'output'
        output.mkdir()
        (base / 'cells.txt').write_text('CELL_A\n')
        (base / 'layer_map').write_text('1 0 M1\n')
        (base / 'deck.drc').write_text('// rule deck\n')
        drc_dir = base / 'drc'
        gds_dir = base / 'gds'
        drc_dir.mkdir()
        gds_dir.mkdir()
        (drc_dir / 'CELL_A.drc.results').write_text('0\n')
        (gds_dir / 'CELL_A.gds').write_text('mock_gds')

        setup = {'layer_map_files': ['layer_map'], 'list_cells': 'cells.txt'}
        task_cfg = {'drc_folder': 'drc', 'gds_folder': 'gds', 'drc_files': ['deck.drc'], 'task_name': 'drc'}
        config = {'goal': 'test'}

        drc_steps = DrcSteps()
        # Mock InputLoader to succeed
        with patch('FAGENT.tasks.drc.steps.InputLoader') as mock_loader:
            mock_inst = Mock()
            mock_loader.return_value = mock_inst
            runs = list(drc_steps.prepare(task_cfg, setup, base, output, config, verbose=False))
            assert len(runs) == 1
            rundir = runs[0][0]
            trace_path = rundir / 'conversation.json'
            assert trace_path.is_file()
            data = json.loads(trace_path.read_text())
            ops = [e for e in data['events'] if e.get('name') == 'preparation']
            assert len(ops) == 2
            assert ops[0]['type'] == 'operation_start'
            assert ops[0]['inputs']['cell'] == 'CELL_A'
            assert ops[1]['type'] == 'operation_finish'
            assert ops[1]['status'] == 'completed'

        # Now test failure propagation and failure trace event
        (base / 'cells.txt').write_text('CELL_FAIL\n')
        (drc_dir / 'CELL_FAIL.drc.results').write_text('0\n')
        (gds_dir / 'CELL_FAIL.gds').write_text('mock_gds')
        with patch('FAGENT.tasks.drc.steps.InputLoader') as mock_fail_loader:
            mock_fail_loader.side_effect = RuntimeError('Corrupt GDS file')
            with pytest.raises(RuntimeError, match='Corrupt GDS file'):
                list(drc_steps.prepare(task_cfg, setup, base, output, config, verbose=False))

            # Check that CELL_FAIL rundir has failed preparation event
            cell_runs = list((output / 'CELL_FAIL' / 'runs').glob('*/rundir'))
            assert len(cell_runs) == 1
            fail_rundir = cell_runs[0]
            fail_trace = json.loads((fail_rundir / 'conversation.json').read_text())
            fail_ops = [e for e in fail_trace['events'] if e.get('name') == 'preparation']
            assert len(fail_ops) == 2
            assert fail_ops[1]['status'] == 'failed'
            assert 'Corrupt GDS file' in str(fail_ops[1]['error'])

    def test_service_setup_and_finalization_trace(self, tmp_path: Path):
        """Verify service setup, graph node execution, artifact writing, and finalization emit trace events."""
        step1 = SimpleNamespace(id='read', action='step', prompt='read prompt', schema={}, tools={}, next='END', function=lambda ctx: {'read_done': True})
        workflow = SimpleNamespace(
            task='drc',
            version=1,
            digest='wf_digest',
            steps=[step1],
            layout_services=False,
            response_schema={'type': 'object'},
            prompt_text=['system prompt'],
            protocol_text='',
            reference_label='',
            knowledge_review=False,
            inputs=[],
        )
        config = {'goal': 'verify'}
        services = GraphServices(tmp_path, config, workflow)
        services.prepare()

        # Check service setup was traced
        trace_data = json.loads((tmp_path / 'conversation.json').read_text())
        service_ops = [e for e in trace_data['events'] if e.get('name') == 'service_setup']
        assert len(service_ops) == 2
        assert service_ops[0]['type'] == 'operation_start'
        assert service_ops[1]['type'] == 'operation_finish'
        assert service_ops[1]['status'] == 'completed'

        runner = GraphRunner(services, tmp_path)
        result = runner.run(messages=[{'role': 'user', 'content': 'start'}])

        trace_data2 = json.loads((tmp_path / 'conversation.json').read_text())
        node_ops = [e for e in trace_data2['events'] if e.get('name') == 'graph_node']
        assert len(node_ops) >= 2
        assert any(e.get('inputs', {}).get('step_id') == 'read' for e in node_ops)

        final_ops = [e for e in trace_data2['events'] if e.get('name') == 'finalization']
        assert len(final_ops) == 2
        assert final_ops[1]['status'] == 'completed'

        artifact_ops = [e for e in trace_data2['events'] if e.get('name') == 'artifact_write']
        assert len(artifact_ops) >= 2

        log = (tmp_path / 'trace.log').read_text()
        assert 'SERVICE SETUP' in log or 'service_setup' in log
        assert 'FINALIZATION' in log or 'finalization' in log

    def test_tool_failure_tracing(self, tmp_path: Path):
        """Verify tool execution failure emits failed operation and error details."""
        tool_spec = ToolSpec(
            function=bad_tool_handler,
            description='A failing tool',
            parameters={'type': 'object', 'properties': {'x': {'type': 'number'}}},
        )
        step = SimpleNamespace(
            id='repair',
            action='agent',
            tools={'bad_tool': tool_spec},
            prompt='',
            schema={},
            next='END',
        )
        workflow = SimpleNamespace(
            task='drc',
            version=1,
            digest='wf_digest',
            steps=[step],
            layout_services=False,
            response_schema={'type': 'object'},
            prompt_text=['system prompt'],
            protocol_text='',
            reference_label='',
            knowledge_review=False,
            inputs=[],
        )
        services = GraphServices(tmp_path, {'goal': 'test'}, workflow)
        nodes = GraphNodes(services)

        state = {
            'inputs': {},
            'task_state': {},
            'messages': [{
                'role': 'assistant',
                'tool_calls': [{'id': 'call-10', 'function': {'name': 'bad_tool', 'arguments': '{"x": 1}'}}],
            }],
        }
        updates = nodes.run_task_step(state, step, tools=True)
        assert updates.get('pending_tools') is False

        trace_data = json.loads((tmp_path / 'conversation.json').read_text())
        tool_ops = [e for e in trace_data['events'] if e.get('name') == 'tool_call']
        assert len(tool_ops) == 2
        assert tool_ops[1]['status'] == 'failed'
        assert 'Geometry check failed' in str(tool_ops[1]['error'])

    def test_resume_trace_records_operation(self, tmp_path: Path):
        """Verify GraphRunner.resume emits resume operation without colliding IDs."""
        step_repair = SimpleNamespace(
            id='repair',
            action='agent',
            prompt='',
            schema={},
            tools={},
            next='human_review',
        )
        step_review = SimpleNamespace(
            id='human_review',
            action='review',
            prompt='',
            schema={},
            tools={},
            next='END',
            continue_to='repair',
        )
        workflow = SimpleNamespace(
            task='drc',
            version=1,
            digest='wf_digest',
            steps=[step_repair, step_review],
            layout_services=False,
            response_schema={'type': 'object'},
            prompt_text=['system prompt'],
            protocol_text='',
            reference_label='',
            knowledge_review=False,
            inputs=[],
        )
        services = GraphServices(tmp_path, {'goal': 'test'}, workflow)
        runner = GraphRunner(services, tmp_path, review_provider=lambda: {'feedback': 'approve', 'storage_consent': False})
        runner.prepare_graph()

        # Call resume with a mock get_state
        mock_snapshot = SimpleNamespace(
            next=['human_review'],
            tasks=[SimpleNamespace(interrupts=[SimpleNamespace(value={'result': {'status': 'incomplete'}})])],
            values={'identity': runner.workflow_identity(), 'result': {'status': 'incomplete'}},
        )
        with patch.object(runner.graph, 'get_state', return_value=mock_snapshot), \
             patch.object(runner, 'resume_graph', return_value={'result': {'status': 'incomplete'}}):
            res = runner.resume(feedback={'feedback': 'stop'})
            assert res['status'] == 'incomplete'

        trace_data = json.loads((tmp_path / 'conversation.json').read_text())
        resume_ops = [e for e in trace_data['events'] if e.get('name') == 'resume']
        assert len(resume_ops) == 2
        assert resume_ops[0]['type'] == 'operation_start'
        assert resume_ops[1]['type'] == 'operation_finish'
        assert resume_ops[1]['status'] == 'completed'
