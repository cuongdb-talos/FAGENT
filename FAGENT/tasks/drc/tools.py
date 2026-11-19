"""Expose layout inspection and repair with explicit candidate evidence."""

# === SECTION 1: Imports ===
from pathlib import Path

from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult, tool


# === SECTION 2: Tools ===
class DrcTools:
    """Inspect cells and save repairs to a candidate layout."""

    @staticmethod
    @tool(description="Inspect direct polygons in GDS user units before editing.",
          parameters={"type": "object", "properties": {"cell_name": {"type": "string"}},
                      "required": ["cell_name"], "additionalProperties": False})
    def run(context, cell_name):
        """Read a cell and record successful inspection."""
        report = context.services.layout.read_gds(cell_name=cell_name)
        inspected = [*context.state.get("inspected", []), cell_name]
        return ToolResult(report, {"inspected": inspected})

    def __init__(self):
        """Initialize stateless tool handlers."""

    @staticmethod
    @tool(description="Replace one direct polygon in GDS user units. Convert DBU coordinates using dbu_per_user_unit.",
          parameters={"type": "object", "properties": {
              "cell_name": {"type": "string"}, "polygon_index": {"type": "integer"},
              "points": {"type": "array", "minItems": 3, "items": {
                  "type": "array", "minItems": 2, "maxItems": 2, "items": {"type": "number"}}}},
              "required": ["cell_name", "polygon_index", "points"], "additionalProperties": False})
    def replace(context, cell_name, polygon_index, points):
        """Require inspection, then save and fingerprint a modified candidate."""
        if cell_name not in context.state.get("inspected", []):
            raise ValueError("Inspect the target cell before editing")
        layout = context.services.layout
        result = layout.replace_polygon(cell_name=cell_name, polygon_index=polygon_index, points=points)
        if result.get("status") != "modified":
            return result
        try:
            candidate = context.services.checkpoint_candidate()
            digest = GraphServices.candidate_digest(Path(candidate))
            if not digest:
                raise ValueError("Missing candidate digest")
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            return ToolResult({"error": type(exc).__name__}, {"candidate_path": None, "candidate_digest": None, "validation": None})
        return ToolResult(result, {"candidate_path": str(candidate), "candidate_digest": digest, "validation": None})


read_gds = DrcTools.run
replace_polygon = DrcTools.replace
