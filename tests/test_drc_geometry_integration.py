# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import gdstk
import pytest

from langgraph.checkpoint.memory import MemorySaver

from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.capabilities.layout.polygon_ids import PolygonIds
from FAGENT.engine.graph_builder import GraphBuilder
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.workflow_loader import WorkflowLoader
from FAGENT.tasks.drc.checklist import Checklist


# === SECTION 2: Mock Stub Chat Client ===
class SequenceChatClient:
    """Stub chat client that feeds scripted responses sequentially and captures calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.captured_requests = []

    def run(self):
        return self

    def chat(self, messages, tools=None, response_schema=None):
        self.captured_requests.append([dict(m) for m in messages])
        if not self.responses:
            raise RuntimeError('No more scripted responses available in SequenceChatClient')
        return self.responses.pop(0)


# === SECTION 3: End-to-End Test Suite Class ===
class TestDrcGeometryIntegration:
    """End-to-end integration test suite verifying DRC report -> checklist -> plan -> geometry tools -> local recheck."""

    @pytest.fixture
    def environment(self, tmp_path: Path):
        """Prepare real workflow, test GDS layout, deck, layermap, and input JSON files."""
        root = Path('FAGENT/tasks').resolve()
        workflow = WorkflowLoader(root / 'drc' / 'workflow.yaml', root).run()

        rundir = tmp_path / 'rundir'
        rundir.mkdir(parents=True)
        gds_source = tmp_path / 'ROM_MCELL0.gds'

        lib = gdstk.Library(unit=1e-6, precision=1e-9)
        cell = lib.new_cell('ROM_MCELL0')
        # CT inner: 1000..1500 DBU
        p_ct = gdstk.Polygon([(1.0, 1.0), (1.5, 1.0), (1.5, 1.5), (1.0, 1.5)], layer=50, datatype=0)
        # M1 outer: left 982 DBU (18 DBU enclosure vs 25 required)
        p_m1 = gdstk.Polygon([(0.982, 0.950), (1.550, 0.950), (1.550, 1.550), (0.982, 1.550)], layer=61, datatype=0)
        cell.add(p_ct, p_m1)
        lib.write_gds(str(gds_source))

        deck_file = tmp_path / 'test_rules.drc'
        deck_file.write_text("""CT_EN_7a_7b {
@ CT_EN_7a: M1 enclosure of CT >= 0.025um
  X = RECTANGLE ENC CT M1 < 0.025 OPPOSITE
  (CT NOT M1) NOT INSIDE EXCLU
}
""")

        layermap_file = tmp_path / 'test.layermap'
        layermap_file.write_text("CT drawing 50 0\nM1 drawing 61 0\n")

        report = {
            'top_cell': 'ROM_MCELL0',
            'dbu_per_user_unit': 1000,
            'source': 'ROM_MCELL0.drc.results',
            'rules': [
                {
                    'name': 'CT_EN_7a_7b',
                    'reported_count': 1,
                    'total_count': 1,
                    'geometry': [{'id': 101, 'type': 'p', 'points_dbu': [[1000, 1000], [1500, 1500]]}],
                }
            ],
            'total_violations': 1,
        }

        gds_data = {'source': str(gds_source), 'top_cell': 'ROM_MCELL0'}
        (rundir / 'gds.json').write_text(json.dumps(gds_data))
        (rundir / 'drc.json').write_text(json.dumps(report))

        config = {
            'goal': 'Fix CT enclosure violations',
            'rag_limit': 1,
            'drc_files': [str(deck_file)],
            'layer_maps': [str(layermap_file)],
        }
        services = GraphServices(rundir, config, workflow)
        services.layout = LayoutService(rundir=rundir).run()
        services.drc_files = [deck_file]
        services.layer_maps = [layermap_file]
        services.rag = SimpleNamespace(retrieve=Mock(return_value=[{'rule': 'CT_EN_7a_7b', 'content': 'Enclosure text'}]))

        return services, workflow, rundir, gds_source

    def test_full_drc_flow_with_geometry_repair_and_local_recheck(self, environment):
        """Run complete graph: read -> plan -> get_violation -> propose -> apply -> recheck -> complete."""
        services, workflow, rundir, gds_source = environment

        # Scripted model conversation
        plan_resp = {
            'role': 'assistant',
            'content': json.dumps({
                'summary': 'Ordered repair sequence for CT enclosure',
                'steps': [
                    {'tool': 'get_violation', 'purpose': 'Inspect marker', 'task_id': 'task-1'},
                    {'tool': 'find_rule', 'purpose': 'Verify rule requirements', 'task_id': 'task-1'},
                    {'tool': 'find_related_polygons', 'purpose': 'Locate CT and M1 shapes', 'task_id': 'task-1'},
                    {'tool': 'measure_violation', 'purpose': 'Measure current gap', 'task_id': 'task-1'},
                    {'tool': 'propose_polygon_fix', 'purpose': 'Generate edit proposal', 'task_id': 'task-1'},
                    {'tool': 'apply_polygon_fix', 'purpose': 'Apply fix to candidate', 'task_id': 'task-1'},
                    {'tool': 'recheck_violation', 'purpose': 'Locally verify repair', 'task_id': 'task-1'},
                ],
            }),
        }

        # Step 1: Repair agent calls propose_polygon_fix directly (asserting no call-order enforcement)
        call_propose = {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {
                    'id': 'call-prop-01',
                    'type': 'function',
                    'function': {'name': 'propose_polygon_fix', 'arguments': '{"task_id": "task-1"}'},
                }
            ],
        }

        # Step 2: Repair agent applies the proposal returned
        call_apply = {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {
                    'id': 'call-apply-02',
                    'type': 'function',
                    'function': {
                        'name': 'apply_polygon_fix',
                        'arguments': '{"repair_id": "repair-task-1-ROM_MCELL0_poly_1-left"}',
                    },
                }
            ],
        }

        # Step 3: Repair agent calls recheck_violation
        call_recheck = {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {
                    'id': 'call-recheck-03',
                    'type': 'function',
                    'function': {'name': 'recheck_violation', 'arguments': '{"task_id": "task-1"}'},
                }
            ],
        }

        # Step 4: Repair agent concludes successfully
        call_finish = {
            'role': 'assistant',
            'content': json.dumps({
                'decision': 'completed',
                'status': 'verified',
                'reason': 'CT enclosure verified locally',
                'violations_analysis': [],
                'recommendations': 'none',
            }),
        }

        mock_client = SequenceChatClient([plan_resp, call_propose, call_apply, call_recheck, call_finish])
        services.client = mock_client

        graph = GraphBuilder(services, MemorySaver()).run()
        state = graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': str(gds_source), 'drc': 'drc', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'e2e_full_flow'}},
        )

        assert state.get('failed') is not True
        assert state.get('candidate_digest') is not None
        assert state.get('plan') is not None

        # Verify checklist is updated to 'pass'
        checklist = state.get('checklist')
        assert checklist is not None
        task = checklist['tasks'][0]
        assert task['status'] == 'pass'
        assert task['evidence']
        assert task['evidence'][0]['type'] == 'local_geometry_recheck'
        assert (rundir / 'candidate.gds').is_file()

    def test_e2e_replan_routing_on_tool_failure_bounded(self, environment):
        """Verify tool error triggers return to plan, records actual failure conversation, and reaches iteration bound."""
        services, workflow, rundir, gds_source = environment
        services.max_iterations = 2

        # Model repeatedly plans and calls a failing tool
        plan_resp = {
            'role': 'assistant',
            'content': json.dumps({
                'summary': 'Plan with unknown repair_id',
                'steps': [{'tool': 'apply_polygon_fix', 'purpose': 'Invalid apply', 'task_id': 'task-1'}],
            }),
        }
        call_failing = {
            'role': 'assistant',
            'content': None,
            'tool_calls': [
                {
                    'id': 'call-fail-01',
                    'type': 'function',
                    'function': {'name': 'apply_polygon_fix', 'arguments': '{"repair_id": "nonexistent_repair_id"}'},
                }
            ],
        }

        mock_client = SequenceChatClient([plan_resp, call_failing, plan_resp, call_failing, plan_resp, call_failing])
        services.client = mock_client

        graph = GraphBuilder(services, MemorySaver()).run()
        state = graph.invoke(
            {'messages': services.messages, 'inputs': {'gds': str(gds_source), 'drc': 'drc', 'layer_map': ['m']}, 'iteration': 0},
            {'configurable': {'thread_id': 'e2e_replan_bound'}},
        )

        assert state.get('failed') is True
        assert state.get('result', {}).get('status') == 'incomplete'
        assert 'iteration limit reached' in state.get('result', {}).get('reason')
