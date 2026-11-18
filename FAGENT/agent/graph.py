"""Construction of the checkpointed FAGENT workflow."""
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
    graph.add_conditional_edges('model', nodes.route_model, {'tools': 'tools', 'finish': 'finish', 'human_review': 'human_review'})
    graph.add_conditional_edges('tools', nodes.route_tools, {'end': END, 'model': 'model'})
    graph.add_edge('finish', 'human_review')
    graph.add_conditional_edges('human_review', nodes.route_review, {'end': END, 'model': 'model'})
    return graph.compile(checkpointer=checkpointer)
