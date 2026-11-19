"""Compile validated workflow steps into one LangGraph."""
from langgraph.graph import END, START, StateGraph

from .nodes import GraphNodes
from .state import FagentState


def build_graph(services, checkpointer):
    nodes = GraphNodes(services).run()
    graph = StateGraph(FagentState)
    previous = START
    for step in services.workflow.steps:
        graph.add_node(step.id, nodes.actions[step.action])
        if previous != 'repair_tools':
            graph.add_edge(previous, step.id)
        if step.action == 'repair_drc':
            graph.add_node('repair_tools', nodes.repair_tools)
            graph.add_conditional_edges(step.id, nodes.route_repair, {'tools': 'repair_tools', 'done': next_step(services.workflow.steps, step.id)})
            graph.add_edge('repair_tools', step.id)
        previous = 'repair_tools' if step.action == 'repair_drc' else step.id
    resume_step = next((step.id for step in services.workflow.steps if step.action == 'repair_drc'), previous)
    graph.add_conditional_edges(previous, nodes.route_review, {'stop': END, 'continue': resume_step, 'unclear': previous})
    return graph.compile(checkpointer=checkpointer)


def next_step(steps, step_id):
    return steps[next(index for index, step in enumerate(steps) if step.id == step_id) + 1].id
