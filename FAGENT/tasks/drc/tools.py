"""Expose staged DRC repair and check tools with transactional candidate state."""

# === SECTION 1: Imports ===
import copy
import shutil
from pathlib import Path

from FAGENT.capabilities.layout.cover_check import AreaCheck, CoverCheck, WidthCheck
from FAGENT.capabilities.layout.repair_proposals import AreaProposalBuilder, CoverProposalBuilder, WidthProposalBuilder
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult, ToolSpec


# === SECTION 2: Five-Tool DRC Handler Class ===
class DrcTools:
    """Five-tool DRC repair interface with staged proposals and atomic transactions."""

    # --------------------------------------------------------------------------
    # Main Entry Point & Initialization
    # --------------------------------------------------------------------------
    def run(self):
        """Expose model-facing repair and check tools; saving remains an internal workflow step."""
        self.tools = [fix_cover, fix_area, fix_width, check_cover, check_area, check_width]
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
        cover_context: dict | None = None,
    ) -> ToolResult:
        """Propose or atomically apply an area repair using a model-selected polygon ID."""
        if action not in ("propose", "apply"):
            return ToolResult({"status": "rejected", "reason": f"Unsupported action: {action}"}, {})
        if action == "apply":
            return DrcTools._apply_proposal(context, task_id=task_id, expected_kind="area", proposal_id=proposal_id)

        resolved, err = DrcTools._resolve_task_context(context, task_id)
        if err:
            return ToolResult({"status": "unknown", "reason": err, "proposals": []}, {})

        if minimum_area is None and cover_context is None:
            return ToolResult({"status": "unknown", "reason": "minimum_area is required for a proposal", "proposals": []}, {})
        if not polygon_id:
            return ToolResult({"status": "unknown", "reason": "polygon_id is required", "proposals": []}, {})

        layout = resolved["layout"]
        cell_name = resolved["cell_name"]
        try:
            poly_pts = layout.polygon_ids.resolve(cell_name, polygon_id).points.tolist()
            protected = []
            if cover_context is not None:
                protected_ids = cover_context.get("protected_inner_ids")
                if not isinstance(protected_ids, list) or not protected_ids:
                    return ToolResult({"status": "unknown", "reason": "cover_context requires protected_inner_ids", "proposals": []}, {})
                protected = [layout.polygon_ids.resolve(cell_name, pid).points.tolist() for pid in protected_ids]
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult({"status": "unknown", "reason": f"Unknown polygon_id: {exc}", "proposals": []}, {})

        built = AreaProposalBuilder().run(
            polygon_points=poly_pts,
            minimum_area=float(minimum_area) if minimum_area is not None else None,
            grid=resolved["grid"],
            polygon_id=polygon_id,
            cover_context=cover_context,
            inner_polygons=protected,
        )
        return DrcTools._stage_built_proposals(
            context,
            resolved=resolved,
            task_id=task_id,
            kind="area",
            built=built,
            bound_inputs={"polygon_id": polygon_id, "minimum_area": float(minimum_area) if minimum_area is not None else None, "cover_context": cover_context},
        )

    def check_cover(context, task_id: str) -> ToolResult:
        """Verify staged cover transaction on live layout; rollback all task edits on failure."""
        return DrcTools._check_transaction(context, task_id=task_id, expected_kind="cover")

    def check_area(context, task_id: str) -> ToolResult:
        """Verify staged area transaction on live layout; rollback all task edits on failure."""
        return DrcTools._check_transaction(context, task_id=task_id, expected_kind="area")

    def fix_width(context, task_id: str, action: str, polygon_id: str | None = None,
                  minimum_width: float | None = None, proposal_id: str | None = None,
                  preserve_cover: bool = True, cover_context: dict | None = None,
                  resize_mode: str = 'expand_only') -> ToolResult:
        """Propose or apply a minimum-width repair, preserving supplied cover relations."""
        if action not in ('propose', 'apply'):
            return ToolResult({'status': 'rejected', 'reason': f'Unsupported action: {action}'}, {})
        if action == 'apply':
            return DrcTools._apply_proposal(context, task_id, 'width', proposal_id)
        resolved, err = DrcTools._resolve_task_context(context, task_id)
        if err:
            return ToolResult({'status': 'unknown', 'reason': err, 'proposals': []}, {})
        if not polygon_id or minimum_width is None:
            return ToolResult({'status': 'unknown', 'reason': 'polygon_id and minimum_width are required for propose', 'proposals': []}, {})
        if isinstance(minimum_width, bool) or not isinstance(minimum_width, (int, float)) or minimum_width <= 0:
            return ToolResult({'status': 'unknown', 'reason': 'minimum_width must be a positive number in GDS user units', 'proposals': []}, {})
        if not isinstance(preserve_cover, bool):
            return ToolResult({'status': 'unknown', 'reason': 'preserve_cover must be a boolean', 'proposals': []}, {})
        if cover_context is not None and not isinstance(cover_context, dict):
            return ToolResult({'status': 'unknown', 'reason': 'cover_context must be an object', 'proposals': []}, {})
        layout, cell_name = resolved['layout'], resolved['cell_name']
        try:
            points = layout.polygon_ids.resolve(cell_name, polygon_id).points.tolist()
            cover_polygons = {}
            if cover_context:
                ids = list(cover_context.get('protected_inner_ids') or [])
                ids.extend(pid for group in cover_context.get('groups', []) for pid in group)
                cover_polygons = {pid: layout.polygon_ids.resolve(cell_name, pid).points.tolist() for pid in dict.fromkeys(ids)}
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult({'status': 'unknown', 'reason': f'Unknown polygon_id in cover_context: {exc}', 'proposals': []}, {})
        built = WidthProposalBuilder().run(
            polygon_points=points, minimum_width=float(minimum_width), grid=resolved['grid'],
            polygon_id=polygon_id, preserve_cover=preserve_cover,
            cover_context=cover_context, cover_polygons=cover_polygons, resize_mode=resize_mode,
        )
        return DrcTools._stage_built_proposals(
            context, resolved=resolved, task_id=task_id, kind='width', built=built,
            bound_inputs={'polygon_id': polygon_id, 'minimum_width': float(minimum_width),
                          'preserve_cover': preserve_cover, 'cover_context': cover_context},
        )

    def check_width(context, task_id: str) -> ToolResult:
        """Verify width and optional cover preservation after fix_width apply."""
        return DrcTools._check_transaction(context, task_id=task_id, expected_kind='width')

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

    def _check_bound_cover(layout, cell_name: str, polygon_id: str, target_points: list,
                           cover_context: dict, grid: float) -> dict:
        """Check either target-as-inner groups or target-as-cover protected polygons."""
        minimum = cover_context.get('minimum_enclosure', 0)
        results = []
        try:
            if cover_context.get('groups'):
                groups = [[layout.polygon_ids.resolve(cell_name, pid).points.tolist() for pid in group]
                          for group in cover_context['groups']]
                results.append(CoverCheck().run(inner=target_points, groups=groups, minimum=minimum, precision=grid))
            for inner_id in cover_context.get('protected_inner_ids', []):
                inner = layout.polygon_ids.resolve(cell_name, inner_id).points.tolist()
                results.append(CoverCheck().run(inner=inner, outer=target_points, minimum=minimum, precision=grid))
        except (KeyError, ValueError, TypeError) as exc:
            return {'status': 'unknown', 'reason': f'Cannot resolve cover_context polygon ID: {exc}'}
        if not results:
            return {'status': 'unknown', 'reason': 'cover_context must include groups or protected_inner_ids'}
        status = 'unknown' if any(item.get('status') == 'unknown' for item in results) else (
            'pass' if all(item.get('status') == 'pass' for item in results) else 'fail'
        )
        return {'status': status, 'checks': results}

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
            if expected_kind == 'width':
                res = WidthCheck().run(poly_pts, bound['minimum_width'], precision=grid)
            else:
                res = AreaCheck().run(polygon=poly_pts, minimum_area=bound["minimum_area"], precision=grid) if bound.get("minimum_area") is not None else {"status": "pass"}
            cover_context = bound.get("cover_context")
            if res.get("status") == "pass" and bound.get('preserve_cover', True) and cover_context:
                cover_results = DrcTools._check_bound_cover(layout, cell_name, bound['polygon_id'], poly_pts, cover_context, grid)
                res = {"status": "pass" if cover_results.get('status') == 'pass' else cover_results.get('status', 'fail'),
                       "width_or_area": res, "cover": cover_results, "reason": "Cover context recheck"}

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
    description=(
        "Copy IDs from layout.polygons[].id. For propose, provide task_id, inner_id, groups and minimum_enclosure in GDS user units. "
        "For apply, provide task_id and a returned proposal_id. Local checks are not full-deck DRC."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Current checklist task ID. Required for both 'propose' and 'apply'.",
            },
            "action": {
                "type": "string",
                "enum": ["propose", "apply"],
                "description": "Use 'propose' to generate fix options; use 'apply' to apply one returned proposal_id.",
            },
            "minimum_enclosure": {
                "type": "number",
                "minimum": 0,
                "description": "Required for 'propose'. Minimum enclosure in GDS user units; use 0 if only coverage is required.",
            },
            "inner_id": {
                "type": "string",
                "description": "Required for 'propose'. Target polygon ID to be covered, copied from layout.polygons[].id.",
            },
            "groups": {
                "type": "array",
                "description": (
                    "Required for 'propose'. List of covering polygon ID groups. "
                    "IDs inside one group mean OR; multiple groups mean AND (inner_id must be covered by every group)."
                ),
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "proposal_id": {
                "type": "string",
                "description": "Required for 'apply'. Exact proposal_id returned by a prior 'propose' call.",
            },
        },
        "required": ["task_id", "action"],
        "additionalProperties": False,
    },
)

fix_area = DrcTools.fix_area
fix_area.task_tool = ToolSpec(
    function=fix_area,
    description=(
        "Copy polygon_id values from layout.polygons[].id. For propose, provide task_id, polygon_id and minimum_area in squared GDS user units, "
        "or cover_context to contract an oversized cover while preserving its protected inner polygons. "
        "For apply, provide task_id and a returned proposal_id. Local checks are not full-deck DRC."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Current checklist task ID. Required for both 'propose' and 'apply'.",
            },
            "action": {
                "type": "string",
                "enum": ["propose", "apply"],
                "description": "Use 'propose' to generate fix options; use 'apply' to apply one returned proposal_id.",
            },
            "polygon_id": {
                "type": "string",
                "description": "Required for 'propose'. Target polygon ID to resize, copied from layout.polygons[].id.",
            },
            "minimum_area": {
                "type": "number",
                "description": "Required for minimum-area repairs; optional with cover_context to enforce an additional area floor while shrinking.",
            },
            "cover_context": {
                "type": "object",
                "description": "For safely shrinking a covering polygon. protected_inner_ids lists polygons that must remain enclosed; minimum_enclosure defaults to 0.",
                "properties": {
                    "protected_inner_ids": {"type": "array", "items": {"type": "string"}},
                    "minimum_enclosure": {"type": "number", "minimum": 0, "default": 0},
                },
                "required": ["protected_inner_ids"],
                "additionalProperties": False,
            },
            "proposal_id": {
                "type": "string",
                "description": "Required for 'apply'. Exact proposal_id returned by a prior 'propose' call.",
            },
        },
        "required": ["task_id", "action"],
        "additionalProperties": False,
    },
)

fix_width = DrcTools.fix_width
fix_width.task_tool = ToolSpec(
    function=fix_width,
    description=(
        "Copy polygon IDs from layout.polygons[].id. For propose, provide task_id, polygon_id and minimum_width in GDS user units; "
        "optional cover_context preserves coverage/enclosure. For apply, provide task_id and a returned proposal_id."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Current checklist task ID."},
            "action": {"type": "string", "enum": ["propose", "apply"]},
            "polygon_id": {"type": "string", "description": "Polygon ID copied verbatim from layout.polygons[].id; required for propose."},
            "minimum_width": {"type": "number", "exclusiveMinimum": 0, "description": "Required minimum width in GDS user units; required for propose."},
            "proposal_id": {"type": "string", "description": "Required for apply; use an ID returned by propose."},
            "preserve_cover": {"type": "boolean", "default": True},
            "cover_context": {
                "type": "object",
                "description": "Optional cover relations: groups for this polygon as inner, or protected_inner_ids for this polygon as cover.",
                "properties": {
                    "groups": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                    "protected_inner_ids": {"type": "array", "items": {"type": "string"}},
                    "minimum_enclosure": {"type": "number", "minimum": 0, "default": 0},
                },
                "additionalProperties": False,
            },
            "resize_mode": {"type": "string", "enum": ["expand_only", "center", "keep_edges"], "default": "expand_only"},
        },
        "required": ["task_id", "action"],
        "additionalProperties": False,
    },
)

check_cover = DrcTools.check_cover
check_cover.task_tool = ToolSpec(
    function=check_cover,
    description=(
        "Verify coverage/enclosure after fix_cover('apply'). "
        "Returns status, measurement, candidate_digest, deck_clean=false, and rolled_back=true if check fails."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID that was just applied with fix_cover.",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

check_area = DrcTools.check_area
check_area.task_tool = ToolSpec(
    function=check_area,
    description=(
        "Verify minimum polygon area after fix_area('apply'). "
        "Returns status, measurement, candidate_digest, deck_clean=false, and rolled_back=true if check fails."
    ),
    parameters={
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Task ID that was just applied with fix_area.",
            },
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

check_width = DrcTools.check_width
check_width.task_tool = ToolSpec(
    function=check_width,
    description="Verify minimum width and supplied cover conditions after fix_width apply; failed checks roll back the transaction.",
    parameters={
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "Task ID just applied with fix_width."}},
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

save_gds = DrcTools.save_gds
save_gds.task_tool = ToolSpec(
    function=save_gds,
    description=(
        "Internal workflow step only; do not call directly. "
        "Saves the candidate GDS after the task queue finishes and returns status, output_path, digest, and task_summary."
    ),
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)
