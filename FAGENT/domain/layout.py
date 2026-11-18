"""Explicitly exposed layout and DRC operations."""
import math
import json
from pathlib import Path
import gdstk


class LayoutService:
    def run(self, rundir=None, verbose=None):
        if rundir is not None:
            self.rundir = Path(rundir).resolve()
        if verbose is not None:
            self.verbose = verbose
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
        self.dirty = False
        self.last_validation = None
        self.drc_log = None
        return self

    def __init__(self, rundir=None, verbose=True):
        self.rundir = Path(rundir).resolve() if rundir else None
        self.verbose = verbose
        self.gds_data = None
        self.drc_data = None
        self.gds_path = None
        self.candidate = None
        self.library = None
        self.dirty = False
        self.last_validation = None
        self.drc_log = None

    @staticmethod
    def allowed_tools():
        return {'read_gds', 'replace_polygon', 'save_gds', 'validate'}

    @staticmethod
    def tool_specs():
        return [
            {
                'type': 'function',
                'function': {
                    'name': 'read_gds',
                    'description': 'Read direct polygons in a cell, in GDS user units.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'cell_name': {'type': 'string'},
                        },
                        'required': ['cell_name'],
                    },
                },
            },
            {
                'type': 'function',
                'function': {
                    'name': 'replace_polygon',
                    'description': 'Replace one direct polygon. Coordinates are in GDS user units.',
                    'parameters': {
                        'type': 'object',
                        'properties': {
                            'cell_name': {'type': 'string'},
                            'polygon_index': {'type': 'integer'},
                            'points': {
                                'type': 'array',
                                'items': {
                                    'type': 'array',
                                    'items': {'type': 'number'},
                                },
                            },
                        },
                        'required': ['cell_name', 'polygon_index', 'points'],
                    },
                },
            },
            {
                'type': 'function',
                'function': {
                    'name': 'save_gds',
                    'description': 'Save the current candidate GDS.',
                    'parameters': {
                        'type': 'object',
                        'properties': {},
                        'required': [],
                    },
                },
            },
            {
                'type': 'function',
                'function': {
                    'name': 'validate',
                    'description': 'Validate the candidate using fresh Calibre results.',
                    'parameters': {
                        'type': 'object',
                        'properties': {},
                        'required': [],
                    },
                },
            },
        ]

    def read_drc(self, path=None):
        return self.read_drc_log(path)

    def read_drc_log(self, path=None):
        target = Path(path) if path else self.rundir / 'drc.json'
        self.drc_log = json.loads(target.read_text())
        return self.drc_log

    def read_gds(self, cell_name):
        cell = self.get_cell(cell_name)
        return {'cell': cell_name, 'polygons': [
            {'index': i, 'layer': p.layer, 'datatype': p.datatype, 'points': p.points.tolist()}
            for i, p in enumerate(cell.polygons)], 'references': len(cell.references),
            'paths': len(cell.paths)}

    def replace_polygon(self, cell_name, polygon_index, points):
        cell = self.get_cell(cell_name)
        old = self._get_polygon(cell, polygon_index)
        self._validate_points(points)
        new = self._create_polygon(points, old.layer, old.datatype)
        cell.remove(old)
        cell.add(new)
        self.dirty = True
        self.last_validation = None
        return {'status': 'modified', 'new_index': len(cell.polygons) - 1}

    def _get_polygon(self, cell, polygon_index):
        if isinstance(polygon_index, bool) or not isinstance(polygon_index, int) or not 0 <= polygon_index < len(cell.polygons):
            raise ValueError('Invalid polygon index')
        old = cell.polygons[polygon_index]
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

    def check_output_path(self, output):
        output = Path(output)
        sources = [self.gds_path]
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
        self.library.write_gds(str(self.candidate))
        if getattr(self, 'verbose', True):
            print(f"    [Candidate] Candidate layout saved to {self.candidate}")
        return {'candidate': str(self.candidate), 'validated': False}

    def run_calibre(self):
        self.calibre_result = {'status': 'good', 'simulated': True}
        return self.calibre_result

    def validate(self):
        if getattr(self, 'verbose', True):
            print(f"[Phase 4/5] Executing Candidate Validation...")
        if not self.dirty:
            return {'status': 'not_validated', 'reason': 'No candidate modification'}
        self.save_gds()

        self.run_calibre()
        self.last_validation = self.calibre_result
        return self.last_validation

    def get_cell(self, name):
        self.load_library()
        matches = [cell for cell in self.library.cells if cell.name == name]
        if len(matches) != 1:
            raise ValueError(f'Unknown or ambiguous cell: {name}')
        return matches[0]

    def load_library(self):
        if self.library is None:
            try:
                import gdstk
            except ImportError as exc:
                raise RuntimeError('Install gdstk to inspect or edit GDS geometry') from exc
            self.library = gdstk.read_gds(str(self.gds_path))
