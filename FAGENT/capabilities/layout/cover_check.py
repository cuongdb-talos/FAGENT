"""Geometric polygon coverage and minimum enclosure checker using Shapely/GEOS."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
import math

from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import nearest_points, unary_union


# ==============================================================================
# SECTION 2: Geometric Enclosure Checker Class
# ==============================================================================
class CoverCheck:
    """Evaluates full polygon coverage and boundary-to-boundary enclosure margin."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, inner=None, outer=None, minimum: float = 0.0, precision: float = 1e-3, groups=None) -> dict:
        """Evaluate whether outer or every outer group encloses inner by at least minimum in GDS user units."""
        self.result = self._evaluate(inner, outer, minimum, precision, groups=groups)
        return self.result

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self):
        """Initialize the enclosure checker."""
        self.result = None

    # --------------------------------------------------------------------------
    # Core Evaluation Pipeline
    # --------------------------------------------------------------------------
    def _evaluate(self, inner, outer, minimum, precision, groups=None) -> dict:
        """Validate inputs, check coverage across outer or groups, and collect violations."""
        if not self._is_valid_number(minimum) or float(minimum) < 0.0:
            return self._unknown(minimum, 'minimum enclosure must be a finite non-negative number')
        if not self._is_valid_number(precision) or float(precision) <= 0.0:
            return self._unknown(minimum, 'GDS precision must be a positive finite number')

        min_req = float(minimum)
        prec = float(precision)
        tol = 0.5 * prec

        inner_poly, err_in = self._to_polygon(inner, 'inner')
        if err_in:
            return self._unknown(min_req, err_in)
        if inner_poly.area <= tol * tol:
            return self._unknown(min_req, 'Degenerate zero-area polygon geometry')

        if groups is not None:
            return self._evaluate_groups(inner_poly, groups, min_req, tol)

        outer_poly, err_out = self._to_polygon(outer, 'outer')
        if err_out:
            return self._unknown(min_req, err_out)
        if outer_poly.area <= tol * tol:
            return self._unknown(min_req, 'Degenerate zero-area polygon geometry')
        return self._evaluate_pair(inner_poly, outer_poly, min_req, tol)

    def _evaluate_groups(self, inner_poly, groups, min_req: float, tol: float) -> dict:
        """Evaluate coverage where every group union must independently cover inner_poly."""
        if not isinstance(groups, (list, tuple)) or len(groups) == 0:
            return self._unknown(min_req, 'groups must be a non-empty list of polygon groups')

        group_results = []
        all_violations = []
        for idx, group in enumerate(groups):
            if not isinstance(group, (list, tuple)) or len(group) == 0:
                return self._unknown(min_req, f'Group {idx} must contain at least one polygon')
            polys = []
            for member in group:
                poly, err = self._to_polygon(member, f'group[{idx}]')
                if err:
                    return self._unknown(min_req, err)
                if poly.area <= tol * tol:
                    return self._unknown(min_req, f'Degenerate zero-area polygon in group {idx}')
                polys.append(poly)
            group_geom = unary_union(polys)
            if group_geom.is_empty or not group_geom.is_valid or group_geom.area <= tol * tol:
                return self._unknown(min_req, f'Invalid union geometry in group {idx}')
            res = self._evaluate_pair(inner_poly, group_geom, min_req, tol)
            res['group_index'] = idx
            group_results.append(res)
            if res['status'] == 'unknown':
                return res
            if res['status'] == 'fail':
                for v in res.get('violations', []):
                    all_violations.append({**v, 'group_index': idx})

        if all_violations:
            return {
                'status': 'fail',
                'minimum': min_req,
                'measured_distance': None,
                'units': 'gds_user_units',
                'violations': all_violations,
                'group_results': group_results,
                'reason': 'One or more groups failed required enclosure',
            }

        distances = [r['measured_distance'] for r in group_results if r.get('measured_distance') is not None]
        return {
            'status': 'pass',
            'minimum': min_req,
            'measured_distance': min(distances) if distances else 0.0,
            'units': 'gds_user_units',
            'violations': [],
            'group_results': group_results,
            'reason': 'All groups satisfy full enclosure and minimum distance',
        }

    def _evaluate_pair(self, inner_poly, outer_geom, min_req: float, tol: float) -> dict:
        """Evaluate coverage and boundary-to-boundary enclosure between inner_poly and outer_geom."""
        inner_boundary = inner_poly.boundary
        outer_boundary = outer_geom.boundary
        outer_edges = self._extract_segments(outer_boundary, tol)
        if not outer_edges:
            return self._unknown(min_req, 'Outer polygon has no valid boundary segments')

        covered = (
            inner_poly.difference(outer_geom).area <= tol * tol
            and inner_poly.difference(outer_geom.buffer(tol)).is_empty
        )

        if covered:
            measured = float(inner_boundary.distance(outer_boundary))
            if measured + tol >= min_req:
                return {
                    'status': 'pass',
                    'minimum': min_req,
                    'measured_distance': measured,
                    'units': 'gds_user_units',
                    'violations': [],
                    'reason': 'Full enclosure satisfies minimum distance',
                }
        else:
            measured = None

        violations = []
        outside_geom = inner_boundary.difference(outer_geom.buffer(tol))
        for seg_coords in self._extract_segments(outside_geom, tol):
            violations.append(
                self._outside_violation(seg_coords, outer_boundary, outer_edges, min_req, tol)
            )

        if not covered and not violations:
            uncovered_geom = inner_poly.difference(outer_geom)
            for seg_coords in self._extract_segments(uncovered_geom.boundary, tol):
                violations.append(
                    self._outside_violation(seg_coords, outer_boundary, outer_edges, min_req, tol)
                )

        if min_req > tol:
            inside_geom = inner_boundary.intersection(outer_geom.buffer(tol))
            insufficient_zone = outer_boundary.buffer(min_req - tol)
            violating_inside = inside_geom.intersection(insufficient_zone)
            for seg_coords in self._extract_segments(violating_inside, tol):
                violation = self._inside_violation(seg_coords, outer_boundary, outer_edges, min_req, tol)
                if violation is not None:
                    violations.append(violation)

        if not violations:
            return self._unknown(
                min_req,
                'Enclosure failure could not be decomposed into boundary violation segments',
                measured_distance=measured,
            )

        return {
            'status': 'fail',
            'minimum': min_req,
            'measured_distance': measured,
            'units': 'gds_user_units',
            'violations': violations,
            'reason': 'One or more inner boundary segments violate required enclosure',
        }

    # --------------------------------------------------------------------------
    # Geometry Helpers
    # --------------------------------------------------------------------------
    def _is_valid_number(self, value) -> bool:
        """Return True when value is a finite int/float and not a bool."""
        return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))

    def _unknown(self, minimum, reason: str, measured_distance=None) -> dict:
        """Build an indeterminate unknown response."""
        return {
            'status': 'unknown',
            'minimum': float(minimum) if self._is_valid_number(minimum) else None,
            'measured_distance': measured_distance,
            'units': 'gds_user_units',
            'violations': [],
            'reason': reason,
        }

    def _to_polygon(self, shape, label: str):
        """Convert a gdstk.Polygon, Shapely Polygon, or coordinate list to a valid Shapely Polygon."""
        if shape is None:
            return None, f'Missing {label} polygon'
        if isinstance(shape, Polygon):
            poly = shape
        elif hasattr(shape, 'points'):
            poly = Polygon(shape.points)
        else:
            try:
                pts = list(shape)
            except TypeError:
                return None, f'Unsupported {label} geometry type'
            if len(pts) < 3:
                return None, f'{label} polygon requires at least 3 vertices'
            for pt in pts:
                if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                    return None, f'Invalid coordinate in {label} polygon'
                if not self._is_valid_number(pt[0]) or not self._is_valid_number(pt[1]):
                    return None, f'Non-finite coordinate in {label} polygon'
            poly = Polygon(pts)
        if poly.is_empty or not poly.is_valid:
            return None, f'Invalid {label} polygon geometry'
        return poly, None

    def _extract_segments(self, geom, tol: float) -> list:
        """Extract non-degenerate 2-point boundary segments from a Shapely geometry."""
        if geom is None or geom.is_empty:
            return []
        segments = []
        if isinstance(geom, LineString):
            coords = list(geom.coords)
            for i in range(len(coords) - 1):
                p0 = [float(coords[i][0]), float(coords[i][1])]
                p1 = [float(coords[i + 1][0]), float(coords[i + 1][1])]
                if math.hypot(p1[0] - p0[0], p1[1] - p0[1]) > tol:
                    segments.append([p0, p1])
            return segments
        if hasattr(geom, 'geoms'):
            for sub in geom.geoms:
                segments.extend(self._extract_segments(sub, tol))
        return segments

    def _associate_outer_edge(self, ref_geom, target_dist: float, outer_edges: list, tol: float):
        """Find the closest outer boundary edge, or return None if ambiguous."""
        matches = []
        for edge in outer_edges:
            edge_line = LineString(edge)
            if abs(float(ref_geom.distance(edge_line)) - target_dist) <= tol:
                matches.append(edge)
        if len(matches) == 1:
            return matches[0], 'matched', None
        if len(matches) > 1:
            return None, 'ambiguous', 'Multiple outer boundary segments are equidistant within GDS precision'
        return None, 'unmatched', 'No outer boundary segment matched within GDS precision'

    def _outside_violation(self, seg_coords: list, outer_boundary, outer_edges: list, min_req: float, tol: float) -> dict:
        """Build a violation record for an inner boundary segment outside the cover."""
        p0 = Point(seg_coords[0])
        p1 = Point(seg_coords[1])
        pmid = Point((seg_coords[0][0] + seg_coords[1][0]) * 0.5, (seg_coords[0][1] + seg_coords[1][1]) * 0.5)
        candidates = (
            (float(p0.distance(outer_boundary)), p0),
            (float(p1.distance(outer_boundary)), p1),
            (float(pmid.distance(outer_boundary)), pmid),
        )
        max_dist, worst_pt = max(candidates, key=lambda item: item[0])
        _, outer_pt = nearest_points(worst_pt, outer_boundary)
        outer_seg, assoc_status, assoc_reason = self._associate_outer_edge(worst_pt, max_dist, outer_edges, tol)
        return {
            'kind': 'outside',
            'inner_segment': seg_coords,
            'outer_segment': outer_seg,
            'association_status': assoc_status,
            'association_reason': assoc_reason,
            'measured_distance': -max_dist,
            'shortage': max_dist + min_req,
            'measurement_location': {
                'inner_point': [float(worst_pt.x), float(worst_pt.y)],
                'outer_point': [float(outer_pt.x), float(outer_pt.y)],
            },
        }

    def _inside_violation(self, seg_coords: list, outer_boundary, outer_edges: list, min_req: float, tol: float):
        """Build a violation record for an inside boundary segment with insufficient enclosure."""
        seg = LineString(seg_coords)
        dist = float(seg.distance(outer_boundary))
        shortage = min_req - dist
        if shortage <= tol:
            return None
        inner_pt, outer_pt = nearest_points(seg, outer_boundary)
        outer_seg, assoc_status, assoc_reason = self._associate_outer_edge(inner_pt, dist, outer_edges, tol)
        return {
            'kind': 'insufficient_margin',
            'inner_segment': seg_coords,
            'outer_segment': outer_seg,
            'association_status': assoc_status,
            'association_reason': assoc_reason,
            'measured_distance': dist,
            'shortage': shortage,
            'measurement_location': {
                'inner_point': [float(inner_pt.x), float(inner_pt.y)],
                'outer_point': [float(outer_pt.x), float(outer_pt.y)],
            },
        }


# ==============================================================================
# SECTION 3: Geometric Area Checker Class
# ==============================================================================
class AreaCheck:
    """Evaluates polygon area against a minimum area limit in GDS user units."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, polygon=None, minimum_area: float = 0.0, precision: float = 1e-3) -> dict:
        """Evaluate whether polygon area meets minimum_area in squared GDS user units."""
        self.result = self._evaluate(polygon, minimum_area, precision)
        return self.result

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self):
        """Initialize the area checker."""
        self.result = None

    # --------------------------------------------------------------------------
    # Core Evaluation Pipeline
    # --------------------------------------------------------------------------
    def _evaluate(self, polygon, minimum_area, precision) -> dict:
        """Validate polygon and minimum_area, then measure area."""
        checker = CoverCheck()
        if not checker._is_valid_number(minimum_area) or float(minimum_area) <= 0.0:
            return {
                'status': 'unknown',
                'minimum_area': None,
                'measured_area': None,
                'deficit_area': None,
                'units': 'gds_user_units_sq',
                'reason': 'minimum_area must be a finite positive number',
            }
        if not checker._is_valid_number(precision) or float(precision) <= 0.0:
            return {
                'status': 'unknown',
                'minimum_area': float(minimum_area),
                'measured_area': None,
                'deficit_area': None,
                'units': 'gds_user_units_sq',
                'reason': 'GDS precision must be a positive finite number',
            }

        min_area = float(minimum_area)
        tol = 0.5 * float(precision)
        poly, err = checker._to_polygon(polygon, 'polygon')
        if err:
            return {
                'status': 'unknown',
                'minimum_area': min_area,
                'measured_area': None,
                'deficit_area': None,
                'units': 'gds_user_units_sq',
                'reason': err,
            }
        if poly.area <= tol * tol:
            return {
                'status': 'unknown',
                'minimum_area': min_area,
                'measured_area': float(poly.area),
                'deficit_area': None,
                'units': 'gds_user_units_sq',
                'reason': 'Degenerate zero-area polygon geometry',
            }

        measured = float(poly.area)
        deficit = max(0.0, min_area - measured)
        if measured + 1e-12 >= min_area:
            return {
                'status': 'pass',
                'minimum_area': min_area,
                'measured_area': measured,
                'deficit_area': 0.0,
                'units': 'gds_user_units_sq',
                'reason': 'Polygon area satisfies minimum area',
            }
        return {
            'status': 'fail',
            'minimum_area': min_area,
            'measured_area': measured,
            'deficit_area': deficit,
            'units': 'gds_user_units_sq',
            'reason': 'Polygon area is below minimum area',
        }


class WidthCheck:
    """Check the minimum bounding width/height of an axis-aligned rectangle."""

    def run(self, polygon=None, minimum_width: float = 0.0, precision: float = 1e-3) -> dict:
        self.result = self._evaluate(polygon, minimum_width, precision)
        return self.result

    def __init__(self):
        self.result = None

    def _evaluate(self, polygon, minimum_width, precision):
        checker = CoverCheck()
        if not checker._is_valid_number(minimum_width) or float(minimum_width) <= 0:
            return {'status': 'unknown', 'reason': 'minimum_width must be a finite positive number', 'units': 'gds_user_units'}
        if not checker._is_valid_number(precision) or float(precision) <= 0:
            return {'status': 'unknown', 'reason': 'GDS precision must be a positive finite number', 'units': 'gds_user_units'}
        poly, error = checker._to_polygon(polygon, 'polygon')
        if error:
            return {'status': 'unknown', 'reason': error, 'units': 'gds_user_units'}
        x0, y0, x1, y1 = poly.bounds
        rect = box(x0, y0, x1, y1)
        if poly.symmetric_difference(rect).area > 1e-12:
            return {'status': 'unknown', 'reason': 'Minimum width check supports axis-aligned rectangles only', 'units': 'gds_user_units'}
        measured = min(x1 - x0, y1 - y0)
        status = 'pass' if measured + 1e-12 >= float(minimum_width) else 'fail'
        return {'status': status, 'minimum_width': float(minimum_width), 'measured_width': measured,
                'units': 'gds_user_units', 'reason': 'Polygon width satisfies minimum width' if status == 'pass' else 'Polygon width is below minimum width'}
