"""Expose layout inspection and repair with explicit candidate evidence."""

# === SECTION 1: Imports ===
import copy
from pathlib import Path

from FAGENT.capabilities.layout.cover_check import CoverCheck
from FAGENT.capabilities.layout.repair_proposals import RepairProposals
from FAGENT.capabilities.layout.violation_geometry import ViolationGeometry
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult, ToolSpec
from FAGENT.tasks.drc.checklist import Checklist
from FAGENT.tasks.drc.rule_evidence import RuleEvidence


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
            if hasattr(layout, "rollback_last_replace"):
                layout.rollback_last_replace()
            return ToolResult({"error": type(exc).__name__}, {"candidate_path": None, "candidate_digest": None, "validation": None})
        updates = {"candidate_path": str(candidate), "candidate_digest": digest, "validation": None}
        if context.state.get("checklist"):
            checklist = Checklist.from_dict(context.state["checklist"])
            checklist_snapshot = checklist.invalidate(cell_name, polygon_id)
            updates["checklist"] = checklist_snapshot
            updates["checklist_digest"] = checklist_snapshot["digest"]
            updates["checklist_path"] = None
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

    def _resolve_rule_inputs(context):
        """Resolve DRC deck paths and physical layer map files from services, layout, or config."""
        services = context.services
        cfg = getattr(services, "config", {}) or {}
        input_sources = getattr(services, "input_sources", None) or {}
        layout = getattr(services, "layout", None)
        gds_data = getattr(layout, "gds_data", None) or {}

        deck_paths = (
            getattr(services, "drc_files", None)
            or input_sources.get("decks")
            or getattr(layout, "drc_files", None)
            or cfg.get("drc_files", [])
        )
        layer_maps = (
            getattr(services, "layer_maps", None)
            or input_sources.get("layer_maps")
            or gds_data.get("layer_maps")
            or cfg.get("layer_maps", [])
            or cfg.get("layer_map_files", [])
        )
        references = context.state.get("drc_context", {}).get("knowledge", {}).get("references", [])
        return deck_paths, layer_maps, references

    def read_checklist(context):
        """Return the current run-scoped violation checklist."""
        raw = context.state.get("checklist")
        if not raw:
            raise ValueError("Checklist has not been initialized for this run")
        checklist = Checklist.from_dict(raw)
        return ToolResult(checklist.read(), {})

    def get_violation(context, task_id):
        """Lookup source DRC violation record, marker coordinates, task state, and candidate identity."""
        raw = context.state.get("checklist")
        if not raw:
            raise ValueError("Checklist has not been initialized for this run")
        checklist = Checklist.from_dict(raw)
        task = next((t for t in checklist.tasks if t.get("task_id") == task_id), None)
        if task is None:
            raise ValueError(f"Unknown checklist task_id: {task_id}")

        report = context.state.get("report")
        if not report and hasattr(context.services, "drc_report"):
            report = context.services.drc_report
        if not report and context.state.get("drc_context"):
            report = context.state["drc_context"].get("drc")
        if not report:
            raise ValueError("DRC report data is unavailable in context")

        rule_name = task.get("rule")
        vid = task.get("violation_id")
        occ = task.get("occurrence", 0)

        matched_geom = None
        for r in report.get("rules", []):
            if r.get("name") == rule_name:
                current_occ = 0
                for g in r.get("geometry", []):
                    if g.get("id") == vid:
                        if current_occ == occ:
                            matched_geom = g
                            break
                        current_occ += 1
                if matched_geom is not None:
                    break

        candidate_path = context.state.get("candidate_path")
        candidate_digest = context.state.get("candidate_digest")

        payload = {
            "task": copy.deepcopy(task),
            "violation": copy.deepcopy(matched_geom) if matched_geom else None,
            "rule": rule_name,
            "owning_cell": report.get("top_cell"),
            "dbu_per_user_unit": report.get("dbu_per_user_unit"),
            "source": report.get("source"),
            "candidate_path": candidate_path,
            "candidate_digest": candidate_digest,
        }
        return ToolResult(payload, {})

    def find_rule(context, task_id):
        """Retrieve original rule text, source locations, and physical layer maps."""
        raw = context.state.get("checklist")
        if not raw:
            raise ValueError("Checklist has not been initialized for this run")
        checklist = Checklist.from_dict(raw)
        task = next((t for t in checklist.tasks if t.get("task_id") == task_id), None)
        if task is None:
            raise ValueError(f"Unknown checklist task_id: {task_id}")

        deck_paths, layer_maps, references = DrcTools._resolve_rule_inputs(context)

        if not deck_paths:
            evidence = {'rule': task.get('rule'), 'found': False, 'supported': False, 'reason': 'No DRC rule decks configured', 'layer_mapping': {}}
        else:
            evidence = RuleEvidence().run(task, deck_paths=deck_paths, layer_maps=layer_maps, references=references)
        return ToolResult(evidence, {})

    def find_related_polygons(context, task_id, search_margin_dbu=0):
        """Resolve DRC marker to candidate layout polygons with candidate digest binding."""
        v_result = DrcTools.get_violation(context, task_id)
        task = v_result.payload.get("task") or {}
        task["violation"] = v_result.payload.get("violation")
        task["owning_cell"] = v_result.payload.get("owning_cell")

        deck_paths, layer_maps, references = DrcTools._resolve_rule_inputs(context)

        if not deck_paths:
            rule_evidence = {'rule': task.get('rule'), 'found': False, 'supported': False, 'reason': 'No DRC rule decks configured', 'layer_mapping': {}}
        else:
            rule_evidence = RuleEvidence().run(task, deck_paths=deck_paths, layer_maps=layer_maps, references=references)
        geom_result = ViolationGeometry().run(
            task=task,
            rule_evidence=rule_evidence,
            layout=context.services.layout,
            candidate_digest=context.state.get("candidate_digest"),
            search_margin_dbu=search_margin_dbu,
        )
        return ToolResult(geom_result, {})

    def measure_violation(context, task_id, polygon_ids=None):
        """Measure geometric enclosure or distance for a specific violation task."""
        v_result = DrcTools.get_violation(context, task_id)
        task = v_result.payload.get("task") or {}
        task["violation"] = v_result.payload.get("violation")
        task["owning_cell"] = v_result.payload.get("owning_cell")

        deck_paths, layer_maps, references = DrcTools._resolve_rule_inputs(context)

        if not deck_paths:
            rule_evidence = {'rule': task.get('rule'), 'found': False, 'supported': False, 'reason': 'No DRC rule decks configured', 'layer_mapping': {}}
        else:
            rule_evidence = RuleEvidence().run(task, deck_paths=deck_paths, layer_maps=layer_maps, references=references)
        geom_result = ViolationGeometry().run(
            task=task,
            rule_evidence=rule_evidence,
            layout=context.services.layout,
            candidate_digest=context.state.get("candidate_digest"),
        )
        proposals_result = RepairProposals().run(
            task=task,
            rule_evidence=rule_evidence,
            geometry=geom_result,
            layout=context.services.layout,
            candidate_digest=context.state.get("candidate_digest"),
        )
        return ToolResult(proposals_result["measurement"], {})

    def propose_polygon_fix(context, task_id, polygon_ids=None):
        """Generate deterministic repair proposals for a measured violation."""
        v_result = DrcTools.get_violation(context, task_id)
        task = v_result.payload.get("task") or {}
        task["violation"] = v_result.payload.get("violation")
        task["owning_cell"] = v_result.payload.get("owning_cell")

        deck_paths, layer_maps, references = DrcTools._resolve_rule_inputs(context)

        if not deck_paths:
            rule_evidence = {'rule': task.get('rule'), 'found': False, 'supported': False, 'reason': 'No DRC rule decks configured', 'layer_mapping': {}}
        else:
            rule_evidence = RuleEvidence().run(task, deck_paths=deck_paths, layer_maps=layer_maps, references=references)
        geom_result = ViolationGeometry().run(
            task=task,
            rule_evidence=rule_evidence,
            layout=context.services.layout,
            candidate_digest=context.state.get("candidate_digest"),
        )
        proposals_result = RepairProposals().run(
            task=task,
            rule_evidence=rule_evidence,
            geometry=geom_result,
            layout=context.services.layout,
            candidate_digest=context.state.get("candidate_digest"),
        )
        proposals_list = proposals_result["proposals"]
        proposals_map = {p["repair_id"]: p for p in proposals_list}
        existing = dict(context.state.get("proposals") or {})
        existing.update(proposals_map)
        updates = {"proposals": existing}
        payload = {"task_id": task_id, "proposals": proposals_list}
        reason = proposals_result.get("reason") or proposals_result.get("measurement", {}).get("reason", "")
        if reason:
            payload["reason"] = reason
        return ToolResult(payload, updates)

    def apply_polygon_fix(context, repair_id):
        """Apply a staged repair proposal to the candidate layout transactionally."""
        proposals = context.state.get("proposals") or {}
        proposal = proposals.get(repair_id)
        if not proposal:
            raise ValueError(f"Unknown repair_id: {repair_id}")

        expected_digest = proposal.get("candidate_digest")
        current_digest = context.state.get("candidate_digest")
        if expected_digest != current_digest:
            raise ValueError(f"Stale repair proposal {repair_id}: candidate layout has changed (expected {expected_digest}, current {current_digest})")

        assumptions = proposal.get("rule_assumptions", "")
        if not assumptions:
            raise ValueError("Proposal lacking verified rule assumptions cannot be applied")

        cell_name = proposal["cell"]
        polygon_id = proposal["target_polygon_id"]
        points = proposal["after_points_user"]

        layout = context.services.layout
        result = layout.replace_polygon(cell_name=cell_name, polygon_id=polygon_id, points=points)
        if result.get("status") != "modified":
            return ToolResult(result, {})

        try:
            candidate = context.services.checkpoint_candidate()
            digest = GraphServices.candidate_digest(Path(candidate))
            if not digest:
                raise ValueError("Missing candidate digest after checkpoint")
        except Exception:
            if hasattr(layout, "rollback_last_replace"):
                layout.rollback_last_replace()
            raise

        updates = {
            "candidate_path": str(candidate),
            "candidate_digest": digest,
            "validation": None,
            "proposals": {},
        }
        if context.state.get("checklist"):
            checklist = Checklist.from_dict(context.state["checklist"])
            checklist_snapshot = checklist.invalidate(cell_name, polygon_id)
            updates["checklist"] = checklist_snapshot
            updates["checklist_digest"] = checklist_snapshot["digest"]
            updates["checklist_path"] = None

        payload = {
            "status": "applied",
            "repair_id": repair_id,
            "cell": cell_name,
            "polygon_id": polygon_id,
            "candidate_digest": digest,
        }
        return ToolResult(payload, updates)

    def recheck_violation(context, task_id):
        """Evaluate supported local geometry on the saved candidate layout."""
        meas_res = DrcTools.measure_violation(context, task_id)
        meas = meas_res.payload
        status = meas.get("status")
        candidate_digest = context.state.get("candidate_digest")

        updates = {}
        if status == "pass":
            local_verified = True
            raw = context.state.get("checklist")
            if raw:
                checklist = Checklist.from_dict(raw)
                evidence_rec = {
                    "type": "local_geometry_recheck",
                    "status": "pass",
                    "candidate_digest": candidate_digest,
                    "measurement": meas,
                }
                snapshot = checklist.update(
                    task_id=task_id,
                    status="pass",
                    reason="Locally verified geometry pass on candidate layout (local geometry only, not full DRC deck clean)",
                    evidence=[evidence_rec],
                )
                updates = {
                    "checklist": snapshot,
                    "checklist_digest": snapshot["digest"],
                    "checklist_path": str(Path(context.services.rundir) / "checklist.json"),
                }
        else:
            local_verified = False

        payload = {
            "status": status,
            "task_id": task_id,
            "local_verified": local_verified,
            "deck_clean": False,
            "notice": "Local geometry recheck only; not a deck-level DRC pass.",
            "measurement": meas,
            "candidate_digest": candidate_digest,
        }
        return ToolResult(payload, updates)

    def skip_plan_step(context, task_id, tool, reason):
        """Explicitly record that a conditional active plan step was skipped with a reason."""
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be a non-empty string")
        if not isinstance(tool, str) or not tool.strip():
            raise ValueError("tool must be a non-empty string")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason must be a non-empty string")
        return ToolResult(
            {
                "status": "skipped",
                "skipped_task_id": task_id.strip(),
                "skipped_tool": tool.strip(),
                "reason": reason.strip(),
            },
            {},
        )

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
        return ToolResult(
            snapshot,
            {
                "checklist": snapshot,
                "checklist_digest": snapshot["digest"],
                "checklist_path": str(Path(context.services.rundir) / "checklist.json"),
            },
        )

    def query_layout_region(context, cell_name, bounds_user, layers=None):
        """Query additional candidate GDS geometry within bounds_user without editing state."""
        layout = getattr(context.services, "layout", None)
        if layout is None:
            raise ValueError("Layout service is unavailable")
        payload = ViolationGeometry().query_region(
            layout,
            cell_name,
            bounds_user,
            candidate_digest=context.state.get("candidate_digest"),
            layer_specs=layers,
        )
        return ToolResult(payload, {})

    run = staticmethod(run)
    replace = staticmethod(replace)
    check_cover = staticmethod(check_cover)
    _resolve_rule_inputs = staticmethod(_resolve_rule_inputs)
    read_checklist = staticmethod(read_checklist)
    get_violation = staticmethod(get_violation)
    find_rule = staticmethod(find_rule)
    find_related_polygons = staticmethod(find_related_polygons)
    measure_violation = staticmethod(measure_violation)
    propose_polygon_fix = staticmethod(propose_polygon_fix)
    apply_polygon_fix = staticmethod(apply_polygon_fix)
    recheck_violation = staticmethod(recheck_violation)
    update_checklist = staticmethod(update_checklist)
    query_layout_region = staticmethod(query_layout_region)


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

get_violation = DrcTools.get_violation
get_violation.task_tool = ToolSpec(
    function=get_violation,
    description="Lookup source DRC violation record, marker coordinates, task state, and candidate identity.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

find_rule = DrcTools.find_rule
find_rule.task_tool = ToolSpec(
    function=find_rule,
    description="Retrieve named rule text, line numbers, and physical layer maps from the rule deck.",
    parameters={
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

find_related_polygons = DrcTools.find_related_polygons
find_related_polygons.task_tool = ToolSpec(
    function=find_related_polygons,
    description="Find live layout polygons overlapping the DRC violation marker on physical layers.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "search_margin_dbu": {"type": "number"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

measure_violation = DrcTools.measure_violation
measure_violation.task_tool = ToolSpec(
    function=measure_violation,
    description="Measure geometric enclosure or distances for a violation task against physical layout polygons.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "polygon_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

propose_polygon_fix = DrcTools.propose_polygon_fix
propose_polygon_fix.task_tool = ToolSpec(
    function=propose_polygon_fix,
    description="Propose deterministic geometry edits with before/after coordinates to repair a measured violation.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "polygon_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

apply_polygon_fix = DrcTools.apply_polygon_fix
apply_polygon_fix.task_tool = ToolSpec(
    function=apply_polygon_fix,
    description="Apply a staged repair proposal to the candidate layout transactionally.",
    parameters={
        "type": "object",
        "properties": {
            "repair_id": {"type": "string"},
        },
        "required": ["repair_id"],
        "additionalProperties": False,
    },
)

recheck_violation = DrcTools.recheck_violation
recheck_violation.task_tool = ToolSpec(
    function=recheck_violation,
    description="Evaluate supported local geometry on the saved candidate layout and bind evidence to candidate digest.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

skip_plan_step = DrcTools.skip_plan_step
skip_plan_step.task_tool = ToolSpec(
    function=skip_plan_step,
    description="Report that a conditional plan step (such as apply_polygon_fix or recheck_violation) was skipped for a task with a reason.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "tool": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["task_id", "tool", "reason"],
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

query_layout_region = DrcTools.query_layout_region
query_layout_region.task_tool = ToolSpec(
    function=query_layout_region,
    description="Query candidate GDS geometry in a user-unit bounding box [min_x, min_y, max_x, max_y] to expand evidence for the current task.",
    parameters={
        "type": "object",
        "properties": {
            "cell_name": {"type": "string", "minLength": 1},
            "bounds_user": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 4,
                "maxItems": 4,
            },
            "layers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "layer": {"type": "integer"},
                        "datatype": {"type": "integer"},
                    },
                    "required": ["layer", "datatype"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["cell_name", "bounds_user"],
        "additionalProperties": False,
    },
)
