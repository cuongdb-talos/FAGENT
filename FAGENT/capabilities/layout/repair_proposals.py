# === SECTION 1: Imports ===
import copy
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from shapely.geometry import box as shapely_box, Polygon as ShapelyPolygon

from .cover_check import AreaCheck, CoverCheck


# === SECTION 2: Repair Proposals Engine Class ===
class RepairProposals:
    """Evaluates geometric enclosure measurements and produces deterministic repair proposals."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, task: dict, rule_evidence: dict, geometry: dict, layout, candidate_digest: str = None) -> dict:
        """Measure geometry and generate candidate repair proposals."""
        self.task = copy.deepcopy(task) if isinstance(task, dict) else {}
        self.rule_evidence = copy.deepcopy(rule_evidence) if isinstance(rule_evidence, dict) else {}
        self.geometry = copy.deepcopy(geometry) if isinstance(geometry, dict) else {}
        self.layout = layout
        self.candidate_digest = candidate_digest

        if not self.rule_evidence or not self.rule_evidence.get('supported'):
            reason = self.rule_evidence.get('reason') if self.rule_evidence else 'Missing rule evidence'
            return self._unknown_result(reason or 'Unsupported rule semantics')

        if not self.geometry or self.geometry.get('status') != 'matched':
            reason = self.geometry.get('reason') if self.geometry else 'Unmatched or ambiguous geometry'
            return self._unknown_result(reason or 'Geometry is not matched')

        rule_name = str(self.task.get('rule', '')).strip()
        rule_text = str(self.rule_evidence.get('rule_text', ''))

        # CT_R_11 requires CT covered by M1 AND (GT OR AA) and evaluation of INST/EXCLU
        if rule_name == 'CT_R_11' or '(GT OR AA)' in rule_text or 'INST' in rule_text:
            return self._unknown_result(
                'CT_R_11 requires full coverage by M1 and (GT OR AA), except INST/EXCLU; M1 coverage alone cannot pass full rule'
            )

        matched_polys = self.geometry.get('matched_polygons', [])
        ct_poly = next((p for p in matched_polys if p.get('layer_name') == 'CT'), None)
        m1_poly = next((p for p in matched_polys if p.get('layer_name') == 'M1'), None)

        if not ct_poly or not m1_poly:
            return self._unknown_result('Supported enclosure requires both CT and M1 matched polygons')

        scale = float(self.geometry.get('dbu_per_user_unit', 1000.0))
        required = self._resolve_required_enclosure_dbu(scale)
        if required is None:
            return self._unknown_result('Missing required enclosure threshold in rule evidence')

        ct_pts = ct_poly['points_dbu']
        m1_pts = m1_poly['points_dbu']

        # Measure real polygon coverage and enclosure via CoverCheck in DBU
        cover = CoverCheck().run(ct_pts, m1_pts, minimum=float(required), precision=1e-3)
        if cover.get('status') == 'unknown':
            return self._unknown_result(cover.get('reason') or 'Unsupported polygon geometry for enclosure measurement')

        if cover['status'] == 'pass':
            measured = self._to_dbu_number(cover['measured_distance'])
            self.result = {
                'measurement': {
                    'status': 'pass',
                    'measured_enclosure_dbu': measured,
                    'required_enclosure_dbu': required,
                    'deficit_dbu': 0,
                    'contributing_edges': [],
                    'units': 'dbu',
                    'candidate_digest': self.candidate_digest,
                },
                'proposals': [],
            }
            return self.result

        violations = cover.get('violations') or []
        min_enclosure = self._to_dbu_number(min(v['measured_distance'] for v in violations)) if violations else 0
        deficit = self._to_dbu_number(max(v['shortage'] for v in violations)) if violations else required

        # Only propose rectangular edge expansion when both polygons are axis-aligned rectangles
        proposals = []
        contributing_edges = []
        blocked_reason = ''
        verification = {}
        if self._is_axis_aligned_rect(ct_pts) and self._is_axis_aligned_rect(m1_pts):
            min_ix, max_ix, min_iy, max_iy = self._bbox(ct_pts)
            min_ox, max_ox, min_oy, max_oy = self._bbox(m1_pts)
            left = min_ix - min_ox
            right = max_ox - max_ix
            bottom = min_iy - min_oy
            top = max_oy - max_iy

            if left < required:
                contributing_edges.append('left')
            if right < required:
                contributing_edges.append('right')
            if bottom < required:
                contributing_edges.append('bottom')
            if top < required:
                contributing_edges.append('top')

            new_min_ox = min_ox - (required - left) if 'left' in contributing_edges else min_ox
            new_max_ox = max_ox + (required - right) if 'right' in contributing_edges else max_ox
            new_min_oy = min_oy - (required - bottom) if 'bottom' in contributing_edges else min_oy
            new_max_oy = max_oy + (required - top) if 'top' in contributing_edges else max_oy

            after_pts_dbu = [
                [new_min_ox, new_min_oy],
                [new_max_ox, new_min_oy],
                [new_max_ox, new_max_oy],
                [new_min_ox, new_max_oy],
            ]
            verification = self._verify_proposal_safety(ct_poly, m1_poly, after_pts_dbu, scale, required)
            if not verification['verified']:
                blocked_reason = verification['reason']
            else:
                # Re-measure proposed geometry with CoverCheck before emitting proposal
                post_cover = CoverCheck().run(ct_pts, after_pts_dbu, minimum=float(required), precision=1e-3)
                if post_cover.get('status') == 'pass':
                    after_pts_user = [[round(x / scale, 6), round(y / scale, 6)] for x, y in after_pts_dbu]
                    primary_edge = contributing_edges[0] if len(contributing_edges) == 1 else 'multiple'
                    task_id_str = self.task.get('task_id', 'task-1')
                    poly_id_str = m1_poly['polygon_id'].replace(':', '_')
                    repair_id = f"repair-{task_id_str}-{poly_id_str}-{primary_edge}"
                    post_measured = self._to_dbu_number(post_cover['measured_distance'])
                    proposals = [{
                        'repair_id': repair_id,
                        'target_polygon_id': m1_poly['polygon_id'],
                        'cell': self.geometry.get('cell'),
                        'edge': primary_edge,
                        'delta_dbu': deficit,
                        'before_points_dbu': m1_pts,
                        'after_points_dbu': after_pts_dbu,
                        'after_points_user': after_pts_user,
                        'rule_assumptions': 'Assumes axis-aligned rectangular contact enclosure without adjacent competing spacing constraints or exclusion regions',
                        'candidate_digest': self.candidate_digest,
                        'verification': verification,
                        'post_measurement': {
                            'status': 'pass',
                            'measured_enclosure_dbu': post_measured,
                            'deficit_dbu': 0,
                            'units': 'dbu',
                        },
                    }]
                else:
                    blocked_reason = 'Post-repair geometry failed local enclosure recheck'
        else:
            blocked_reason = 'Non-rectangular polygon geometry is not supported for automatic expansion'

        self.result = {
            'measurement': {
                'status': 'fail',
                'measured_enclosure_dbu': min_enclosure,
                'required_enclosure_dbu': required,
                'deficit_dbu': deficit,
                'contributing_edges': contributing_edges,
                'units': 'dbu',
                'candidate_digest': self.candidate_digest,
            },
            'proposals': proposals,
            'reason': blocked_reason,
            'verification': verification,
        }
        return self.result

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self):
        """Initialize repair proposals attributes."""
        self.task = None
        self.rule_evidence = None
        self.geometry = None
        self.layout = None
        self.candidate_digest = None
        self.result = None

    # --------------------------------------------------------------------------
    # Helper Methods
    # --------------------------------------------------------------------------
    def _verify_proposal_safety(self, ct_poly: dict, m1_poly: dict, after_pts_dbu: list, scale: float, required_dbu: int) -> dict:
        """Verify candidate digest, connectivity, neighbor clearance, and shared-cell hierarchy."""
        geom_digest = self.geometry.get('candidate_digest')
        if self.candidate_digest is not None and geom_digest is not None and geom_digest != self.candidate_digest:
            return {'verified': False, 'reason': f'Stale geometry candidate_digest ({geom_digest} != {self.candidate_digest})'}

        cell_name = self.geometry.get('cell') or getattr(self.layout, 'top_cell', None)
        bound = {
            'candidate_digest': self.candidate_digest,
            'cell': cell_name,
            'polygon_ids': [ct_poly.get('polygon_id'), m1_poly.get('polygon_id')],
        }

        if self.layout is None or getattr(self.layout, 'library', None) is None or not cell_name:
            if (self.geometry.get('connectivity_verified') is True
                    and self.geometry.get('neighbour_constraints_verified') is True
                    and self.geometry.get('shared_cell_impact_verified') is True):
                return {**bound, 'verified': True, 'connectivity_verified': True, 'neighbour_constraints_verified': True, 'shared_cell_impact_verified': True, 'reason': ''}
            return {**bound, 'verified': False, 'connectivity_verified': False, 'neighbour_constraints_verified': False, 'shared_cell_impact_verified': False, 'reason': 'Missing layout hierarchy information; cannot verify connectivity, neighbor constraints, or shared-cell impact'}

        cell = self.layout.get_cell(cell_name)
        cells = getattr(self.layout.library, 'cells', None)
        if cell is None or cells is None:
            return {**bound, 'verified': False, 'reason': 'Missing cell or library hierarchy information'}

        # 1. Shared-cell impact verification
        if getattr(cell, 'paths', None) and len(cell.paths) > 0:
            return {**bound, 'verified': False, 'shared_cell_impact_verified': False, 'reason': 'Cell contains unsupported path elements'}
        for other in cells:
            if getattr(other, 'name', None) == cell_name:
                continue
            for ref in getattr(other, 'references', None) or []:
                ref_cell = getattr(ref, 'cell', None)
                ref_name = getattr(ref_cell, 'name', None) or getattr(ref, 'cell_name', None)
                if ref_name == cell_name:
                    return {**bound, 'verified': False, 'shared_cell_impact_verified': False, 'reason': f'Shared cell {cell_name} is instanced by {getattr(other, "name", "parent")}; editing blocked'}

        after_user = [[x / scale, y / scale] for x, y in after_pts_dbu]
        after_poly = ShapelyPolygon(after_user)
        for ref in getattr(cell, 'references', None) or []:
            try:
                bbox = ref.bounding_box()
            except Exception:
                return {**bound, 'verified': False, 'shared_cell_impact_verified': False, 'reason': 'Unbounded child reference in target cell'}
            if bbox is not None:
                ref_box = shapely_box(bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1])
                if ref_box.intersects(after_poly) or ref_box.distance(after_poly) <= 1e-6:
                    return {**bound, 'verified': False, 'shared_cell_impact_verified': False, 'reason': 'Child cell reference overlaps proposed expansion region'}

        # 2. Connectivity & 3. Neighbor clearance verification
        layer_map = self.rule_evidence.get('layer_mapping', {})
        m1_spec = layer_map.get('M1')
        ct_spec = layer_map.get('CT')
        if not m1_spec or not ct_spec:
            return {**bound, 'verified': False, 'connectivity_verified': False, 'neighbour_constraints_verified': False, 'reason': 'Missing CT/M1 physical layer mapping for connectivity and neighbor checks'}

        pids = getattr(self.layout, 'polygon_ids', None)
        m1_pid = m1_poly.get('polygon_id')
        ct_pid = ct_poly.get('polygon_id')
        clearance_user = float(required_dbu) / scale

        for p in getattr(cell, 'polygons', []):
            if len(p.points) < 3:
                continue
            pid = pids.id_for(cell_name, p) if pids is not None else None
            if p.layer == m1_spec['layer'] and p.datatype == m1_spec['datatype']:
                if pid == m1_pid:
                    continue
                other_geom = ShapelyPolygon(p.points)
                if other_geom.intersects(after_poly) or other_geom.distance(after_poly) <= 1e-6:
                    return {**bound, 'verified': False, 'connectivity_verified': False, 'reason': 'Proposed expansion shorts or touches another M1 polygon (connectivity impact)'}
                if other_geom.distance(after_poly) < clearance_user - 1e-6:
                    return {**bound, 'verified': False, 'neighbour_constraints_verified': False, 'reason': 'Neighboring M1 shape is within clearance margin of proposed expansion'}
            elif p.layer == ct_spec['layer'] and p.datatype == ct_spec['datatype']:
                if pid == ct_pid:
                    continue
                other_ct = ShapelyPolygon(p.points)
                if other_ct.intersects(after_poly) or other_ct.distance(after_poly) <= 1e-6:
                    return {**bound, 'verified': False, 'connectivity_verified': False, 'reason': 'Proposed expansion connects to an unrelated CT polygon'}

        return {
            **bound,
            'verified': True,
            'connectivity_verified': True,
            'neighbour_constraints_verified': True,
            'shared_cell_impact_verified': True,
            'reason': '',
        }

    def _unknown_result(self, reason: str) -> dict:
        """Build an indeterminate unknown measurement result with no proposals."""
        self.result = {
            'measurement': {
                'status': 'unknown',
                'reason': reason,
                'candidate_digest': self.candidate_digest,
                'units': 'dbu',
            },
            'proposals': [],
        }
        return self.result

    def _resolve_required_enclosure_dbu(self, scale: float):
        """Return explicit required enclosure in DBU or None when unproven."""
        value = self.rule_evidence.get('required_enclosure_dbu')
        multiplier = 1
        if value is None:
            value = self.rule_evidence.get('required_enclosure_user')
            multiplier = scale
        if value is None or isinstance(value, bool):
            return None
        try:
            required = Decimal(str(value)) * Decimal(str(multiplier))
            if not required.is_finite() or required < 0:
                return None
            return int(required.to_integral_value(rounding=ROUND_CEILING))
        except (InvalidOperation, ValueError):
            return None

    def _is_axis_aligned_rect(self, points: list) -> bool:
        """Return True when points form a non-degenerate axis-aligned rectangle."""
        if not isinstance(points, list) or len(points) < 4:
            return False
        poly = ShapelyPolygon(points)
        if not poly.is_valid or poly.area <= 0:
            return False
        rect = shapely_box(*poly.bounds)
        return poly.symmetric_difference(rect).area <= 1e-6

    def _bbox(self, points: list) -> tuple:
        """Return (min_x, max_x, min_y, max_y) for point list."""
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return min(xs), max(xs), min(ys), max(ys)

    def _to_dbu_number(self, value: float):
        """Return int when value is integral within tolerance, else rounded float."""
        rounded = round(float(value))
        if abs(float(value) - rounded) <= 1e-6:
            return int(rounded)
        return round(float(value), 6)


# === SECTION 3: Five-Tool User-Unit Proposal Builders ===
class CoverProposalBuilder:
    """Generates deterministic grid-snapped cover repair proposals in GDS user units."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(
        self,
        inner_points: list,
        groups: list,
        minimum_enclosure: float,
        grid: float = 1e-3,
        inner_id: str = 'inner',
        group_ids: list | None = None,
    ) -> dict:
        """Evaluate groups and generate deterministic rectangular cover expansion proposals."""
        self.inner_id = inner_id
        self.result = self._build(inner_points, groups, minimum_enclosure, grid, group_ids)
        return self.result

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self):
        """Initialize cover proposal builder state."""
        self.inner_id = None
        self.result = None

    # --------------------------------------------------------------------------
    # Proposal Generation Helpers
    # --------------------------------------------------------------------------
    def _build(self, inner_points, groups, minimum_enclosure, grid, group_ids) -> dict:
        """Check coverage and build grid-aligned rectangular expansions for failing single-polygon groups."""
        cover = CoverCheck().run(inner=inner_points, groups=groups, minimum=minimum_enclosure, precision=grid)
        if cover.get('status') in ('unknown', 'pass'):
            return {
                'status': cover['status'],
                'measurement': cover,
                'proposals': [],
                'reason': cover.get('reason', ''),
            }

        helper = RepairProposals()
        if not helper._is_axis_aligned_rect(inner_points):
            return {
                'status': 'unknown',
                'measurement': cover,
                'proposals': [],
                'reason': 'Non-rectangular inner polygon is not supported for automatic cover expansion',
            }

        min_ix, max_ix, min_iy, max_iy = helper._bbox(inner_points)
        grid_dec = Decimal(str(grid))
        min_enc_dec = Decimal(str(minimum_enclosure))
        changes = []
        updated_groups = []

        for idx, group in enumerate(groups):
            g_res = next((r for r in cover.get('group_results', []) if r.get('group_index') == idx), None)
            if g_res and g_res.get('status') == 'pass':
                updated_groups.append(group)
                continue
            if len(group) != 1 or not helper._is_axis_aligned_rect(group[0]):
                return {
                    'status': 'unknown',
                    'measurement': cover,
                    'proposals': [],
                    'reason': f'Group {idx} requires manual multi-polygon or non-rectangular selection',
                }
            outer_pts = group[0]
            min_ox, max_ox, min_oy, max_oy = helper._bbox(outer_pts)
            target_min_x = float(((Decimal(str(min_ix)) - min_enc_dec) / grid_dec).to_integral_value(rounding=ROUND_FLOOR) * grid_dec)
            target_max_x = float(((Decimal(str(max_ix)) + min_enc_dec) / grid_dec).to_integral_value(rounding=ROUND_CEILING) * grid_dec)
            target_min_y = float(((Decimal(str(min_iy)) - min_enc_dec) / grid_dec).to_integral_value(rounding=ROUND_FLOOR) * grid_dec)
            target_max_y = float(((Decimal(str(max_iy)) + min_enc_dec) / grid_dec).to_integral_value(rounding=ROUND_CEILING) * grid_dec)

            new_min_ox = min(min_ox, target_min_x)
            new_max_ox = max(max_ox, target_max_x)
            new_min_oy = min(min_oy, target_min_y)
            new_max_oy = max(max_oy, target_max_y)
            after_pts = [
                [new_min_ox, new_min_oy],
                [new_max_ox, new_min_oy],
                [new_max_ox, new_max_oy],
                [new_min_ox, new_max_oy],
            ]
            poly_id = group_ids[idx][0] if group_ids and idx < len(group_ids) and group_ids[idx] else f'group_{idx}_0'
            changes.append({
                'polygon_id': poly_id,
                'before_points_user': outer_pts,
                'after_points_user': after_pts,
            })
            updated_groups.append([after_pts])

        recheck = CoverCheck().run(inner=inner_points, groups=updated_groups, minimum=minimum_enclosure, precision=grid)
        if recheck.get('status') != 'pass':
            return {
                'status': 'unknown',
                'measurement': cover,
                'proposals': [],
                'reason': 'Proposed cover expansion failed local geometric recheck',
            }

        return {
            'status': 'fail',
            'measurement': cover,
            'proposals': [{
                'changes': changes,
                'rule_assumptions': 'Axis-aligned rectangular expansion snapped outward to GDS grid',
                'local_check': recheck,
            }],
            'reason': '',
        }


class AreaProposalBuilder:
    """Generates deterministic grid-snapped area repair proposals in GDS user units."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(
        self,
        polygon_points: list,
        minimum_area: float,
        grid: float = 1e-3,
        polygon_id: str = 'poly',
    ) -> dict:
        """Evaluate area and generate deterministic grid-aligned rectangular expansion proposals."""
        self.polygon_id = polygon_id
        self.result = self._build(polygon_points, minimum_area, grid, polygon_id)
        return self.result

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self):
        """Initialize area proposal builder state."""
        self.polygon_id = None
        self.result = None

    # --------------------------------------------------------------------------
    # Proposal Generation Helpers
    # --------------------------------------------------------------------------
    def _build(self, polygon_points, minimum_area, grid, polygon_id) -> dict:
        """Check area and compute a grid-snapped rectangular expansion meeting minimum_area."""
        area_res = AreaCheck().run(polygon=polygon_points, minimum_area=minimum_area, precision=grid)
        if area_res.get('status') in ('unknown', 'pass'):
            return {
                'status': area_res['status'],
                'measurement': area_res,
                'proposals': [],
                'reason': area_res.get('reason', ''),
            }

        helper = RepairProposals()
        if not helper._is_axis_aligned_rect(polygon_points):
            return {
                'status': 'unknown',
                'measurement': area_res,
                'proposals': [],
                'reason': 'Non-rectangular polygon geometry is not supported for automatic area expansion',
            }

        min_x, max_x, min_y, max_y = helper._bbox(polygon_points)
        width = Decimal(str(max_x)) - Decimal(str(min_x))
        height = Decimal(str(max_y)) - Decimal(str(min_y))
        grid_dec = Decimal(str(grid))
        min_area_dec = Decimal(str(minimum_area))

        new_max_x = max_x
        new_max_y = max_y
        if width >= height:
            req_height = min_area_dec / width
            snapped_h = (req_height / grid_dec).to_integral_value(rounding=ROUND_CEILING) * grid_dec
            new_max_y = float(Decimal(str(min_y)) + snapped_h)
        else:
            req_width = min_area_dec / height
            snapped_w = (req_width / grid_dec).to_integral_value(rounding=ROUND_CEILING) * grid_dec
            new_max_x = float(Decimal(str(min_x)) + snapped_w)

        after_pts = [
            [min_x, min_y],
            [new_max_x, min_y],
            [new_max_x, new_max_y],
            [min_x, new_max_y],
        ]
        recheck = AreaCheck().run(polygon=after_pts, minimum_area=minimum_area, precision=grid)
        if recheck.get('status') != 'pass':
            return {
                'status': 'unknown',
                'measurement': area_res,
                'proposals': [],
                'reason': 'Proposed area expansion failed local area recheck',
            }

        return {
            'status': 'fail',
            'measurement': area_res,
            'proposals': [{
                'changes': [{
                    'polygon_id': polygon_id,
                    'before_points_user': polygon_points,
                    'after_points_user': after_pts,
                }],
                'rule_assumptions': 'Axis-aligned rectangular expansion along shorter dimension snapped up to GDS grid',
                'local_check': recheck,
            }],
            'reason': '',
        }
