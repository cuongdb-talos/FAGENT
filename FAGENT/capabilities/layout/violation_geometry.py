# === SECTION 1: Imports ===
import copy
from pathlib import Path
import sys

from shapely.geometry import box as shapely_box, LineString, Point as ShapelyPoint, Polygon as ShapelyPolygon


# === SECTION 2: Violation Geometry Service ===
class ViolationGeometry:
    """Matches DRC violation markers to live layout polygons and computes DBU geometry."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, task: dict, rule_evidence: dict, layout, candidate_digest: str = None, search_margin_dbu: float = 0.0) -> dict:
        """Resolve DRC marker to candidate layout polygons with candidate digest binding."""
        self.task = copy.deepcopy(task) if isinstance(task, dict) else {}
        self.rule_evidence = copy.deepcopy(rule_evidence) if isinstance(rule_evidence, dict) else {}
        self.layout = layout
        self.candidate_digest = candidate_digest
        self.search_margin_dbu = float(search_margin_dbu)

        if not self.rule_evidence or not self.rule_evidence.get('supported'):
            reason = self.rule_evidence.get('reason') if self.rule_evidence else 'Missing rule evidence'
            self.result = {
                'status': 'unsupported',
                'reason': reason or 'Unsupported rule semantics',
                'cell': None,
                'candidate_digest': self.candidate_digest,
                'matched_polygons': [],
            }
            return self.result

        violation = self.task.get('violation')
        if not violation or not violation.get('points_dbu'):
            self.result = {
                'status': 'unsupported',
                'reason': 'Missing violation marker geometry',
                'cell': None,
                'candidate_digest': self.candidate_digest,
                'matched_polygons': [],
            }
            return self.result

        if hasattr(self.layout, 'load_library'):
            self.layout.load_library()
        scale = self.layout.polygon_ids.library_scale(self.layout.library)
        cell_name = (
            getattr(self.layout, 'top_cell', None)
            or (self.layout.gds_data.get('top_cell') if hasattr(self.layout, 'gds_data') and isinstance(self.layout.gds_data, dict) else None)
            or self.task.get('owning_cell')
        )
        if not cell_name:
            self.result = {
                'status': 'unsupported',
                'reason': 'Cannot determine target layout cell name',
                'cell': None,
                'candidate_digest': self.candidate_digest,
                'matched_polygons': [],
            }
            return self.result

        cell = self.layout.get_cell(cell_name)
        if getattr(cell, 'paths', None) and len(cell.paths) > 0:
            self.result = {
                'status': 'unsupported',
                'reason': 'Hierarchical references or paths in search region are unsupported',
                'cell': cell_name,
                'candidate_digest': self.candidate_digest,
                'matched_polygons': [],
            }
            return self.result

        pts_dbu = violation['points_dbu']
        pts_user = [[x / scale, y / scale] for x, y in pts_dbu]
        margin_user = self.search_margin_dbu / scale
        marker_geom = self._create_marker_geometry(pts_user, margin_user)

        layer_mapping = self.rule_evidence.get('layer_mapping', {})
        matched_list = []

        for layer_name, layer_spec in layer_mapping.items():
            target_layer = layer_spec['layer']
            target_datatype = layer_spec['datatype']

            candidate_polys = []
            for p in cell.polygons:
                if p.layer == target_layer and p.datatype == target_datatype:
                    if len(p.points) >= 3:
                        poly_geom = ShapelyPolygon(p.points)
                        if poly_geom.intersects(marker_geom) or poly_geom.distance(marker_geom) <= 1e-6:
                            candidate_polys.append((p, poly_geom))

            if len(candidate_polys) == 0:
                self.result = {
                    'status': 'unsupported',
                    'reason': f'No polygon found on layer {layer_name}',
                    'cell': cell_name,
                    'candidate_digest': self.candidate_digest,
                    'matched_polygons': [],
                }
                return self.result

            if len(candidate_polys) > 1:
                self.result = {
                    'status': 'ambiguous',
                    'reason': f'Multiple polygon candidates found for layer {layer_name}',
                    'cell': cell_name,
                    'candidate_digest': self.candidate_digest,
                    'matched_polygons': [],
                }
                return self.result

            p, poly_geom = candidate_polys[0]
            pid = self.layout.polygon_ids.id_for(cell_name, p)
            points_dbu = [[round(x * scale), round(y * scale)] for x, y in p.points]

            relation = 'contains' if poly_geom.contains(marker_geom) else 'intersects'
            matched_list.append({
                'layer_name': layer_name,
                'layer': target_layer,
                'datatype': target_datatype,
                'polygon_id': pid,
                'points_dbu': points_dbu,
                'points_user': p.points.tolist(),
                'spatial_relation': relation,
            })

        self.result = {
            'status': 'matched',
            'reason': '',
            'cell': cell_name,
            'candidate_digest': self.candidate_digest,
            'matched_polygons': matched_list,
            'dbu_per_user_unit': scale,
        }
        return self.result

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self):
        """Initialize violation geometry attributes."""
        self.task = None
        self.rule_evidence = None
        self.layout = None
        self.candidate_digest = None
        self.search_margin_dbu = 0.0
        self.result = None

    # --------------------------------------------------------------------------
    # Helper Methods
    # --------------------------------------------------------------------------
    def _create_marker_geometry(self, pts_user: list, margin: float):
        """Construct a Shapely geometry from user coordinates with optional margin."""
        if len(pts_user) == 1:
            pt = ShapelyPoint(pts_user[0])
            return pt.buffer(max(margin, 1e-6))
        if len(pts_user) == 2:
            min_x = min(pts_user[0][0], pts_user[1][0]) - margin
            max_x = max(pts_user[0][0], pts_user[1][0]) + margin
            min_y = min(pts_user[0][1], pts_user[1][1]) - margin
            max_y = max(pts_user[0][1], pts_user[1][1]) + margin
            if abs(max_x - min_x) < 1e-6:
                max_x += 1e-6
            if abs(max_y - min_y) < 1e-6:
                max_y += 1e-6
            return shapely_box(min_x, min_y, max_x, max_y)
        # 3 or more points: polygon
        try:
            poly = ShapelyPolygon(pts_user)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if margin > 0:
                poly = poly.buffer(margin)
            return poly
        except Exception:
            min_x = min(p[0] for p in pts_user) - margin
            max_x = max(p[0] for p in pts_user) + margin
            min_y = min(p[1] for p in pts_user) - margin
            max_y = max(p[1] for p in pts_user) + margin
            return shapely_box(min_x, min_y, max_x, max_y)
