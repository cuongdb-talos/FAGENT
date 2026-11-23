"""Expose layout inspection and repair with explicit candidate evidence."""

# === SECTION 1: Imports ===
from pathlib import Path

from FAGENT.capabilities.layout.cover_check import CoverCheck
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult, ToolSpec
from FAGENT.tasks.drc.checklist import Checklist


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
        updates = {"candidate_path": str(candidate), "candidate_digest": digest, "validation": None}
        if context.state.get("checklist"):
            checklist = Checklist.from_dict(context.state["checklist"])
            checklist_snapshot = checklist.invalidate(cell_name, polygon_id)
            checklist_path = Path(context.services.rundir) / "checklist.json"
            context.services.check_output_path(checklist_path)
            checklist.save(checklist_path)
            updates["checklist"] = checklist_snapshot
            updates["checklist_digest"] = checklist_snapshot["digest"]
            updates["checklist_path"] = str(checklist_path)
        return ToolResult(result, updates)

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

    def read_checklist(context):
        """Return the current run-scoped violation checklist."""
        raw = context.state.get("checklist")
        if not raw:
            raise ValueError("Checklist has not been initialized for this run")
        checklist = Checklist.from_dict(raw)
        return ToolResult(checklist.read(), {})

    def update_checklist(context, task_id, status, work=None, reason="", polygon_ids=None, evidence=None):
        """Update one existing checklist task and persist the updated checklist atomically."""
        raw = context.state.get("checklist")
        if not raw:
            raise ValueError("Checklist has not been initialized for this run")
        checklist = Checklist.from_dict(raw)
        snapshot = checklist.update(
            task_id=task_id,
            status=status,
            work=work,
            reason=reason,
            polygon_ids=polygon_ids,
            evidence=evidence,
        )
        checklist_path = Path(context.services.rundir) / "checklist.json"
        context.services.check_output_path(checklist_path)
        checklist.save(checklist_path)
        return ToolResult(
            snapshot,
            {
                "checklist": snapshot,
                "checklist_digest": snapshot["digest"],
                "checklist_path": str(checklist_path),
            },
        )

    run = staticmethod(run)
    replace = staticmethod(replace)
    check_cover = staticmethod(check_cover)
    read_checklist = staticmethod(read_checklist)
    update_checklist = staticmethod(update_checklist)


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

read_checklist = DrcTools.read_checklist
read_checklist.task_tool = ToolSpec(
    function=read_checklist,
    description="Read the current run-scoped DRC repair checklist and task statuses.",
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)

update_checklist = DrcTools.update_checklist
update_checklist.task_tool = ToolSpec(
    function=update_checklist,
    description="Update an existing DRC checklist task status, work description, reason, polygon_ids, and evidence.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "pass", "skipped", "unresolved"]},
            "work": {"type": "string"},
            "reason": {"type": "string"},
            "polygon_ids": {"type": "array", "items": {"type": "string"}},
            "evidence": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["task_id", "status", "reason"],
        "additionalProperties": False,
    },
)
