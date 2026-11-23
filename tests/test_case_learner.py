# === SECTION 1: Imports ===
import json
from pathlib import Path
from unittest.mock import Mock

from FAGENT.rag.case_learner import CaseLearner

# === SECTION 2: Test Suite Class ===


class TestCaseLearnerTraceCompatibility:
    """Test suite verifying CaseLearner compatibility with version 2 conversation trace."""

    def test_case_learner_extracts_equivalent_legacy_and_version2_conversations(self, tmp_path):
        run_dir = tmp_path / 'run_case'
        run_dir.mkdir()

        result_data = {
            'status': 'verified',
            'validation': {'status': 'verified', 'simulated': False},
            'response': {'violations_analysis': [{'rule': 'RULE_1', 'detail': 'fixed'}]},
        }
        drc_data = {
            'rules': [{'name': 'RULE_1', 'description': 'desc 1'}],
        }

        (run_dir / 'result.json').write_text(json.dumps(result_data), encoding='utf-8')
        (run_dir / 'drc.json').write_text(json.dumps(drc_data), encoding='utf-8')

        assistant_msg1 = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-1', 'function': {'name': 'read_gds', 'arguments': '{}'}}]}
        tool_msg1 = {'role': 'tool', 'tool_call_id': 'call-1', 'content': json.dumps({'polygons': 5})}
        assistant_msg2 = {'role': 'assistant', 'content': json.dumps({'decision': 'completed'})}

        legacy_conversation = [
            {'role': 'user', 'content': 'prompt 1'},
            assistant_msg1,
            tool_msg1,
            {'role': 'user', 'content': 'prompt 2'},
            assistant_msg2,
        ]
        (run_dir / 'conversation.json').write_text(json.dumps(legacy_conversation), encoding='utf-8')

        mock_rag = Mock()
        mock_rag.index_records.return_value = ['rec-1']
        mock_rag.knowledge_dir = Path('FAGENT/knowledge').resolve()

        review = {'storage_consent': True, 'authorized': True, 'consent_source': 'human_review'}

        learner_legacy = CaseLearner(rundir=run_dir, knowledge_dir=Path('FAGENT/knowledge').resolve(), config={})
        learner_legacy.rag = mock_rag
        learner_legacy.run(review=review)

        assert learner_legacy.learned is True
        rec_legacy = learner_legacy.extracted_records[-1]
        data_legacy = json.loads(rec_legacy['content'])
        fingerprint_legacy = rec_legacy['id'].split('run:')[-1]

        v2_trace = {
            'version': 2,
            'events': [
                {'type': 'model_request', 'request_id': '1', 'messages': [{'role': 'user', 'content': 'prompt 1'}], 'tools': [], 'response_schema': {}},
                {'type': 'model_response', 'request_id': '1', 'message': assistant_msg1},
                {'type': 'tool_result', 'message': tool_msg1},
                {'type': 'model_request', 'request_id': '2', 'messages': [{'role': 'user', 'content': 'prompt 2'}], 'tools': [], 'response_schema': {}},
                {'type': 'model_response', 'request_id': '2', 'message': assistant_msg2},
            ],
        }
        (run_dir / 'conversation.json').write_text(json.dumps(v2_trace), encoding='utf-8')

        learner_v2 = CaseLearner(rundir=run_dir, knowledge_dir=Path('FAGENT/knowledge').resolve(), config={})
        learner_v2.rag = mock_rag
        learner_v2.run(review=review)

        assert learner_v2.learned is True
        rec_v2 = learner_v2.extracted_records[-1]
        data_v2 = json.loads(rec_v2['content'])
        fingerprint_v2 = rec_v2['id'].split('run:')[-1]

        assert data_legacy['conversation'] == [assistant_msg1, tool_msg1, assistant_msg2]
        assert data_v2['conversation'] == [assistant_msg1, tool_msg1, assistant_msg2]
        assert fingerprint_legacy == fingerprint_v2

    def test_case_learner_handles_empty_trace(self, tmp_path):
        run_dir = tmp_path / 'run_empty'
        run_dir.mkdir()
        (run_dir / 'result.json').write_text(json.dumps({'status': 'verified', 'validation': {'status': 'verified'}}))
        (run_dir / 'drc.json').write_text(json.dumps({'rules': []}))
        (run_dir / 'conversation.json').write_text(json.dumps({'version': 2, 'events': []}))

        mock_rag = Mock()
        mock_rag.index_records.return_value = []
        mock_rag.knowledge_dir = Path('FAGENT/knowledge').resolve()

        review = {'storage_consent': True, 'authorized': True, 'consent_source': 'human_review'}
        learner = CaseLearner(rundir=run_dir, knowledge_dir=Path('FAGENT/knowledge').resolve(), config={})
        learner.rag = mock_rag
        learner.run(review=review)

        assert learner.learned is True
        run_rec = learner.extracted_records[-1]
        assert json.loads(run_rec['content'])['conversation'] == []

    def test_case_learner_handles_failed_call_events_in_trace(self, tmp_path):
        run_dir = tmp_path / 'run_failed'
        run_dir.mkdir()
        (run_dir / 'result.json').write_text(json.dumps({'status': 'verified', 'validation': {'status': 'verified'}}))
        (run_dir / 'drc.json').write_text(json.dumps({'rules': []}))
        v2_trace = {
            'version': 2,
            'events': [
                {'type': 'model_request', 'request_id': '1', 'messages': [{'role': 'user', 'content': 'prompt 1'}], 'tools': [], 'response_schema': {}},
                {'type': 'model_error', 'request_id': '1', 'error_type': 'RuntimeError', 'error_message': 'Model crash'},
            ],
        }
        (run_dir / 'conversation.json').write_text(json.dumps(v2_trace))

        mock_rag = Mock()
        mock_rag.index_records.return_value = []
        mock_rag.knowledge_dir = Path('FAGENT/knowledge').resolve()

        review = {'storage_consent': True, 'authorized': True, 'consent_source': 'human_review'}
        learner = CaseLearner(rundir=run_dir, knowledge_dir=Path('FAGENT/knowledge').resolve(), config={})
        learner.rag = mock_rag
        learner.run(review=review)

        assert learner.learned is True
        run_rec = learner.extracted_records[-1]
        assert json.loads(run_rec['content'])['conversation'] == []
