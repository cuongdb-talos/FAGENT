"""Expose the five DRC repair, check, and save tools with transactional candidate state."""

# === SECTION 1: Imports ===
import copy
import shutil
from pathlib import Path

from FAGENT.capabilities.layout.cover_check import AreaCheck, CoverCheck
from FAGENT.capabilities.layout.repair_proposals import AreaProposalBuilder, CoverProposalBuilder
from FAGENT.engine.graph_services import GraphServices
from FAGENT.engine.task_contracts import ToolResult, ToolSpec
from FAGENT.tasks.drc.rule_evidence import RuleEvidence


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
    ) -> ToolResult:
        """Propose or atomically apply a cover repair using model-selected polygon IDs."""
        if action not in ("propose", "apply"):
            return ToolResult({"status": "rejected", "reason": f"Unsupported action: {action}"}, {})
        if action == "apply":
            return DrcTools._apply_proposal(context, task_id=task_id, expected_kind="cover", proposal_id=proposal_id)

        resolved, err = DrcTools._resolve_task_context(context, task_id)
        if err:
            return ToolResult({"status": "unknown", "reason": err, "proposals": []}, {})

        rule_ev = resolved["rule_evidence"]
        min_enc = rule_ev.get("required_enclosure_user")
        if min_enc is None and rule_ev.get("required_enclosure_dbu") is not None:
            min_enc = float(rule_ev["required_enclosure_dbu"]) * resolved["grid"]
        if min_enc is None:
            min_enc = 0.0

        if not inner_id or not isinstance(groups, list) or not groups or any(not g for g in groups):
            return ToolResult({"status": "unknown", "reason": "inner_id and non-empty groups are required", "proposals": []}, {})

        layout = resolved["layout"]
        cell_name = resolved["cell_name"]
        try:
            inner_pts = layout.polygon_ids.resolve(cell_name, inner_id).points.tolist()
            group_pts = [
                [layout.polygon_ids.resolve(cell_name, pid).points.tolist() for pid in grp]
                for grp in groups
            ]
        except (KeyError, ValueError, TypeError) as exc:
            return ToolResult({"status": "unknown", "reason": f"Unknown polygon_id: {exc}", "proposals": []}, {})

        built = CoverProposalBuilder().run(
            inner_points=inner_pts,
            groups=group_pts,
            minimum_enclosure=float(min_enc),
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
            bound_inputs={"inner_id": inner_id, "groups": groups, "minimum_enclosure": float(min_enc)},
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

        rule_ev = resolved["rule_evidence"]
        min_area = minimum_area if minimum_area is not None else (
            rule_ev.get("minimum_area_user") if rule_ev.get("minimum_area_user") is not None else rule_ev.get("required_area_user")
        )
        if min_area is None:
            return ToolResult({"status": "unknown", "reason": "Missing minimum_area in arguments or rule evidence", "proposals": []}, {})
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
            minimum_area=float(min_area),
            grid=resolved["grid"],
            polygon_id=polygon_id,
        )
        return DrcTools._stage_built_proposals(
            context,
            resolved=resolved,
            task_id=task_id,
            kind="area",
            built=built,
            bound_inputs={"polygon_id": polygon_id, "minimum_area": float(min_area)},
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
        """Resolve task, rule evidence, physical layer mapping, cell, and GDS grid."""
        checklist = context.state.get("checklist") or {}
        task = next((t for t in checklist.get("tasks", []) if t.get("task_id") == task_id), None)
        if task is None:
            return None, f"Unknown task_id: {task_id}"

        drc_ctx = context.state.get("drc_context") or {}
        rule_name = task.get("rule")
        layer_map = drc_ctx.get("layer_mapping") or {}
        rule_ev_map = drc_ctx.get("rule_evidence") or {}
        rule_ev = rule_ev_map.get(rule_name)

        layout = context.services.layout
        layout.load_library()

        if rule_ev is None and not rule_ev_map:
            cfg = getattr(context.services, "config", None) or {}
            input_sources = getattr(context.services, "input_sources", None) or {}
            gds_data = getattr(layout, "gds_data", None) or {}
            deck_paths = (
                getattr(context.services, "drc_files", None)
                or input_sources.get("decks")
                or cfg.get("drc_files", [])
            )
            layer_maps = (
                getattr(context.services, "layer_maps", None)
                or input_sources.get("layer_maps")
                or gds_data.get("layer_maps")
                or cfg.get("layer_maps", [])
                or cfg.get("layer_map_files", [])
            )
            if deck_paths:
                rule_ev = RuleEvidence().run(task=task, deck_paths=deck_paths, layer_maps=layer_maps)
                if not layer_map:
                    layer_map = rule_ev.get("layer_mapping") or {}

        if not layer_map:
            return None, "Missing physical layer_mapping"
        if not rule_ev or rule_ev.get("supported") is False:
            reason = (rule_ev or {}).get("reason") or f"Unsupported or missing rule evidence for {rule_name}"
            return None, reason
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
            "rule_evidence": rule_ev,
            "layer_mapping": layer_map,
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
        rule_ev = resolved["rule_evidence"]

        if expected_kind == "cover":
            min_enc = rule_ev.get("required_enclosure_user")
            if min_enc is None:
                min_enc = bound.get("minimum_enclosure", 0.0)
            inner_pts = layout.polygon_ids.resolve(cell_name, bound["inner_id"]).points.tolist()
            group_pts = [
                [layout.polygon_ids.resolve(cell_name, pid).points.tolist() for pid in grp]
                for grp in bound["groups"]
            ]
            res = CoverCheck().run(inner=inner_pts, groups=group_pts, minimum=float(min_enc), precision=grid)
        else:
            min_area = rule_ev.get("minimum_area_user")
            if min_area is None:
                min_area = bound.get("minimum_area", 0.0)
            poly_pts = layout.polygon_ids.resolve(cell_name, bound["polygon_id"]).points.tolist()
            res = AreaCheck().run(polygon=poly_pts, minimum_area=float(min_area), precision=grid)

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
    description="Propose or apply a cover repair for a task using model-selected inner_id and polygon groups.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "action": {"type": "string", "enum": ["propose", "apply"]},
            "inner_id": {"type": "string"},
            "groups": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "proposal_id": {"type": "string"},
        },
        "required": ["task_id", "action"],
        "additionalProperties": False,
    },
)

fix_area = DrcTools.fix_area
fix_area.task_tool = ToolSpec(
    function=fix_area,
    description="Propose or apply an area repair for a task using a model-selected polygon_id and minimum_area.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
            "action": {"type": "string", "enum": ["propose", "apply"]},
            "polygon_id": {"type": "string"},
            "minimum_area": {"type": "number"},
            "proposal_id": {"type": "string"},
        },
        "required": ["task_id", "action"],
        "additionalProperties": False,
    },
)

check_cover = DrcTools.check_cover
check_cover.task_tool = ToolSpec(
    function=check_cover,
    description="Check staged cover edits for a task against bound inputs; rolls back task edits on failure.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

check_area = DrcTools.check_area
check_area.task_tool = ToolSpec(
    function=check_area,
    description="Check staged area edit for a task against minimum_area; rolls back task edit on failure.",
    parameters={
        "type": "object",
        "properties": {
            "task_id": {"type": "string"},
        },
        "required": ["task_id"],
        "additionalProperties": False,
    },
)

save_gds = DrcTools.save_gds
save_gds.task_tool = ToolSpec(
    function=save_gds,
    description="Save the final verified candidate GDS after all queued tasks are complete.",
    parameters={
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
)
