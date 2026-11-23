# === SECTION 1: Imports ===
from datetime import datetime
import json
from pathlib import Path
import pytest

from FAGENT.engine.conversation_trace import ConversationTrace

# === SECTION 2: Test Suite ===


class TestReadableTrace:
    """Test suite for ConversationTrace lifecycle operations, formatting, and compatibility."""

    def test_readable_trace_correlates_calls_and_preserves_json(self, tmp_path: Path):
        """Verify model requests, responses, and tool outcomes correlate and persist."""
        trace = ConversationTrace(tmp_path)
        trace.append({
            'type': 'model_request',
            'request_id': '1',
            'messages': [{'role': 'user', 'content': 'large input'}],
            'tools': [],
            'response_schema': {},
        })
        trace.append({
            'type': 'model_response',
            'request_id': '1',
            'message': {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{
                    'id': 'call-1',
                    'function': {'name': 'read_gds', 'arguments': '{"cell_name":"cell_example"}'},
                }],
            },
        })
        trace.append({
            'type': 'tool_result',
            'message': {'role': 'tool', 'tool_call_id': 'call-1', 'content': '{"error":"missing cell"}'},
        })
        log = (tmp_path / 'trace.log').read_text()
        assert log.index('MODEL REQUEST 1') < log.index('MODEL RESPONSE 1') < log.index('TOOL RESULT')
        assert 'read_gds' in log and 'call-1' in log
        assert '"cell_name": "cell_example"' in log
        assert '"error": "missing cell"' in log
        assert 'large input' not in log
        assert len(json.loads((tmp_path / 'conversation.json').read_text())['events']) == 3

    def test_readable_trace_rebuilds_from_existing_trace(self, tmp_path: Path):
        """Verify trace.log is rebuilt from conversation.json on initialization."""
        events = [{'type': 'model_error', 'request_id': '7', 'error_type': 'RuntimeError', 'error_message': 'model unavailable'}]
        (tmp_path / 'conversation.json').write_text(json.dumps({'version': 2, 'events': events}))
        ConversationTrace(tmp_path)
        log = (tmp_path / 'trace.log').read_text()
        assert 'MODEL ERROR 7' in log
        assert 'RuntimeError: model unavailable' in log

    def test_operation_lifecycle_structured_payloads_and_utc_timestamps(self, tmp_path: Path):
        """Verify start_operation and finish_operation emit structured events with paired IDs and UTC timestamps."""
        trace = ConversationTrace(tmp_path)
        op_id = trace.start_operation('drc_read', {'report_path': '/path/to/ROM_MCELL0.drc.results'})
        assert isinstance(op_id, str) and len(op_id) > 0

        trace.finish_operation(
            op_id,
            'drc_read',
            status='completed',
            outputs={'violations_count': 2, 'rules': ['RULE_X', 'RULE_Y']},
        )

        trace_data = json.loads((tmp_path / 'conversation.json').read_text())
        assert trace_data.get('version') == 2
        events = trace_data.get('events', [])
        assert len(events) == 2

        start_event = events[0]
        assert start_event.get('type') == 'operation_start'
        assert start_event.get('operation_id') == op_id
        assert start_event.get('name') == 'drc_read'
        assert start_event.get('inputs') == {'report_path': '/path/to/ROM_MCELL0.drc.results'}
        ts_start = start_event.get('timestamp', '')
        assert ts_start.endswith('+00:00') or ts_start.endswith('Z')
        datetime.fromisoformat(ts_start)

        finish_event = events[1]
        assert finish_event.get('type') == 'operation_finish'
        assert finish_event.get('operation_id') == op_id
        assert finish_event.get('name') == 'drc_read'
        assert finish_event.get('status') == 'completed'
        assert finish_event.get('outputs') == {'violations_count': 2, 'rules': ['RULE_X', 'RULE_Y']}
        assert finish_event.get('error') is None
        ts_finish = finish_event.get('timestamp', '')
        assert ts_finish.endswith('+00:00') or ts_finish.endswith('Z')
        datetime.fromisoformat(ts_finish)

    def test_operation_error_recording(self, tmp_path: Path):
        """Verify failed operations record status and error details."""
        trace = ConversationTrace(tmp_path)
        op_id = trace.start_operation('service_setup', {'config': {'task': 'drc'}})
        trace.finish_operation(op_id, 'service_setup', status='failed', error='Connection refused to EDA daemon')

        trace_data = json.loads((tmp_path / 'conversation.json').read_text())
        events = trace_data.get('events', [])
        assert len(events) == 2
        finish_event = events[1]
        assert finish_event.get('status') == 'failed'
        assert finish_event.get('error') == 'Connection refused to EDA daemon'

        log = (tmp_path / 'trace.log').read_text()
        assert 'FAILED' in log or 'failed' in log
        assert 'Connection refused to EDA daemon' in log

    def test_operation_resume_id_no_collision(self, tmp_path: Path):
        """Verify resume increments operation ID without colliding with prior events."""
        trace1 = ConversationTrace(tmp_path)
        op_id1 = trace1.start_operation('prep', {'step': 1})
        trace1.finish_operation(op_id1, 'prep', status='completed')

        trace2 = ConversationTrace(tmp_path)
        op_id2 = trace2.start_operation('prep_resume', {'step': 2})
        assert op_id2 != op_id1
        assert len(trace2.events) == 3

    def test_readable_trace_summarizes_operations(self, tmp_path: Path):
        """Verify trace.log displays chronological summaries for DRC and RAG operations."""
        trace = ConversationTrace(tmp_path)
        op_drc = trace.start_operation('read_drc', {'report_path': '/path/to/report.drc'})
        trace.finish_operation(op_drc, 'read_drc', status='completed', outputs={'violations_count': 2, 'rules': ['RULE_X', 'RULE_Y']})

        op_rag = trace.start_operation('rag_retrieve', {'query': 'Find rules', 'rules': ['RULE_X'], 'limit': 3})
        trace.finish_operation(
            op_rag,
            'rag_retrieve',
            status='completed',
            outputs={'results_count': 1, 'records': [{'id': 'rec-1', 'score': 0.95}]},
        )

        log = (tmp_path / 'trace.log').read_text()
        assert 'read_drc' in log or 'READ DRC' in log
        assert 'violations_count' in log or 'violations=2' in log or '2' in log
        assert 'rag_retrieve' in log or 'RAG RETRIEVE' in log
        assert 'rec-1' in log or 'records' in log
