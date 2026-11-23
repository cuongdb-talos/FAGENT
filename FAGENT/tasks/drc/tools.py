"""Expose layout inspection and repair with explicit candidate evidence."""

# === SECTION 1: Imports ===
from pathlib import Path

from FAGENT.capabilities.layout.cover_check import CoverCheck
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult, ToolSpec


# === SECTION 2: Tools ===
class DrcTools:
    """Inspect cells and save repairs to a candidate layout."""

    def run(context, cell_name):
        """Read a cell and record successful inspection."""
        report = context.services.layout.read_gds(cell_name=cell_name)
        inspected = [*context.state.get("inspected", []), cell_name]
        return ToolResult(report, {"inspected": inspected})

    def __init__(self):
        """Initialize stateless tool handlers."""

    def replace(context, cell_name, polygon_id, points):
        """Require inspection, then save and fingerprint a modified candidate."""
        if cell_name not in context.state.get("inspected", []):
            raise ValueError("Inspect the target cell before editing")
        layout = context.services.layout
        result = layout.replace_polygon(cell_name=cell_name, polygon_id=polygon_id, points=points)
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

    def check_cover(context, cell_name, inner_id, outer_id, minimum):
        """Evaluate geometric enclosure between two direct polygons identified by stable IDs."""
        layout = context.services.layout
        try:
            layout.get_cell(cell_name)
            inner_poly = layout.polygon_ids.resolve(cell_name, inner_id)
            outer_poly = layout.polygon_ids.resolve(cell_name, outer_id)
            precision = float(layout.library.precision) / float(layout.library.unit)
            result = CoverCheck().run(inner_poly, outer_poly, minimum=minimum, precision=precision)
        except (OSError, ValueError, RuntimeError, TypeError) as exc:
            result = {
                "status": "unknown",
                "minimum": minimum,
                "measured_distance": None,
                "units": "gds_user_units",
                "violations": [],
                "reason": str(exc),
            }
        payload = {"cell": cell_name, "inner_id": inner_id, "outer_id": outer_id, **result}
        return ToolResult(payload, {})

    run = staticmethod(run)
    replace = staticmethod(replace)
    check_cover = staticmethod(check_cover)


read_gds = DrcTools.run
read_gds.task_tool = ToolSpec(
    function=read_gds,
    description="Inspect direct polygons in GDS user units before editing.",
    parameters={
        "type": "object",
        "properties": {"cell_name": {"type": "string"}},
        "required": ["cell_name"],
        "additionalProperties": False,
    },
)

replace_polygon = DrcTools.replace
replace_polygon.task_tool = ToolSpec(
    function=replace_polygon,
    description="Replace one direct polygon by stable polygon_id in GDS user units. Convert DBU coordinates using dbu_per_user_unit.",
    parameters={
        "type": "object",
        "properties": {
            "cell_name": {"type": "string"},
            "polygon_id": {"type": "string"},
            "points": {
                "type": "array",
                "minItems": 3,
                "items": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 2,
                    "items": {"type": "number"},
                },
            },
        },
        "required": ["cell_name", "polygon_id", "points"],
        "additionalProperties": False,
    },
)

check_cover = DrcTools.check_cover
check_cover.task_tool = ToolSpec(
    function=check_cover,
    description="Check geometric enclosure between an inner polygon and an outer covering polygon by stable polygon_id in GDS user units.",
    parameters={
        "type": "object",
        "properties": {
            "cell_name": {"type": "string"},
            "inner_id": {"type": "string"},
            "outer_id": {"type": "string"},
            "minimum": {"type": "number"},
        },
        "required": ["cell_name", "inner_id", "outer_id", "minimum"],
        "additionalProperties": False,
    },
)
