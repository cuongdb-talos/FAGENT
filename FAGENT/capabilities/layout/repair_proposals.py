# === SECTION 1: Imports ===
import copy
from hashlib import sha256
import json
from pathlib import Path
import sys


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

        if not self.geometry or self.geometry.get('status') != 'matched':
            reason = self.geometry.get('reason') if self.geometry else 'Unmatched or ambiguous geometry'
            self.result = {
                'measurement': {
                    'status': 'unknown',
                    'reason': reason or 'Geometry is not matched',
                    'candidate_digest': self.candidate_digest,
                    'units': 'dbu',
                },
                'proposals': [],
            }
            return self.result

        rule_name = str(self.task.get('rule', '')).strip()
        rule_text = str(self.rule_evidence.get('rule_text', ''))

        # CT_R_11 requires CT covered by M1 AND (GT OR AA) and evaluation of INST/EXCLU
        if rule_name == 'CT_R_11' or '(GT OR AA)' in rule_text or 'INST' in rule_text:
            self.result = {
                'measurement': {
                    'status': 'unknown',
                    'reason': 'CT_R_11 requires full coverage by M1 and (GT OR AA), except INST/EXCLU; M1 coverage alone cannot pass full rule',
                    'candidate_digest': self.candidate_digest,
                    'units': 'dbu',
                },
                'proposals': [],
            }
            return self.result

        matched_polys = self.geometry.get('matched_polygons', [])
        ct_poly = next((p for p in matched_polys if p.get('layer_name') == 'CT'), None)
        m1_poly = next((p for p in matched_polys if p.get('layer_name') == 'M1'), None)

        if not ct_poly or not m1_poly:
            self.result = {
                'measurement': {
                    'status': 'unknown',
                    'reason': 'Supported enclosure requires both CT and M1 matched polygons',
                    'candidate_digest': self.candidate_digest,
                    'units': 'dbu',
                },
                'proposals': [],
            }
            return self.result

        scale = float(self.geometry.get('dbu_per_user_unit', 1000.0))
        required = int(self.rule_evidence.get('required_enclosure_dbu', 25))

        # Calculate bounding boxes in DBU
        ct_pts = ct_poly['points_dbu']
        m1_pts = m1_poly['points_dbu']

        min_ix = min(p[0] for p in ct_pts)
        max_ix = max(p[0] for p in ct_pts)
        min_iy = min(p[1] for p in ct_pts)
        max_iy = max(p[1] for p in ct_pts)

        min_ox = min(p[0] for p in m1_pts)
        max_ox = max(p[0] for p in m1_pts)
        min_oy = min(p[1] for p in m1_pts)
        max_oy = max(p[1] for p in m1_pts)

        left = min_ix - min_ox
        right = max_ox - max_ix
        bottom = min_iy - min_oy
        top = max_oy - max_iy

        min_enclosure = min(left, right, bottom, top)
        contributing_edges = []
        if left < required:
            contributing_edges.append('left')
        if right < required:
            contributing_edges.append('right')
        if bottom < required:
            contributing_edges.append('bottom')
        if top < required:
            contributing_edges.append('top')

        if min_enclosure >= required:
            status = 'pass'
            deficit = 0
            proposals = []
        else:
            status = 'fail'
            deficit = required - min_enclosure
            # Propose expansion of M1 on contributing edges
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
            after_pts_user = [[round(x / scale, 6), round(y / scale, 6)] for x, y in after_pts_dbu]

            primary_edge = contributing_edges[0] if len(contributing_edges) == 1 else 'multiple'
            task_id_str = self.task.get('task_id', 'task-1')
            poly_id_str = m1_poly['polygon_id'].replace(':', '_')
            repair_id = f"repair-{task_id_str}-{poly_id_str}-{primary_edge}"

            proposals = [{
                'repair_id': repair_id,
                'target_polygon_id': m1_poly['polygon_id'],
                'cell': self.geometry.get('cell'),
                'edge': primary_edge,
                'delta_dbu': deficit,
                'before_points_dbu': m1_pts,
                'after_points_dbu': after_pts_dbu,
                'after_points_user': after_pts_user,
                'rule_assumptions': 'Assumes rectangular contact enclosure without adjacent competing spacing constraints or exclusion regions',
                'candidate_digest': self.candidate_digest,
                'post_measurement': {
                    'status': 'pass',
                    'measured_enclosure_dbu': required,
                    'deficit_dbu': 0,
                    'units': 'dbu',
                },
            }]

        self.result = {
            'measurement': {
                'status': status,
                'measured_enclosure_dbu': min_enclosure,
                'required_enclosure_dbu': required,
                'deficit_dbu': deficit,
                'contributing_edges': contributing_edges,
                'units': 'dbu',
                'candidate_digest': self.candidate_digest,
            },
            'proposals': proposals,
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
