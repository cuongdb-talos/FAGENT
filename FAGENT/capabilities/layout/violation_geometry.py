# === SECTION 1: Imports ===
import copy
import math
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

        if self._references_affect_region(cell, marker_geom):
            self.result = {
                'status': 'unsupported',
                'reason': 'Hierarchical references or paths in search region are unsupported',
                'cell': cell_name,
                'candidate_digest': self.candidate_digest,
                'matched_polygons': [],
            }
            return self.result

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

        shared_ok, shared_reason = self._verify_shared_cell_impact(cell_name, cell, marker_geom)
        self.result = {
            'status': 'matched',
            'reason': '',
            'cell': cell_name,
            'candidate_digest': self.candidate_digest,
            'matched_polygons': matched_list,
            'dbu_per_user_unit': scale,
            'shared_cell_impact_verified': shared_ok,
            'verification_evidence': {
                'candidate_digest': self.candidate_digest,
                'cell': cell_name,
                'polygon_ids': [m['polygon_id'] for m in matched_list],
                'shared_cell_impact_verified': shared_ok,
                'shared_cell_reason': shared_reason,
            },
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
        self.region_result = None

    # --------------------------------------------------------------------------
    # Bounded Region Query
    # --------------------------------------------------------------------------
    def query_region(
        self,
        layout,
        cell_name: str,
        bounds_user: list[float],
        candidate_digest: str | None,
        layer_specs: list[dict] | None = None,
    ) -> dict:
        """Query all direct polygons and conservative hierarchy overlaps within bounds_user."""
        min_x, min_y, max_x, max_y = self._validate_bounds_user(bounds_user)
        allowed_layers = self._parse_layer_specs(layer_specs)

        if hasattr(layout, 'load_library'):
            layout.load_library()
        cell = layout.get_cell(cell_name)
        region_geom = shapely_box(min_x, min_y, max_x, max_y)

        polygons = []
        for p in getattr(cell, 'polygons', None) or []:
            layer_num = int(p.layer)
            datatype_num = int(p.datatype)
            if allowed_layers is not None and (layer_num, datatype_num) not in allowed_layers:
                continue
            if len(p.points) < 3:
                continue
            poly_geom = ShapelyPolygon(p.points)
            if poly_geom.intersects(region_geom) or poly_geom.distance(region_geom) <= 1e-6:
                pid = layout.polygon_ids.id_for(cell_name, p)
                points = p.points.tolist() if hasattr(p.points, 'tolist') else [list(pt) for pt in p.points]
                polygons.append({
                    'id': pid,
                    'layer': layer_num,
                    'datatype': datatype_num,
                    'points': points,
                    'editable': True,
                })

        references_in_region = self._collect_references_in_region(cell, region_geom)
        paths_in_region = self._collect_paths_in_region(cell, region_geom)
        complete = len(references_in_region) == 0 and len(paths_in_region) == 0
        reason = '' if complete else 'Region contains unsupported hierarchical references or paths'

        self.region_result = {
            'cell': cell_name,
            'bounds_user': [min_x, min_y, max_x, max_y],
            'candidate_digest': candidate_digest,
            'coordinate_units': 'user',
            'polygons': polygons,
            'references_in_region': references_in_region,
            'paths_in_region': paths_in_region,
            'complete': complete,
            'reason': reason,
        }
        return self.region_result

    # --------------------------------------------------------------------------
    # Helper Methods
    # --------------------------------------------------------------------------
    def _validate_bounds_user(self, bounds_user) -> tuple[float, float, float, float]:
        """Validate bounds_user as four finite numbers [min_x, min_y, max_x, max_y]."""
        if not isinstance(bounds_user, (list, tuple)) or len(bounds_user) != 4:
            raise ValueError('bounds_user must be a 4-element list [min_x, min_y, max_x, max_y]')
        parsed = []
        for value in bounds_user:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError('bounds_user entries must be numeric')
            num = float(value)
            if not math.isfinite(num):
                raise ValueError('bounds_user entries must be finite')
            parsed.append(num)
        min_x, min_y, max_x, max_y = parsed
        if min_x > max_x or min_y > max_y:
            raise ValueError('bounds_user must satisfy min_x <= max_x and min_y <= max_y')
        return min_x, min_y, max_x, max_y

    def _parse_layer_specs(self, layer_specs) -> set[tuple[int, int]] | None:
        """Parse optional physical layer filter dicts into a set of (layer, datatype) tuples."""
        if layer_specs is None:
            return None
        if not isinstance(layer_specs, (list, tuple)):
            raise ValueError('layer_specs must be a list of {layer, datatype} dicts')
        allowed = set()
        for spec in layer_specs:
            if not isinstance(spec, dict) or 'layer' not in spec or 'datatype' not in spec:
                raise ValueError('Each layer spec must contain integer layer and datatype')
            layer = spec['layer']
            datatype = spec['datatype']
            if isinstance(layer, bool) or not isinstance(layer, int) or isinstance(datatype, bool) or not isinstance(datatype, int):
                raise ValueError('layer and datatype must be integers')
            allowed.add((layer, datatype))
        return allowed

    def _collect_references_in_region(self, cell, region_geom) -> list[dict]:
        """Collect cell references overlapping region_geom or having unknown bounds."""
        found = []
        for ref in getattr(cell, 'references', None) or []:
            ref_cell = getattr(ref, 'cell', None)
            ref_name = getattr(ref_cell, 'name', None) or getattr(ref, 'cell_name', None) or 'unknown'
            try:
                bbox = ref.bounding_box()
            except Exception:
                bbox = None
            if bbox is None:
                found.append({'cell': ref_name, 'bounds_known': False, 'editable': False})
                continue
            ref_box = shapely_box(bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1])
            if ref_box.intersects(region_geom) or ref_box.distance(region_geom) <= 1e-6:
                found.append({
                    'cell': ref_name,
                    'bounds_known': True,
                    'bounds_user': [float(bbox[0][0]), float(bbox[0][1]), float(bbox[1][0]), float(bbox[1][1])],
                    'editable': False,
                })
        return found

    def _collect_paths_in_region(self, cell, region_geom) -> list[dict]:
        """Collect path elements overlapping region_geom or having unknown bounds."""
        found = []
        for path in getattr(cell, 'paths', None) or []:
            layers = [int(v) for v in (getattr(path, 'layers', None) or [])]
            datatypes = [int(v) for v in (getattr(path, 'datatypes', None) or [])]
            bbox = self._path_bounding_box(path)
            if bbox is None:
                found.append({'layers': layers, 'datatypes': datatypes, 'bounds_known': False, 'editable': False})
                continue
            path_box = shapely_box(bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1])
            if path_box.intersects(region_geom) or path_box.distance(region_geom) <= 1e-6:
                found.append({
                    'layers': layers,
                    'datatypes': datatypes,
                    'bounds_known': True,
                    'bounds_user': [float(bbox[0][0]), float(bbox[0][1]), float(bbox[1][0]), float(bbox[1][1])],
                    'editable': False,
                })
        return found

    def _path_bounding_box(self, path):
        """Return ((min_x, min_y), (max_x, max_y)) for a path, or None if unavailable."""
        if hasattr(path, 'bounding_box'):
            try:
                bbox = path.bounding_box()
                if bbox is not None:
                    return bbox
            except Exception:
                return None
        if hasattr(path, 'to_polygons'):
            try:
                polys = path.to_polygons()
                if polys:
                    xs = [float(pt[0]) for poly in polys for pt in poly.points]
                    ys = [float(pt[1]) for poly in polys for pt in poly.points]
                    if xs and ys:
                        return ((min(xs), min(ys)), (max(xs), max(ys)))
            except Exception:
                return None
        return None
    def _verify_shared_cell_impact(self, cell_name: str, cell, marker_geom) -> tuple:
        """Verify cell is not instanced by parent cells and has no overlapping references or paths."""
        if getattr(cell, 'paths', None) and len(cell.paths) > 0:
            return False, 'Cell contains unsupported path elements'
        if self._references_affect_region(cell, marker_geom):
            return False, 'Hierarchical references overlap marker region'
        lib = getattr(self.layout, 'library', None)
        cells = getattr(lib, 'cells', None)
        if cells is None:
            return False, 'Missing layout library hierarchy information'
        for other in cells:
            if getattr(other, 'name', None) == cell_name:
                continue
            for ref in getattr(other, 'references', None) or []:
                ref_cell = getattr(ref, 'cell', None)
                ref_name = getattr(ref_cell, 'name', None) or getattr(ref, 'cell_name', None)
                if ref_name == cell_name:
                    return False, f'Cell {cell_name} is shared and instanced by {getattr(other, "name", "parent")}'
        return True, ''

    def _references_affect_region(self, cell, marker_geom) -> bool:
        """Return True if any cell reference can intersect or affect the marker search region."""
        refs = getattr(cell, 'references', None) or []
        for ref in refs:
            try:
                bbox = ref.bounding_box()
            except Exception:
                return True
            if bbox is None:
                continue
            ref_box = shapely_box(bbox[0][0], bbox[0][1], bbox[1][0], bbox[1][1])
            if ref_box.intersects(marker_geom) or ref_box.distance(marker_geom) <= 1e-6:
                return True
        return False

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
