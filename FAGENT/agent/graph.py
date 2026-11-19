"""Module FAGENT.agent.graph: Xây dựng và biên dịch StateGraph cho quy trình FAGENT.

Trách nhiệm & Chức năng:
- Cung cấp hàm `build_graph()` khởi tạo StateGraph với FagentState.
- Thiết lập các Node xử lý: 'model', 'tools', 'finish', 'human_review'.
- Thiết lập các Edge điều kiện:
  + Từ 'model' rẽ nhánh sang 'tools', 'finish' hoặc 'human_review'.
  + Từ 'tools' quay lại 'model' hoặc kết thúc 'end' (nếu store_knowledge thành công).
  + Từ 'human_review' quay lại 'model' khi có feedback hoặc kết thúc 'end'.
- Biên dịch đồ thị kèm bộ kiểm tra điểm phục hồi (checkpointer) SqliteSaver.
"""
from langgraph.graph import END, START, StateGraph
from .nodes import GraphNodes
from .state import FagentState


def build_graph(services, checkpointer):
    nodes = GraphNodes(services).run()
    graph = StateGraph(FagentState)
    graph.add_node('model', nodes.model)
    graph.add_node('tools', nodes.tools)
    graph.add_node('finish', nodes.finish)
    graph.add_node('human_review', nodes.review)
    graph.add_edge(START, 'model')
    graph.add_conditional_edges(
        'model', nodes.route_model, 
        {
            'tools': 'tools', 
            'finish': 'finish', 
            'human_review': 'human_review'})
    graph.add_conditional_edges(
        'tools', nodes.route_tools, {'end': END, 'model': 'model'})
    graph.add_edge('finish', END)
    graph.add_conditional_edges('human_review', nodes.route_review, {'end': END, 'model': 'model'})
    return graph.compile(checkpointer=checkpointer)
