"""Geometric polygon coverage and minimum enclosure checker using Shapely/GEOS."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points


# ==============================================================================
# SECTION 2: Geometric Enclosure Checker Class
# ==============================================================================
class CoverCheck:
    """Evaluates full polygon coverage and boundary-to-boundary enclosure margin."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, inner=None, outer=None, minimum: float = 0.0, precision: float = 1e-3) -> dict:
        """Evaluate whether outer encloses inner by at least minimum in GDS user units."""
        self.result = self._evaluate(inner, outer, minimum, precision)
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
    def _evaluate(self, inner, outer, minimum, precision) -> dict:
        """Validate inputs, check coverage, and collect all violating boundary segments."""
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
        outer_poly, err_out = self._to_polygon(outer, 'outer')
        if err_out:
            return self._unknown(min_req, err_out)

        if inner_poly.area <= tol * tol or outer_poly.area <= tol * tol:
            return self._unknown(min_req, 'Degenerate zero-area polygon geometry')
        if len(inner_poly.interiors) > 0 or len(outer_poly.interiors) > 0:
            return self._unknown(min_req, 'Polygons with interior holes are unsupported')

        inner_boundary = inner_poly.boundary
        outer_boundary = outer_poly.boundary
        outer_edges = self._extract_segments(outer_boundary, tol)
        if not outer_edges:
            return self._unknown(min_req, 'Outer polygon has no valid boundary segments')

        covered = (
            inner_poly.difference(outer_poly).area <= tol * tol
            and inner_poly.difference(outer_poly.buffer(tol)).is_empty
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
        outside_geom = inner_boundary.difference(outer_poly.buffer(tol))
        for seg_coords in self._extract_segments(outside_geom, tol):
            violations.append(
                self._outside_violation(seg_coords, outer_boundary, outer_edges, min_req, tol)
            )

        if min_req > tol:
            inside_geom = inner_boundary.intersection(outer_poly.buffer(tol))
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
