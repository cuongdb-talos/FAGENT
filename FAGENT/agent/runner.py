"""Module FAGENT.agent.runner: Trình điều phối thực thi đồ thị và quản lý Artifacts / Checkpoints.

Trách nhiệm & Chức năng:
- Cung cấp lớp `GraphRunner` quản lý toàn bộ vòng đời thực thi của LangGraph:
  + Khởi tạo cơ sở dữ liệu SQLite checkpoint (`graph-checkpoints.sqlite3`) để lưu trạng thái từng bước.
  + Xử lý các điểm ngắt `interrupt` tại node human_review và tiếp nhận input từ người dùng qua CLI.
  + Hỗ trợ phục hồi thread làm việc cũ từ checkpoint thông qua resume-thread.
  + Lưu trữ artifacts cuối cùng vào rundir: `conversation.json` (lịch sử hội thoại) và `result.json` (kết quả và lý do kết thúc).
"""
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .graph import build_graph


class GraphRunner:
    def run(self, messages):
        self.prepare_graph()
        state = self.graph.invoke({'messages': messages, 'iteration': 0}, self.graph_config)
        return self.finish(state)

    def __init__(self, services, rundir, review_provider=None, checkpointer=None, thread_id=None):
        self.services = services
        self.rundir = Path(rundir)
        self.review_provider = review_provider or self.prompt_review
        self.checkpointer = checkpointer
        self.connection = None
        self.thread_id = thread_id or uuid4().hex
        self.graph = None
        self.messages = []
        self.result = {}
        self.graph_config = None

    def prepare_graph(self):
        self.rundir.mkdir(parents=True, exist_ok=True)
        if self.checkpointer is None:
            checkpoint_path = self.rundir / 'graph-checkpoints.sqlite3'
            self.services.check_output_path(checkpoint_path)
            self.connection = sqlite3.connect(checkpoint_path, check_same_thread=False)
            self.checkpointer = SqliteSaver(self.connection)
        self.graph = build_graph(self.services, self.checkpointer)
        self.graph_config = {'configurable': {'thread_id': self.thread_id},
                             'recursion_limit': max(25, self.services.max_iterations * 4 + 10)}

    def resume(self, feedback):
        self.prepare_graph()
        snapshot = self.graph.get_state(self.graph_config)
        if snapshot.next != ('human_review',) or not any(task.interrupts for task in snapshot.tasks):
            raise ValueError('No graph paused at review is available for this thread')
        candidate = snapshot.values.get('candidate_path')
        if candidate:
            self.services.restore_candidate(candidate, snapshot.values.get('validation'))
        state = self.graph.invoke(Command(resume=feedback if feedback is not None else {'finished': True}), self.graph_config)
        return self.finish(state)

    def finish(self, state):
        while state.get('__interrupt__'):
            try:
                feedback = self.review_provider()
            except EOFError:
                feedback = None
            state = self.graph.invoke(Command(resume=feedback if feedback is not None else {'finished': True}), self.graph_config)
        self.result = dict(state.get('result') or {'status': 'incomplete', 'reason': 'No result'})
        self.result['validation'] = state.get('validation')
        self.result['review'] = state.get('review') or {'authorized': False}
        self.messages = state.get('messages', [])
        artifacts = ((self.rundir / 'conversation.json', self.messages), (self.rundir / 'result.json', self.result))
        for target, _ in artifacts:
            self.services.check_output_path(target)
        for target, value in artifacts:
            target.write_text(json.dumps(value, indent=2) + '\n')
        if state.get('save_requested') and self.result['review'].get('authorized'):
            self.services.store_knowledge(self.result['review'])
        return self.result

    def prompt_review(self):
        return input(f'Review task [thread {self.thread_id}] (EOF to finish): ')
