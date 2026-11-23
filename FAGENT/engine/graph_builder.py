"""Compile validated workflow steps into one LangGraph."""

# ==============================================================================
# SECTION 1: Third-Party LangGraph Imports
# ==============================================================================
from functools import partial

from langgraph.graph import END, START, StateGraph

# ==============================================================================
# SECTION 2: Internal FAGENT Module Imports
# ==============================================================================
from .fagent_state import FagentState
from .graph_nodes import GraphNodes


# ==============================================================================
# SECTION 3: Graph Compilation and Assembly Class
# ==============================================================================
class GraphBuilder:
    """Assembles declarative workflow steps into an executable LangGraph StateGraph."""

    # --------------------------------------------------------------------------
    # Main Graph Compilation Execution
    # --------------------------------------------------------------------------
    def run(self):
        graph = StateGraph(FagentState)
        nodes = GraphNodes(self.services)
        steps = self.services.workflow.steps
        for step in steps:
            node = nodes.node_name(step.id)
            graph.add_node(node, partial(nodes.run_task_step, step=step))
            routes = [END]
            if step.next != 'END':
                routes.append(nodes.node_name(step.next))
            if step.action == 'review':
                routes.extend((node, nodes.node_name(step.continue_to)))
            if step.action == 'agent':
                tool_node = f'{step.id}--tools'
                routes.extend((node, tool_node))
                graph.add_node(tool_node, partial(nodes.run_task_step, step=step, tools=True))
                graph.add_conditional_edges(tool_node, partial(nodes.route_task_tools, step=step), [END, node])
            graph.add_conditional_edges(node, partial(nodes.route_task_step, step=step), list(dict.fromkeys(routes)))
        graph.add_edge(START, nodes.node_name(steps[0].id))
        self.graph = graph.compile(checkpointer=self.checkpointer)
        return self.graph

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, services, checkpointer):
        self.services = services
        self.checkpointer = checkpointer
        self.graph = None
