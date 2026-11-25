"""Tests for the five DRC tools, proposal staging, digest checks, and atomic transactions."""

# === SECTION 1: Imports ===
import json
from pathlib import Path
from types import SimpleNamespace

import gdstk
import pytest

from FAGENT.capabilities.layout.layout_service import LayoutService
from FAGENT.engine.graph_services import GraphServices
from FAGENT.tasks.drc import tools as drc_tools


# === SECTION 2: Module-Level Fixture Helpers ===
def build_layout_env(tmp_path: Path, polygons: list[tuple[int, int, list[list[float]]]]) -> tuple[SimpleNamespace, dict]:
    """Create a real GDS file, rundir, LayoutService, and GraphServices-like context."""
    rundir = tmp_path / "rundir"
    rundir.mkdir(parents=True, exist_ok=True)
    src_gds = tmp_path / "source.gds"

    lib = gdstk.Library(unit=1e-6, precision=1e-9)
    cell = lib.new_cell("TOP")
    for layer, datatype, pts in polygons:
        cell.add(gdstk.Polygon(pts, layer=layer, datatype=datatype))
    lib.write_gds(str(src_gds))

    (rundir / "gds.json").write_text(json.dumps({"source": str(src_gds), "top_cell": "TOP"}))
    (rundir / "drc.json").write_text(json.dumps({"top_cell": "TOP", "rules": []}))

    layout = LayoutService(rundir=rundir, verbose=False).run()
    layout.load_library()

    services = GraphServices.__new__(GraphServices)
    services.rundir = rundir
    services.layout = layout
    services.config = {}
    services.drc_files = []
    services.layer_maps = []
    services.input_sources = {}
    services.verbose = False

    initial_digest = GraphServices.candidate_digest(src_gds)
    state = {
        "rundir": str(rundir),
        "candidate_path": str(src_gds),
        "candidate_digest": initial_digest,
        "report": {"top_cell": "TOP", "rules": []},
        "checklist": {
            "tasks": [
                {"task_id": "t-cover", "rule": "CT_R_11", "cell": "TOP", "status": "in_progress"},
                {"task_id": "t-area", "rule": "GT_A_13", "cell": "TOP", "status": "in_progress"},
                {"task_id": "t-unsupported", "rule": "EX_R_1", "cell": "TOP", "status": "in_progress"},
            ]
        },
        "drc_context": {
            "rule_evidence": {
                "CT_R_11": {"supported": True, "required_enclosure_user": 0.02},
                "GT_A_13": {"supported": True, "minimum_area_user": 0.038},
                "EX_R_1": {"supported": False, "reason": "Unsupported exception INST"},
            },
            "layer_mapping": {
                "CT": {"layer": 1, "datatype": 0},
                "M1": {"layer": 2, "datatype": 0},
                "AA": {"layer": 3, "datatype": 0},
                "GT": {"layer": 4, "datatype": 0},
            },
        },
        "proposals": {},
        "task_transactions": {},
        "task_checks": {},
        "task_queue": [],
        "current_task_id": "t-cover",
        "task_history": [],
    }
    return SimpleNamespace(services=services, state=state), state


def polygon_id_for_layer(layout: LayoutService, cell_name: str, layer: int) -> str:
    """Return the stable polygon_id of the first polygon on the given layer."""
    cell = layout.get_cell(cell_name)
    for poly in cell.polygons:
        if poly.layer == layer:
            return layout.polygon_ids.id_for(cell_name, poly)
    raise AssertionError(f"No polygon on layer {layer}")


def apply_state_updates(state: dict, result) -> None:
    """Merge ToolResult.updates into state dict."""
    if hasattr(result, "updates") and isinstance(result.updates, dict):
        state.update(result.updates)


# === SECTION 3: Five-Tool Interface & Transaction Tests ===
def test_five_tool_specs_exposed_and_obsolete_removed() -> None:
    """Only the five DRC tools are registered as task_tool exports."""
    for name in ("fix_cover", "fix_area", "check_cover", "check_area", "save_gds"):
        tool_fn = getattr(drc_tools, name)
        assert hasattr(tool_fn, "task_tool")
    for obsolete in ("replace_polygon", "query_layout_region", "read_checklist", "update_checklist"):
        assert not hasattr(drc_tools, obsolete)


def test_model_selected_ids_multi_polygon_apply_and_check_pass(tmp_path: Path) -> None:
    """fix_cover proposes and atomically applies multi-polygon edits, then check_cover passes."""
    ctx, state = build_layout_env(
        tmp_path,
        [
            (1, 0, [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]),
            (2, 0, [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]),
            (3, 0, [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]),
        ],
    )
    layout = ctx.services.layout
    ct_id = polygon_id_for_layer(layout, "TOP", 1)
    m1_id = polygon_id_for_layer(layout, "TOP", 2)
    aa_id = polygon_id_for_layer(layout, "TOP", 3)

    prop_res = drc_tools.fix_cover(ctx, task_id="t-cover", action="propose", inner_id=ct_id, groups=[[m1_id], [aa_id]])
    apply_state_updates(state, prop_res)
    proposals = prop_res.payload["proposals"]
    assert len(proposals) == 1
    assert len(proposals[0]["changes"]) == 2
    pid = proposals[0]["proposal_id"]

    app_res = drc_tools.fix_cover(ctx, task_id="t-cover", action="apply", proposal_id=pid)
    apply_state_updates(state, app_res)
    assert app_res.payload["status"] == "applied"

    chk_res = drc_tools.check_cover(ctx, task_id="t-cover")
    apply_state_updates(state, chk_res)
    assert chk_res.payload["status"] == "pass"
    assert chk_res.payload["deck_clean"] is False


def test_unknown_ids_missing_mapping_and_unsupported_exceptions(tmp_path: Path) -> None:
    """Unknown IDs, missing layer mapping, and unsupported rule exceptions return unknown."""
    ctx, state = build_layout_env(
        tmp_path,
        [(4, 0, [[0.0, 0.0], [0.40, 0.0], [0.40, 0.06], [0.0, 0.06]])],
    )
    gt_id = polygon_id_for_layer(ctx.services.layout, "TOP", 4)

    unk = drc_tools.fix_area(ctx, task_id="t-area", action="propose", polygon_id="MISSING:99", minimum_area=0.038)
    assert unk.payload["status"] == "unknown"

    unsup = drc_tools.fix_area(ctx, task_id="t-unsupported", action="propose", polygon_id=gt_id, minimum_area=0.038)
    assert unsup.payload["status"] == "unknown"

    state["drc_context"]["layer_mapping"] = {}
    no_map = drc_tools.fix_area(ctx, task_id="t-area", action="propose", polygon_id=gt_id, minimum_area=0.038)
    assert no_map.payload["status"] == "unknown"


def test_stale_digest_and_noop_rejected(tmp_path: Path) -> None:
    """Stale candidate digest and same-geometry no-op proposals are rejected without editing."""
    ctx, state = build_layout_env(
        tmp_path,
        [(4, 0, [[0.0, 0.0], [0.40, 0.0], [0.40, 0.06], [0.0, 0.06]])],
    )
    gt_id = polygon_id_for_layer(ctx.services.layout, "TOP", 4)

    prop_res = drc_tools.fix_area(ctx, task_id="t-area", action="propose", polygon_id=gt_id, minimum_area=0.038)
    apply_state_updates(state, prop_res)
    pid = prop_res.payload["proposals"][0]["proposal_id"]

    # Mutate digest to simulate another intervening edit
    state["candidate_digest"] = "stale-other-digest"
    stale_res = drc_tools.fix_area(ctx, task_id="t-area", action="apply", proposal_id=pid)
    assert stale_res.payload["status"] == "rejected"

    # Already-passing geometry yields no-op (no staged proposal)
    state["candidate_digest"] = prop_res.payload["proposals"][0]["candidate_digest"]
    noop_res = drc_tools.fix_area(ctx, task_id="t-area", action="propose", polygon_id=gt_id, minimum_area=0.01)
    assert noop_res.payload["status"] == "pass"
    assert noop_res.payload["proposals"] == []


def test_failed_check_rolls_back_all_edited_polygons_preserving_prior_edits(tmp_path: Path) -> None:
    """If check fails on a multi-polygon task, all its edits roll back while prior verified edits remain."""
    ctx, state = build_layout_env(
        tmp_path,
        [
            (1, 0, [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]),
            (2, 0, [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]),
            (3, 0, [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]),
            (4, 0, [[0.00, 0.00], [0.40, 0.00], [0.40, 0.06], [0.00, 0.06]]),
        ],
    )
    layout = ctx.services.layout
    gt_id = polygon_id_for_layer(layout, "TOP", 4)
    ct_id = polygon_id_for_layer(layout, "TOP", 1)
    m1_id = polygon_id_for_layer(layout, "TOP", 2)
    aa_id = polygon_id_for_layer(layout, "TOP", 3)

    # Task 1 (area) succeeds and is verified
    p_area = drc_tools.fix_area(ctx, task_id="t-area", action="propose", polygon_id=gt_id, minimum_area=0.038)
    apply_state_updates(state, p_area)
    a_area = drc_tools.fix_area(ctx, task_id="t-area", action="apply", proposal_id=p_area.payload["proposals"][0]["proposal_id"])
    apply_state_updates(state, a_area)
    c_area = drc_tools.check_area(ctx, task_id="t-area")
    apply_state_updates(state, c_area)
    assert c_area.payload["status"] == "pass"
    verified_digest = state["candidate_digest"]
    verified_gt_pts = layout.polygon_ids.resolve("TOP", gt_id).points.tolist()

    # Task 2 (cover) applies multi-polygon edit, then rule limit tightens so check_cover fails -> atomic rollback
    p_cov = drc_tools.fix_cover(ctx, task_id="t-cover", action="propose", inner_id=ct_id, groups=[[m1_id], [aa_id]])
    apply_state_updates(state, p_cov)
    a_cov = drc_tools.fix_cover(ctx, task_id="t-cover", action="apply", proposal_id=p_cov.payload["proposals"][0]["proposal_id"])
    apply_state_updates(state, a_cov)
    assert state["candidate_digest"] != verified_digest

    state["drc_context"]["rule_evidence"]["CT_R_11"]["required_enclosure_user"] = 0.20
    c_cov = drc_tools.check_cover(ctx, task_id="t-cover")
    apply_state_updates(state, c_cov)
    assert c_cov.payload["status"] == "fail"
    assert c_cov.payload["rolled_back"] is True
    assert state["candidate_digest"] == verified_digest
    assert layout.polygon_ids.resolve("TOP", gt_id).points.tolist() == verified_gt_pts
    assert layout.polygon_ids.resolve("TOP", m1_id).points.tolist() == [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]
    assert layout.polygon_ids.resolve("TOP", aa_id).points.tolist() == [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]


def test_candidate_save_failure_rolls_back_and_preserves_prior_edits(tmp_path: Path, monkeypatch) -> None:
    """When checkpoint_candidate fails during apply, all polygons roll back and prior digest remains."""
    ctx, state = build_layout_env(
        tmp_path,
        [
            (1, 0, [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]),
            (2, 0, [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]),
            (4, 0, [[0.00, 0.00], [0.40, 0.00], [0.40, 0.06], [0.00, 0.06]]),
        ],
    )
    layout = ctx.services.layout
    gt_id = polygon_id_for_layer(layout, "TOP", 4)
    ct_id = polygon_id_for_layer(layout, "TOP", 1)
    m1_id = polygon_id_for_layer(layout, "TOP", 2)

    p_area = drc_tools.fix_area(ctx, task_id="t-area", action="propose", polygon_id=gt_id, minimum_area=0.038)
    apply_state_updates(state, p_area)
    a_area = drc_tools.fix_area(ctx, task_id="t-area", action="apply", proposal_id=p_area.payload["proposals"][0]["proposal_id"])
    apply_state_updates(state, a_area)
    verified_digest = state["candidate_digest"]

    p_cov = drc_tools.fix_cover(ctx, task_id="t-cover", action="propose", inner_id=ct_id, groups=[[m1_id]])
    apply_state_updates(state, p_cov)

    def fail_checkpoint():
        raise OSError("disk full")

    monkeypatch.setattr(ctx.services, "checkpoint_candidate", fail_checkpoint)
    a_cov = drc_tools.fix_cover(ctx, task_id="t-cover", action="apply", proposal_id=p_cov.payload["proposals"][0]["proposal_id"])
    apply_state_updates(state, a_cov)
    assert a_cov.payload["status"] == "error"
    assert state["candidate_digest"] == verified_digest
    assert layout.polygon_ids.resolve("TOP", m1_id).points.tolist() == [[0.10, 0.10], [0.20, 0.10], [0.20, 0.20], [0.10, 0.20]]
