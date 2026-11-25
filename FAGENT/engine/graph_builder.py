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
        steps = getattr(self.services.workflow, 'nodes', None) or self.services.workflow.steps
        for step in steps:
            node = nodes.node_name(step.id)
            graph.add_node(node, partial(nodes.run_task_step, step=step))
            configured_routes = [
                END if target == 'END' else nodes.node_name(target)
                for target in getattr(step, 'routes', {}).values()
            ]
            routes = [END, *configured_routes]
            if step.next != 'END':
                routes.append(nodes.node_name(step.next))
            if not configured_routes and any(getattr(s, 'id', None) == 'save' for s in steps) and step.id != 'save':
                routes.append(nodes.node_name('save'))
            if step.action == 'review' and not configured_routes:
                routes.extend((node, nodes.node_name(step.continue_to)))
            if step.action == 'agent':
                tool_node = f'{step.id}--tools'
                routes.extend((node, tool_node))
                graph.add_node(tool_node, partial(nodes.run_task_step, step=step, tools=True))
                tool_routes = [END, node, *configured_routes]
                if step.next != 'END':
                    tool_routes.append(nodes.node_name(step.next))
                review_node = f'{step.id}--tool-review'
                graph.add_node(review_node, nodes.review_tool)
                graph.add_conditional_edges(
                    review_node,
                    partial(nodes.route_tool_review, step=step),
                    [END, node, review_node],
                )
                tool_routes.append(review_node)
                if any(s.id == 'plan' for s in steps):
                    tool_routes.append(nodes.node_name('plan'))
                graph.add_conditional_edges(tool_node, partial(nodes.route_task_tools, step=step), list(dict.fromkeys(tool_routes)))
            graph.add_conditional_edges(node, partial(nodes.route_task_step, step=step), list(dict.fromkeys(routes)))
        graph.add_edge(START, nodes.node_name(getattr(self.services.workflow, 'entry', steps[0].id)))
        self.graph = graph.compile(checkpointer=self.checkpointer)
        return self.graph

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, services, checkpointer):
        self.services = services
        self.checkpointer = checkpointer
        self.graph = None
