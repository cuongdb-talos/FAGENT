"""Expose the five DRC repair, check, and save tools with transactional candidate state."""

# === SECTION 1: Imports ===
import copy
import shutil
from pathlib import Path

from FAGENT.capabilities.layout.cover_check import AreaCheck, CoverCheck
from FAGENT.capabilities.layout.repair_proposals import AreaProposalBuilder, CoverProposalBuilder
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult, ToolSpec


# === SECTION 2: Five-Tool DRC Handler Class ===
class DrcTools:
    """Five-tool DRC repair interface with staged proposals and atomic transactions."""

    # --------------------------------------------------------------------------
    # Main Entry Point & Initialization
    # --------------------------------------------------------------------------
    def run(self):
        """Expose the five registered DRC tool specifications."""
        self.tools = [fix_cover, fix_area, check_cover, check_area, save_gds]
        return self.tools

    def __init__(self):
        """Initialize stateless tool handler container."""
        self.tools = []

    # --------------------------------------------------------------------------
    # Public Tool Methods
    # --------------------------------------------------------------------------
    def fix_cover(
        context,
        task_id: str,
        action: str,
        inner_id: str | None = None,
        groups: list[list[str]] | None = None,
        proposal_id: str | None = None,
        minimum_enclosure: float | None = None,
    ) -> ToolResult:
        """Propose or atomically apply a cover repair using model-selected polygon IDs."""
        if action not in ("propose", "apply"):
            return ToolResult({"status": "rejected", "reason": f"Unsupported action: {action}"}, {})
        if action == "apply":
            return DrcTools._apply_proposal(context, task_id=task_id, expected_kind="cover", proposal_id=proposal_id)

        resolved, err = DrcTools._resolve_task_context(context, task_id)
        if err:
            return ToolResult({"status": "unknown", "reason": err, "proposals": []}, {})

        from FAGENT.engine.graph_nodes import GraphNodes
        from FAGENT.tasks.drc.rule_evidence import cover_requirements

        if context.state.get('current_task_id') == task_id:
            focused = GraphNodes(context.services).build_active_task_context(context.state, task_id)
            region = focused.get('layout') or {}
            coverage = cover_requirements(
                focused.get('drc', {}).get('rule_evidence', {}), region, region.get('polygons', []), resolved['layout'],
            )
            if coverage is not None:
                if coverage.get('reason'):
                    return ToolResult({"status": "unknown", "reason": coverage['reason'], "proposals": []}, {})
                expected = coverage['group_polygon_ids']
                if (inner_id not in coverage['inner_ids'] or minimum_enclosure != 0
                        or not isinstance(groups, list) or len(groups) != len(expected)
                        or any(not isinstance(group, list) or not group or not set(group) <= set(allowed)
                               for group, allowed in zip(groups, expected))):
                    return ToolResult({
                        "status": "rejected", "reason": "Use one group per required coverage condition",
                        "coverage": coverage, "proposals": [],
                    }, {})

        if minimum_enclosure is None:
            return ToolResult({"status": "unknown", "reason": "minimum_enclosure is required for a proposal", "proposals": []}, {})

        if not inner_id or not isinstance(groups, list) or not groups or any(not g for g in groups):
            return ToolResult({"status": "unknown", "reason": "inner_id and non-empty groups are required", "proposals": []}, {})

        layout = resolved["layout"]
        cell_name = resolved["cell_name"]
        try:
            inner = layout.polygon_ids.resolve(cell_name, inner_id)
            cover_polygons = [[layout.polygon_ids.resolve(cell_name, pid) for pid in group] for group in groups]
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult({"status": "unknown", "reason": f"Unknown polygon_id: {exc}", "proposals": []}, {})

        if any(inner_id in group for group in groups):
            return ToolResult({"status": "unknown", "reason": "Cover groups cannot contain the inner polygon", "proposals": []}, {})

        built = CoverProposalBuilder().run(
            inner_points=inner.points.tolist(),
            groups=[[poly.points.tolist() for poly in group] for group in cover_polygons],
            minimum_enclosure=float(minimum_enclosure),
            grid=resolved["grid"],
            inner_id=inner_id,
            group_ids=groups,
        )
        return DrcTools._stage_built_proposals(
            context,
            resolved=resolved,
            task_id=task_id,
            kind="cover",
            built=built,
            bound_inputs={"inner_id": inner_id, "groups": groups, "minimum_enclosure": float(minimum_enclosure)},
        )

    def fix_area(
        context,
        task_id: str,
        action: str,
        polygon_id: str | None = None,
        minimum_area: float | None = None,
        proposal_id: str | None = None,
    ) -> ToolResult:
        """Propose or atomically apply an area repair using a model-selected polygon ID."""
        if action not in ("propose", "apply"):
            return ToolResult({"status": "rejected", "reason": f"Unsupported action: {action}"}, {})
        if action == "apply":
            return DrcTools._apply_proposal(context, task_id=task_id, expected_kind="area", proposal_id=proposal_id)

        resolved, err = DrcTools._resolve_task_context(context, task_id)
        if err:
            return ToolResult({"status": "unknown", "reason": err, "proposals": []}, {})

        if minimum_area is None:
            return ToolResult({"status": "unknown", "reason": "minimum_area is required for a proposal", "proposals": []}, {})
        if not polygon_id:
            return ToolResult({"status": "unknown", "reason": "polygon_id is required", "proposals": []}, {})

        layout = resolved["layout"]
        cell_name = resolved["cell_name"]
        try:
            poly_pts = layout.polygon_ids.resolve(cell_name, polygon_id).points.tolist()
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult({"status": "unknown", "reason": f"Unknown polygon_id: {exc}", "proposals": []}, {})

        built = AreaProposalBuilder().run(
            polygon_points=poly_pts,
            minimum_area=float(minimum_area),
            grid=resolved["grid"],
            polygon_id=polygon_id,
        )
        return DrcTools._stage_built_proposals(
            context,
            resolved=resolved,
            task_id=task_id,
            kind="area",
            built=built,
            bound_inputs={"polygon_id": polygon_id, "minimum_area": float(minimum_area)},
        )

    def check_cover(context, task_id: str) -> ToolResult:
        """Verify staged cover transaction on live layout; rollback all task edits on failure."""
        return DrcTools._check_transaction(context, task_id=task_id, expected_kind="cover")

    def check_area(context, task_id: str) -> ToolResult:
        """Verify staged area transaction on live layout; rollback all task edits on failure."""
        return DrcTools._check_transaction(context, task_id=task_id, expected_kind="area")

    def save_gds(context) -> ToolResult:
        """Save the final verified candidate GDS once the task queue is complete."""
        if context.state.get("current_task_id") or context.state.get("task_queue"):
            return ToolResult(
                {"status": "rejected", "reason": "save_gds is only available after queue completion"},
                {},
            )
        saved = Path(context.services.checkpoint_candidate())
        digest = GraphServices.candidate_digest(saved)
        if not digest:
            raise ValueError("Missing candidate digest after save_gds")

        history = context.state.get("task_history") or []
        summary = {
            "total": len(history),
            "pass": sum(1 for item in history if item.get("status") == "pass"),
            "unresolved": sum(1 for item in history if item.get("status") == "unresolved"),
        }
        return ToolResult(
            {"status": "saved", "output_path": str(saved), "digest": digest, "task_summary": summary},
            {"candidate_path": str(saved), "candidate_digest": digest, "save_completed": True},
        )

    # --------------------------------------------------------------------------
    # Internal Transaction & Resolution Helpers
    # --------------------------------------------------------------------------
    def _resolve_task_context(context, task_id: str) -> tuple[dict | None, str | None]:
        """Resolve the task, candidate layout, target cell, and GDS grid."""
        checklist = context.state.get("checklist") or {}
        task = next((t for t in checklist.get("tasks", []) if t.get("task_id") == task_id), None)
        if task is None:
            return None, f"Unknown task_id: {task_id}"

        layout = context.services.layout
        layout.load_library()
        cell_name = (
            task.get("cell")
            or (context.state.get("report") or {}).get("top_cell")
            or (layout.gds_data or {}).get("top_cell")
        )
        if not cell_name:
            return None, "Missing target cell_name"

        grid = float(layout.library.precision) / float(layout.library.unit)
        digest = context.state.get("candidate_digest") or GraphServices.candidate_digest(Path(layout.gds_path))
        return {
            "task": task,
            "layout": layout,
            "cell_name": cell_name,
            "grid": grid,
            "candidate_digest": digest,
        }, None

    def _stage_built_proposals(
        context,
        resolved: dict,
        task_id: str,
        kind: str,
        built: dict,
        bound_inputs: dict,
    ) -> ToolResult:
        """Filter no-op proposals, stage valid ones in state, and return ToolResult."""
        if built.get("status") != "fail" or not built.get("proposals"):
            return ToolResult(
                {
                    "status": built.get("status", "unknown"),
                    "measurement": built.get("measurement"),
                    "proposals": [],
                    "reason": built.get("reason", ""),
                },
                {},
            )

        store = copy.deepcopy(context.state.get("proposals") or {})
        staged = []
        for idx, prop in enumerate(built["proposals"], start=1):
            changes = prop.get("changes") or []
            if not DrcTools._changes_modify_geometry(changes, resolved["grid"]):
                continue
            pid = f"{task_id}-{kind}-p{len(store) + idx}"
            record = {
                "proposal_id": pid,
                "task_id": task_id,
                "kind": kind,
                "cell_name": resolved["cell_name"],
                "candidate_digest": resolved["candidate_digest"],
                "changes": changes,
                "affected_ids": [c["polygon_id"] for c in changes],
                "rule_assumptions": prop.get("rule_assumptions", ""),
                "local_check": prop.get("local_check"),
                "bound_inputs": bound_inputs,
            }
            store[pid] = record
            staged.append(record)

        if not staged:
            return ToolResult(
                {"status": "rejected", "reason": "No-op proposal: geometry unchanged", "proposals": []},
                {},
            )
        return ToolResult(
            {"status": "proposed", "measurement": built.get("measurement"), "proposals": staged},
            {"proposals": store},
        )

    def _apply_proposal(context, task_id: str, expected_kind: str, proposal_id: str | None) -> ToolResult:
        """Validate digest and no-op status, then atomically apply and checkpoint all changes."""
        if not proposal_id:
            return ToolResult({"status": "rejected", "reason": "proposal_id is required for apply"}, {})
        store = context.state.get("proposals") or {}
        prop = store.get(proposal_id)
        if not prop or prop.get("task_id") != task_id or prop.get("kind") != expected_kind:
            return ToolResult({"status": "rejected", "reason": f"Unknown or mismatched proposal_id: {proposal_id}"}, {})

        layout = context.services.layout
        layout.load_library()
        current_digest = context.state.get("candidate_digest") or GraphServices.candidate_digest(Path(layout.gds_path))
        if prop.get("candidate_digest") != current_digest:
            return ToolResult(
                {"status": "rejected", "reason": "Stale proposal: candidate_digest does not match current candidate"},
                {},
            )

        changes = prop.get("changes") or []
        grid = float(layout.library.precision) / float(layout.library.unit)
        cell_name = prop["cell_name"]
        live_changes = []
        for ch in changes:
            live_pts = layout.polygon_ids.resolve(cell_name, ch["polygon_id"]).points.tolist()
            live_changes.append({"before_points_user": live_pts, "after_points_user": ch["after_points_user"]})
        if not DrcTools._changes_modify_geometry(live_changes, grid):
            return ToolResult({"status": "rejected", "reason": "No-op proposal: live geometry already matches"}, {})

        pre_apply_path = context.state.get("candidate_path")
        backup_path = None
        if pre_apply_path and Path(pre_apply_path).is_file():
            backup_path = context.services.rundir / f".pre_tx_{task_id}.gds"
            shutil.copyfile(pre_apply_path, backup_path)

        batch = [{"polygon_id": ch["polygon_id"], "points": ch["after_points_user"]} for ch in changes]
        snapshot = layout.apply_polygon_changes(cell_name, batch)
        try:
            candidate = Path(context.services.checkpoint_candidate())
            new_digest = GraphServices.candidate_digest(candidate)
            if not new_digest:
                raise ValueError("Missing candidate digest")
        except Exception as exc:
            layout.rollback_transaction(snapshot)
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)
            return ToolResult({"status": "error", "reason": f"Candidate save failed: {exc}"}, {})

        tx_map = copy.deepcopy(context.state.get("task_transactions") or {})
        tx_map[task_id] = {
            "proposal_id": proposal_id,
            "kind": expected_kind,
            "cell_name": cell_name,
            "pre_apply_digest": current_digest,
            "pre_apply_path": pre_apply_path,
            "pre_apply_backup": str(backup_path) if backup_path else None,
            "changes": changes,
            "bound_inputs": prop.get("bound_inputs", {}),
        }
        return ToolResult(
            {
                "status": "applied",
                "proposal_id": proposal_id,
                "candidate_digest": new_digest,
                "changes": changes,
            },
            {
                "candidate_path": str(candidate),
                "candidate_digest": new_digest,
                "task_transactions": tx_map,
                "validation": None,
            },
        )

    def _check_transaction(context, task_id: str, expected_kind: str) -> ToolResult:
        """Run local check against live layout for staged task transaction; rollback on failure."""
        tx_map = copy.deepcopy(context.state.get("task_transactions") or {})
        tx = tx_map.get(task_id)
        if not tx or tx.get("kind") != expected_kind:
            return ToolResult({"status": "unknown", "deck_clean": False, "reason": "No staged transaction for task"}, {})

        resolved, err = DrcTools._resolve_task_context(context, task_id)
        if err:
            return ToolResult({"status": "unknown", "deck_clean": False, "reason": err}, {})

        layout = resolved["layout"]
        cell_name = tx["cell_name"]
        grid = resolved["grid"]
        bound = tx.get("bound_inputs", {})

        if expected_kind == "cover":
            inner_pts = layout.polygon_ids.resolve(cell_name, bound["inner_id"]).points.tolist()
            group_pts = [
                [layout.polygon_ids.resolve(cell_name, pid).points.tolist() for pid in grp]
                for grp in bound["groups"]
            ]
            res = CoverCheck().run(inner=inner_pts, groups=group_pts, minimum=bound["minimum_enclosure"], precision=grid)
        else:
            poly_pts = layout.polygon_ids.resolve(cell_name, bound["polygon_id"]).points.tolist()
            res = AreaCheck().run(polygon=poly_pts, minimum_area=bound["minimum_area"], precision=grid)

        checks_map = copy.deepcopy(context.state.get("task_checks") or {})
        current_digest = context.state.get("candidate_digest")
        backup_str = tx.get("pre_apply_backup")
        backup_path = Path(backup_str) if backup_str else None
        if res.get("status") == "pass":
            if backup_path is not None:
                backup_path.unlink(missing_ok=True)
            checks_map[task_id] = {
                "status": "pass",
                "deck_clean": False,
                "candidate_digest": current_digest,
                "measurement": res,
            }
            return ToolResult(
                {"status": "pass", "deck_clean": False, "candidate_digest": current_digest, "measurement": res},
                {"task_checks": checks_map},
            )

        revert_batch = [{"polygon_id": ch["polygon_id"], "points": ch["before_points_user"]} for ch in tx["changes"]]
        layout.apply_polygon_changes(cell_name, revert_batch)
        if backup_path is not None and backup_path.is_file():
            shutil.copyfile(backup_path, layout.candidate)
            backup_path.unlink(missing_ok=True)
            candidate = Path(layout.candidate)
            restored_digest = tx["pre_apply_digest"]
            layout.polygon_ids.save(context.services.rundir / "polygon_ids.json", restored_digest)
        else:
            candidate = Path(context.services.checkpoint_candidate())
            restored_digest = GraphServices.candidate_digest(candidate)
        tx_map.pop(task_id, None)
        checks_map[task_id] = {
            "status": res.get("status", "fail"),
            "rolled_back": True,
            "deck_clean": False,
            "candidate_digest": restored_digest,
            "measurement": res,
        }
        return ToolResult(
            {
                "status": res.get("status", "fail"),
                "rolled_back": True,
                "deck_clean": False,
                "candidate_digest": restored_digest,
                "measurement": res,
            },
            {
                "candidate_path": str(candidate),
                "candidate_digest": restored_digest,
                "task_transactions": tx_map,
                "task_checks": checks_map,
            },
        )

    def _changes_modify_geometry(changes: list[dict], grid: float) -> bool:
        """Return True when at least one change alters polygon coordinates beyond grid tolerance."""
        tol = grid * 0.1
        for ch in changes:
            before = ch.get("before_points_user") or []
            after = ch.get("after_points_user") or []
            if len(before) != len(after):
                return True
            for p_b, p_a in zip(before, after):
                if abs(float(p_b[0]) - float(p_a[0])) > tol or abs(float(p_b[1]) - float(p_a[1])) > tol:
                    return True
        return False


# === SECTION 3: ToolSpec Registrations ===
fix_cover = DrcTools.fix_cover
fix_cover.task_tool = ToolSpec(
    function=fix_cover,
    description="Propose a cover repair from model-selected polygon IDs and a model-derived uniform enclosure, or apply a staged proposal.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Use the current input task_id."},
            "action": {"type": "string", "enum": ["propose", "apply"]},
            "minimum_enclosure": {
                "type": "number", "minimum": 0,
                "description": "Model-derived uniform minimum enclosure in GDS user units for propose.",
            },
            "inner_id": {"type": "string", "description": "Editable inner polygon ID from layout.polygons[].id."},
            "groups": {
                "type": "array",
                "description": "Ordered groups of editable polygon IDs. Each group is a union that must cover the inner polygon.",
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "proposal_id": {"type": "string", "description": "For apply, use the ID returned by a suitable proposal from this tool."},
        },
        "required": ["task_id", "action"],
        "additionalProperties": False,
    },
)

fix_area = DrcTools.fix_area
fix_area.task_tool = ToolSpec(
    function=fix_area,
    description=(
        "Repair a minimum-area violation using the area inferred from the current rule text. "
        "Propose with polygon_id and minimum_area. Apply a suitable proposal with the returned proposal_id, "
        "then call check_area after successful apply. Use GDS user units."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Use the current input task_id."},
            "action": {"type": "string", "enum": ["propose", "apply"]},
            "polygon_id": {"type": "string", "description": "For propose, select the editable ID from layout.polygons[].id that matches the marker and target layer."},
            "minimum_area": {"type": "number", "description": "For propose, provide the minimum area inferred from the rule text in squared GDS user units."},
            "proposal_id": {"type": "string", "description": "For apply, use the ID returned by a suitable proposal from this tool."},
        },
        "required": ["task_id", "action"],
        "additionalProperties": False,
    },
)

check_cover = DrcTools.check_cover
check_cover.task_tool = ToolSpec(
    function=check_cover,
    description=(
        "After successful fix_cover apply, check the staged edit against its bound inputs. "
        "Evaluate the entire selected inner polygon, including portions within exception layers. "
        "A failed check rolls back the edit. The local result has deck_clean: false."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Use the current input task_id."},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

check_area = DrcTools.check_area
check_area.task_tool = ToolSpec(
    function=check_area,
    description=(
        "After successful fix_area apply, check the staged edit against minimum_area. "
        "A failed check rolls back the edit. The local result has deck_clean: false."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Use the current input task_id."},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

save_gds = DrcTools.save_gds
save_gds.task_tool = ToolSpec(
    function=save_gds,
    description="Save the final candidate GDS after queue completion. The workflow save node handles this after marker processing.",
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)
