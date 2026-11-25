"""GDS layout inspection, polygon editing, and candidate persistence using gdstk."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
import json
import math
from pathlib import Path
from tempfile import NamedTemporaryFile

import gdstk

from .polygon_ids import PolygonIds


# ==============================================================================
# SECTION 2: GDS Layout Manipulation Service Class
# ==============================================================================
class LayoutService:
    """Provides high-level geometric query, modification, and export operations on GDS files."""

    # --------------------------------------------------------------------------
    # Main Setup and Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, rundir=None, verbose=None, drc_files=None):
        if rundir is not None:
            self.rundir = Path(rundir).resolve()
        if verbose is not None:
            self.verbose = verbose
        if drc_files is not None:
            self.drc_files = list(drc_files)
        if self.rundir is None:
            raise ValueError('rundir must be provided')
        self.gds_data = json.loads((self.rundir / 'gds.json').read_text())
        self.drc_data = json.loads((self.rundir / 'drc.json').read_text())
        source = self.gds_data.get('source')
        if not isinstance(source, str) or not source:
            raise ValueError('gds.json must contain a source path')
        self.gds_path = Path(source)
        if not self.gds_path.is_file():
            raise FileNotFoundError(f'GDS source not found: {self.gds_path}')
        self.candidate = self.rundir / 'candidate.gds'
        self.library = None
        self.polygon_ids = None
        self.dirty = False
        self.last_validation = None
        self.drc_log = None
        self._last_replace = None
        return self

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, rundir=None, verbose=True, drc_files=None):
        self.rundir = Path(rundir).resolve() if rundir else None
        self.verbose = verbose
        self.drc_files = list(drc_files) if drc_files else []
        self.gds_data = None
        self.drc_data = None
        self.gds_path = None
        self.candidate = None
        self.library = None
        self.polygon_ids = None
        self.dirty = False
        self.last_validation = None
        self.drc_log = None
        self._last_replace = None

    # --------------------------------------------------------------------------
    # Allowed Capabilities Configuration
    # --------------------------------------------------------------------------
    def allowed_tools():
        return {'read_gds', 'replace_polygon', 'save_gds', 'validate'}

    allowed_tools = staticmethod(allowed_tools)

    # --------------------------------------------------------------------------
    # Layout and DRC Inspection Methods
    # --------------------------------------------------------------------------
    def read_drc(self, path=None):
        return self.read_drc_log(path)

    def read_drc_log(self, path=None):
        target = Path(path) if path else self.rundir / 'drc.json'
        self.drc_log = json.loads(target.read_text())
        return self.drc_log

    def read_gds(self, cell_name):
        cell = self.get_cell(cell_name)
        return {
            'cell': cell_name,
            'polygons': [
                {
                    'id': self.polygon_ids.id_for(cell_name, p),
                    'kind': 'polygon',
                    'editable': True,
                    'layer': p.layer,
                    'datatype': p.datatype,
                    'points': p.points.tolist(),
                }
                for p in cell.polygons
            ],
            'references': [
                {'kind': 'reference', 'editable': False, 'cell': ref.cell_name}
                for ref in cell.references
            ],
            'paths': [
                {'kind': 'path', 'editable': False, 'layers': list(path.layers), 'datatypes': list(path.datatypes)}
                for path in cell.paths
            ],
        }

    # --------------------------------------------------------------------------
    # Polygon Editing and Geometry Mutation
    # --------------------------------------------------------------------------
    def replace_polygon(self, cell_name, polygon_id, points):
        cell = self.get_cell(cell_name)
        old = self._get_polygon(cell_name, polygon_id)
        self._validate_points(points)
        new = self._create_polygon(points, old.layer, old.datatype)
        cell.remove(old)
        cell.add(new)
        self.polygon_ids.rebind(cell_name, polygon_id, old, new)
        self._last_replace = (cell_name, polygon_id, old, new)
        self.dirty = True
        self.last_validation = None
        return {
            'status': 'modified',
            'cell': cell_name,
            'polygon_id': polygon_id,
            'layer': old.layer,
            'datatype': old.datatype,
        }

    def rollback_last_replace(self):
        """Rollback the most recent in-memory polygon replacement if present."""
        if self._last_replace is None:
            return
        cell_name, polygon_id, old_polygon, new_polygon = self._last_replace
        self.rollback_replace(cell_name, polygon_id, old_polygon, new_polygon)
        self._last_replace = None

    def rollback_replace(self, cell_name, polygon_id, old_polygon, new_polygon):
        """Rollback in-memory polygon replacement and restore original polygon ID binding."""
        cell = self.get_cell(cell_name)
        cell.remove(new_polygon)
        cell.add(old_polygon)
        self.polygon_ids.rebind(cell_name, polygon_id, new_polygon, old_polygon)
        self._last_replace = None

    def apply_polygon_changes(self, cell_name: str, changes: list[dict]) -> dict:
        """Atomically apply a batch of polygon replacements in memory, rolling back on error."""
        if not isinstance(changes, list) or not changes:
            raise ValueError('changes must be a non-empty list')
        snapshot = {
            'cell_name': cell_name,
            'applied': [],
            'prev_dirty': self.dirty,
            'prev_last_validation': self.last_validation,
        }
        try:
            for change in changes:
                pid = change['polygon_id']
                pts = change['points']
                self.replace_polygon(cell_name=cell_name, polygon_id=pid, points=pts)
                snapshot['applied'].append(self._last_replace)
        except Exception:
            self.rollback_transaction(snapshot)
            raise
        return snapshot

    def rollback_transaction(self, snapshot: dict) -> None:
        """Rollback all polygon replacements recorded in snapshot."""
        if not isinstance(snapshot, dict):
            return
        for cell_name, polygon_id, old_polygon, new_polygon in reversed(snapshot.get('applied', [])):
            self.rollback_replace(cell_name, polygon_id, old_polygon, new_polygon)
        self.dirty = snapshot.get('prev_dirty', False)
        self.last_validation = snapshot.get('prev_last_validation')

    def _get_polygon(self, cell_name, polygon_id):
        old = self.polygon_ids.resolve(cell_name, polygon_id)
        if old.properties:
            raise ValueError('Cannot replace polygon with GDS properties')
        return old

    def _validate_points(self, points):
        if not isinstance(points, list) or len(points) < 3 or any(
            not isinstance(p, list) or len(p) != 2 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in p)
            for p in points):
            raise ValueError('points must contain at least three finite [x, y] coordinates')

    def _create_polygon(self, points, layer, datatype):
        new = gdstk.Polygon(points, layer=layer, datatype=datatype)
        if new.area() <= 0:
            raise ValueError('Polygon must have nonzero area')
        return new

    # --------------------------------------------------------------------------
    # Safety Verification and File Persistence
    # --------------------------------------------------------------------------
    def check_output_path(self, output):
        output = Path(output)
        if not output.resolve().is_relative_to(self.rundir):
            raise ValueError(f'Output escapes rundir: {output}')
        sources = [self.gds_path, *map(Path, self.drc_files), *map(Path, self.gds_data.get('layer_maps', []))]
        drc_source = self.drc_data.get('source')
        if isinstance(drc_source, str) and drc_source:
            sources.append(Path(drc_source))
        for source in sources:
            if output.resolve() == source.resolve() or (output.exists() and source.exists() and output.samefile(source)):
                raise ValueError(f'Output must not overwrite input: {output}')

    def save_gds(self):
        self.check_output_path(self.candidate)
        self.rundir.mkdir(parents=True, exist_ok=True)
        self.load_library()
        tmp = NamedTemporaryFile(dir=self.rundir, suffix='.gds', delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        try:
            self.library.write_gds(str(tmp_path))
            tmp_path.replace(self.candidate)
        finally:
            tmp_path.unlink(missing_ok=True)
        if getattr(self, 'verbose', True):
            print(f"    [Candidate] Candidate layout saved to {self.candidate}")
        return {'candidate': str(self.candidate), 'validated': False}

    def get_cell(self, name):
        self.load_library()
        matches = [cell for cell in self.library.cells if cell.name == name]
        if len(matches) != 1:
            raise ValueError(f'Unknown or ambiguous cell: {name}')
        return matches[0]

    def load_library(self):
        if self.library is None:
            self.library = gdstk.read_gds(str(self.gds_path))
            self.polygon_ids = PolygonIds(self.library).run()
        elif self.polygon_ids is None:
            self.polygon_ids = PolygonIds(self.library).run()
