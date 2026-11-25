"""Tests for five-tool DRC geometry checks and deterministic proposal builders."""

# === SECTION 1: Imports ===
import pytest

from FAGENT.capabilities.layout.cover_check import AreaCheck, CoverCheck
from FAGENT.capabilities.layout.repair_proposals import AreaProposalBuilder, CoverProposalBuilder


# === SECTION 2: Module-Level Geometry Helpers ===
def rect_points(x0: float, y0: float, x1: float, y1: float) -> list[list[float]]:
    """Return closed-loop-free 4-vertex axis-aligned rectangle coordinates."""
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def is_on_grid(points: list[list[float]], grid: float) -> bool:
    """Return True when every vertex coordinate is an integer multiple of grid."""
    for x, y in points:
        if abs(round(x / grid) * grid - x) > 1e-9 or abs(round(y / grid) * grid - y) > 1e-9:
            return False
    return True


# === SECTION 3: CoverCheck Group, Hole, Concave, and Rule Limit Tests ===
def test_cover_check_group_union_covers_target() -> None:
    """Two polygons in one group jointly cover inner even when neither covers alone."""
    inner = rect_points(0.10, 0.10, 0.20, 0.20)
    left_half = rect_points(0.05, 0.05, 0.15, 0.25)
    right_half = rect_points(0.15, 0.05, 0.25, 0.25)

    single_left = CoverCheck().run(inner=inner, outer=left_half, minimum=0.05, precision=0.001)
    assert single_left["status"] == "fail"

    grouped = CoverCheck().run(inner=inner, groups=[[left_half, right_half]], minimum=0.05, precision=0.001)
    assert grouped["status"] == "pass"
    assert grouped["measured_distance"] == pytest.approx(0.05, abs=1e-6)


def test_cover_check_requires_every_group_to_cover() -> None:
    """All groups must independently cover inner; one failing group fails the check."""
    inner = rect_points(0.10, 0.10, 0.20, 0.20)
    m1_a = rect_points(0.05, 0.05, 0.15, 0.25)
    m1_b = rect_points(0.15, 0.05, 0.25, 0.25)
    aa_short = rect_points(0.05, 0.05, 0.18, 0.25)

    result = CoverCheck().run(
        inner=inner,
        groups=[[m1_a, m1_b], [aa_short]],
        minimum=0.05,
        precision=0.001,
    )
    assert result["status"] == "fail"


def test_cover_check_respects_holes_in_outer_union() -> None:
    """A ring formed by four polygons with a central hole over inner must fail."""
    inner = rect_points(0.20, 0.20, 0.30, 0.30)
    bottom = rect_points(0.00, 0.00, 0.50, 0.22)
    top = rect_points(0.00, 0.28, 0.50, 0.50)
    left = rect_points(0.00, 0.22, 0.22, 0.28)
    right = rect_points(0.28, 0.22, 0.50, 0.28)

    result = CoverCheck().run(
        inner=inner,
        groups=[[bottom, top, left, right]],
        minimum=0.0,
        precision=0.001,
    )
    assert result["status"] == "fail"


def test_cover_check_respects_concave_outer_polygon() -> None:
    """Concave L-shape whose bounding box encloses inner must still fail if cutout exposes inner."""
    inner = rect_points(0.20, 0.20, 0.30, 0.30)
    l_shape = [
        [0.00, 0.00],
        [0.40, 0.00],
        [0.40, 0.15],
        [0.15, 0.15],
        [0.15, 0.40],
        [0.00, 0.40],
    ]
    result = CoverCheck().run(inner=inner, groups=[[l_shape]], minimum=0.0, precision=0.001)
    assert result["status"] == "fail"


def test_cover_check_uses_rule_minimum_enclosure() -> None:
    """Minimum enclosure threshold from rule evidence controls pass vs fail."""
    inner = rect_points(0.10, 0.10, 0.20, 0.20)
    outer = rect_points(0.08, 0.08, 0.22, 0.22)

    zero_margin = CoverCheck().run(inner=inner, groups=[[outer]], minimum=0.0, precision=0.001)
    strict_margin = CoverCheck().run(inner=inner, groups=[[outer]], minimum=0.05, precision=0.001)

    assert zero_margin["status"] == "pass"
    assert strict_margin["status"] == "fail"


# === SECTION 4: AreaCheck and Proposal Builder Tests ===
def test_area_check_and_grid_aligned_proposal_for_gt_a_13() -> None:
    """0.4 x 0.06 polygon (0.024) fails 0.038; generated grid-aligned candidate passes."""
    poly = rect_points(0.0, 0.0, 0.40, 0.06)
    grid = 0.001

    initial = AreaCheck().run(polygon=poly, minimum_area=0.038, precision=grid)
    assert initial["status"] == "fail"
    assert initial["measured_area"] == pytest.approx(0.024, abs=1e-9)

    built = AreaProposalBuilder().run(
        polygon_points=poly,
        minimum_area=0.038,
        grid=grid,
        polygon_id="GT:1",
    )
    assert built["status"] == "fail"
    assert len(built["proposals"]) == 1
    candidate_pts = built["proposals"][0]["changes"][0]["after_points_user"]
    assert is_on_grid(candidate_pts, grid)

    recheck = AreaCheck().run(polygon=candidate_pts, minimum_area=0.038, precision=grid)
    assert recheck["status"] == "pass"
    assert recheck["measured_area"] >= 0.038


def test_cover_proposal_builder_expands_rectangular_group_on_grid() -> None:
    """CoverProposalBuilder generates a grid-snapped rectangular expansion that passes CoverCheck."""
    inner = rect_points(0.10, 0.10, 0.20, 0.20)
    outer = rect_points(0.09, 0.09, 0.21, 0.21)
    grid = 0.005

    built = CoverProposalBuilder().run(
        inner_points=inner,
        groups=[[outer]],
        minimum_enclosure=0.03,
        grid=grid,
        inner_id="CT:1",
        group_ids=[["M1:1"]],
    )
    assert built["status"] == "fail"
    assert len(built["proposals"]) == 1
    after_pts = built["proposals"][0]["changes"][0]["after_points_user"]
    assert is_on_grid(after_pts, grid)

    recheck = CoverCheck().run(inner=inner, groups=[[after_pts]], minimum=0.03, precision=grid)
    assert recheck["status"] == "pass"


def test_unsupported_geometry_returns_unknown() -> None:
    """Degenerate or non-rectangular proposal inputs return status unknown without guessing."""
    bad_cover = CoverCheck().run(inner=[[0.0, 0.0], [0.1, 0.0]], groups=[[]], minimum=0.01, precision=0.001)
    assert bad_cover["status"] == "unknown"

    concave = [
        [0.00, 0.00],
        [0.40, 0.00],
        [0.40, 0.04],
        [0.10, 0.04],
        [0.10, 0.10],
        [0.00, 0.10],
    ]
    bad_area_prop = AreaProposalBuilder().run(
        polygon_points=concave,
        minimum_area=0.038,
        grid=0.001,
        polygon_id="GT:2",
    )
    assert bad_area_prop["status"] == "unknown"
    assert bad_area_prop["proposals"] == []
