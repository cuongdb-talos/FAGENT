"""Module FAGENT.agent.nodes: Định nghĩa các Node và Routing Logic trong LangGraph.

Trách nhiệm & Chức năng:
- Cung cấp lớp `GraphNodes` triển khai các hành vi tại từng bước của đồ thị:
  + `model`: Gửi prompt tới LLM qua services.chat(), theo dõi số lượt lặp (iteration).
  + `tools`: Thực thi các tool call (read_gds, replace_polygon, save_gds, validate, store_knowledge)
    và cập nhật trạng thái candidate, vô hiệu hóa review cũ khi có sửa đổi hình học.
  + `finish`: Tạo bản tóm tắt kết quả cuối cùng của quá trình sửa lỗi.
  + `review`: Gọi interrupt() tạm dừng phiên làm việc để chờ phản hồi từ người dùng qua CLI.
- Điều hướng luồng thực thi thông qua các hàm conditional edges: `route_model`, `route_tools`, `route_review`.
"""
import json
from langgraph.types import interrupt
from .review import ReviewState


class GraphNodes:
    def run(self):
        return self

    def __init__(self, services):
        self.services = services

    def model(self, state):
        iteration = state.get('iteration', 0)
        limit = self.services.max_iterations
        if iteration >= limit:
            return {'result': {'status': 'incomplete', 'reason': 'Iteration limit reached'}}
        message = self.services.chat(state.get('messages', []))
        return {'messages': state.get('messages', []) + [message], 'iteration': iteration + 1}

    def route_model(self, state):
        if state.get('result'):
            return 'human_review'
        message = state['messages'][-1]
        return 'tools' if message.get('tool_calls') else 'finish'

    def tools(self, state):
        messages = list(state['messages'])
        validation = state.get('validation')
        review = state.get('review', {})
        registry = {tool.name: tool for tool in self.services.tools}
        save_requested = False
        candidate_path = state.get('candidate_path')
        result_state = state.get('result', {})
        verbose = getattr(self.services, 'verbose', getattr(getattr(self.services, 'caller', None), 'verbose', True))
        for call in messages[-1].get('tool_calls', []):
            call_id = call.get('id')
            if not call_id:
                raise ValueError('Tool call is missing its id')
            function = call.get('function', {})
            name = function.get('name', '')
            try:
                args = function.get('arguments', {})
                if isinstance(args, str):
                    args = json.loads(args)
                if verbose:
                    print(f"  * Tool Call: {name}({json.dumps(args, ensure_ascii=False)})")
                if name == 'store_knowledge':
                    evidence = ReviewState().run(review.get('feedback', ''))
                    if not review.get('authorized') or not evidence.authorized:
                        raise ValueError('Explicit completion and save request are required')
                    if not isinstance(args, dict) or not isinstance(args.get('analysis'), dict) or not isinstance(args['analysis'].get('violations_analysis'), list):
                        raise ValueError('Current structured analysis is required before saving')
                    validation_state = validation or {}
                    verified = validation_state.get('status') in ('verified', 'pass', 'good') and validation_state.get('simulated') is False
                    result_state = {'status': 'verified' if verified else 'incomplete', 'reason': 'DRC validation passed' if verified else 'Agent stopped without verified DRC', 'response': args['analysis']}
                    save_requested = True
                    messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps({'status': 'approved_for_storage'})})
                    break
                if name not in registry or not isinstance(args, dict):
                    raise ValueError('Unknown tool or invalid arguments')
                if name in ('replace_polygon', 'validate'):
                    review = {}
                if name == 'validate':
                    validation = None
                result = registry[name].invoke(args)
                if name == 'replace_polygon':
                    validation = None
                    checkpoint_candidate = getattr(self.services, 'checkpoint_candidate', None)
                    if checkpoint_candidate is not None:
                        candidate_path = None
                        candidate_path = checkpoint_candidate()
                if name == 'validate':
                    validation = result
            except Exception as exc:
                result = {'error': str(exc)}
            messages.append({'role': 'tool', 'tool_call_id': call_id, 'content': json.dumps(result)})
        return {'messages': messages, 'validation': validation, 'review': review, 'save_requested': save_requested, 'result': result_state, 'candidate_path': candidate_path}

    def route_tools(self, state):
        return 'end' if state.get('save_requested') else 'model'

    def finish(self, state):
        validation = state.get('validation') or {}
        verified = validation.get('status') in ('verified', 'pass', 'good') and validation.get('simulated') is False
        response = json.loads(state['messages'][-1]['content'])
        result = {'status': 'verified' if verified else 'incomplete', 'reason': 'DRC validation passed' if verified else 'Agent stopped without verified DRC', 'response': response}
        return {'result': result}

    def review(self, state):
        feedback = interrupt({'result': state.get('result'), 'validation': state.get('validation')})
        if feedback is None or feedback == {'finished': True} or str(feedback).strip().lower() == 'stop':
            return {'review_finished': True}
        evidence = ReviewState().run(feedback)
        return {'review': {'authorized': evidence.authorized, **evidence.evidence},
                'messages': state.get('messages', []) + [{'role': 'user', 'content': feedback}],
                'result': {}, 'iteration': 0, 'review_finished': False}

    def route_review(self, state):
        return 'end' if state.get('review_finished') else 'model'
