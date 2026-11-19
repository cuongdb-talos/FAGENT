"""Module FAGENT.agent.services: Dependency Injection Container cho đồ thị LangGraph.

Trách nhiệm & Chức năng:
- Cung cấp lớp `GraphServices` khởi tạo và tiêm phụ thuộc (dependencies) vào các node của graph.
- Quản lý các thành phần cốt lõi:
  + LayoutService: Thao tác hình học GDS trực tiếp qua gdstk.
  + LlamaAdapter: Mô hình suy luận và tính toán vector embedding cục bộ.
  + HybridRetriever: Cơ chế tìm kiếm và truy xuất tri thức sửa lỗi DRC.
  + CaseLearner: Học và lưu trữ các ca sửa lỗi thành công.
- Đóng gói danh sách công cụ hoàn chỉnh (`tools`) thông qua `build_tools()`.
- Quản lý các cấu hình giới hạn thực thi (`max_iterations`, `rag_context_chars`).
"""
import json
from pathlib import Path
import gdstk
from langchain_core.utils.function_calling import convert_to_openai_tool
from ..models.llama_adapter import LlamaAdapter
from ..rag.learning import CaseLearner
from ..rag.retriever import HybridRetriever
from ..tools.layout import LayoutService
from ..tools.agent_tools import build_tools
from .prompts import build_system_prompt


class GraphServices:
    def run(self):
        self.max_iterations = self.caller.config['max_iterations']
        self.tools = build_tools(self.caller.functions)
        return self

    def __init__(self, caller):
        self.caller = caller
        self.tools = []
        self.max_iterations = None
        self.learner = None

    def prepare(self):
        caller = self.caller
        caller.functions = LayoutService(
            rundir=caller.rundir,
            verbose=caller.verbose,
            drc_files=caller.config.get('drc_files', []),
        ).run()
        caller.gds_data = json.loads((caller.rundir / 'gds.json').read_text())
        caller.drc_data = json.loads((caller.rundir / 'drc.json').read_text())
        caller.client = LlamaAdapter(caller.config)
        caller.rag = HybridRetriever(caller.config.get('knowledge_dir'), caller.config, embedding_client=caller.client).run()
        caller.rag.migrate_legacy(caller.rag.knowledge_dir / 'knowledge_db.json')
        references = caller.config.get('reference_dir')
        if references:
            caller.rag.index_documents(references)
        caller.rag.index_rule_decks(caller.config.get('drc_files', []))
        caller.drc_context = caller.initial_drc_context()
        self.run()
        rules = caller.drc_context.get('rules') or [{'name': key} for key in caller.drc_context.get('violations_by_rule', {})]
        names = [rule.get('name', '') for rule in rules if rule.get('name')]
        query = ' '.join(names + [str(rule.get('description', '')) for rule in rules])
        retrieved = caller.rag.retrieve(query, names, caller.config['rag_limit']) if query else []
        user_payload = {'gds': caller.gds_data, 'drc': caller.drc_context}
        if caller.config.get('goal'):
            user_payload = {'goal': caller.config['goal'], **user_payload}
        self.messages = [{'role': 'system', 'content': build_system_prompt(caller.config, retrieved)},
                         {'role': 'user', 'content': json.dumps(user_payload)}]
        return self.messages

    def chat(self, messages):
        specs = [convert_to_openai_tool(tool) for tool in self.tools]
        message = self.caller.chat(messages, specs=specs)
        thinking = getattr(self.caller.client, 'last_thinking', None) or message.get('reasoning_content') or message.get('thinking')
        if self.caller.verbose and thinking:
            print(f"[Thinking]\n{thinking}")
        return message

    def checkpoint_candidate(self):
        return self.caller.functions.save_gds()['candidate']

    def restore_candidate(self, path, validation):
        layout = self.caller.functions
        candidate = Path(path).resolve()
        if candidate != layout.candidate.resolve() or not candidate.is_file():
            raise ValueError('Invalid checkpoint candidate path')
        layout.check_output_path(candidate)
        layout.library = gdstk.read_gds(str(candidate))
        layout.dirty = True
        layout.last_validation = validation

    def check_output_path(self, path):
        return self.caller.functions.check_output_path(path)

    def store_knowledge(self, review):
        caller = self.caller
        self.learner = CaseLearner(rundir=caller.rundir, knowledge_dir=caller.rag.knowledge_dir,
                                   config=caller.config, client=caller.client, verbose=caller.verbose).run(review=review)
        return self.learner
