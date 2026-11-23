"""Stable run-scoped polygon identity registry and checkpoint restoration."""

# ==============================================================================
# SECTION 1: Standard and Third-Party Library Imports
# ==============================================================================
import json
from pathlib import Path
from tempfile import NamedTemporaryFile


# ==============================================================================
# SECTION 2: Stable Polygon Identity Registry Class
# ==============================================================================
class PolygonIds:
    """Assigns, resolves, persists, and verifies stable per-cell polygon IDs."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, library=None):
        """Build a fresh run-scoped ID registry from a gdstk.Library."""
        if library is not None:
            self.library = library
        if self.library is None:
            raise ValueError('gdstk.Library must be provided')
        self._reset()
        seen_cells = set()
        for cell in self.library.cells:
            if cell.name in seen_cells:
                raise ValueError(f'Duplicate cell name in library: {cell.name}')
            seen_cells.add(cell.name)
            cell_ids = []
            for idx, polygon in enumerate(cell.polygons):
                polygon_id = f'{cell.name}:poly:{idx}'
                self._register(cell.name, polygon_id, polygon)
                cell_ids.append(polygon_id)
            self._cell_order[cell.name] = cell_ids
        return self

    # --------------------------------------------------------------------------
    # Initialization and State Reset
    # --------------------------------------------------------------------------
    def __init__(self, library=None):
        """Initialize empty polygon identity maps."""
        self.library = library
        self._obj_to_id = {}
        self._id_to_obj = {}
        self._cell_order = {}

    def _reset(self):
        """Clear all registered polygon mappings."""
        self._obj_to_id = {}
        self._id_to_obj = {}
        self._cell_order = {}

    def _register(self, cell_name, polygon_id, polygon):
        """Bind a single polygon object to its cell-scoped ID."""
        key = (cell_name, polygon_id)
        if key in self._id_to_obj:
            raise ValueError(f'Duplicate polygon ID {polygon_id} in cell {cell_name}')
        self._id_to_obj[key] = polygon
        self._obj_to_id[(cell_name, id(polygon))] = polygon_id

    # --------------------------------------------------------------------------
    # Lookup, Resolution, and Edit Rebinding
    # --------------------------------------------------------------------------
    def id_for(self, cell_name: str, polygon) -> str:
        """Return the stable ID for a direct polygon in cell_name."""
        polygon_id = self._obj_to_id.get((cell_name, id(polygon)))
        if polygon_id is None:
            raise ValueError(f'Unregistered polygon in cell {cell_name}')
        return polygon_id

    def resolve(self, cell_name: str, polygon_id: str):
        """Resolve a cell-scoped polygon_id back to its live gdstk.Polygon."""
        if not isinstance(polygon_id, str) or not polygon_id:
            raise ValueError('polygon_id must be a non-empty string')
        polygon = self._id_to_obj.get((cell_name, polygon_id))
        if polygon is None:
            raise ValueError(f'Unknown polygon_id {polygon_id!r} for cell {cell_name!r}')
        return polygon

    def rebind(self, cell_name: str, polygon_id: str, old_polygon, new_polygon):
        """Transfer an existing polygon_id from old_polygon to its replacement."""
        current = self.resolve(cell_name, polygon_id)
        if current is not old_polygon:
            raise ValueError(f'Polygon identity mismatch for {polygon_id} in {cell_name}')
        self._obj_to_id.pop((cell_name, id(old_polygon)), None)
        self._id_to_obj[(cell_name, polygon_id)] = new_polygon
        self._obj_to_id[(cell_name, id(new_polygon))] = polygon_id

    # --------------------------------------------------------------------------
    # Geometry Signatures and Source Annotation
    # --------------------------------------------------------------------------
    def library_scale(self, library) -> float:
        """Return DBU per user unit from the GDS library precision."""
        unit = getattr(library, 'unit', 1e-6)
        precision = getattr(library, 'precision', 1e-9)
        if not unit or not precision or precision <= 0:
            raise ValueError('Invalid GDS library unit or precision')
        return float(unit) / float(precision)

    def polygon_signature(self, library, polygon):
        """Return canonical (layer, datatype, tuple_of_dbu_xy) for a gdstk.Polygon."""
        scale = self.library_scale(library)
        pts = tuple((int(round(float(x) * scale)), int(round(float(y) * scale))) for x, y in polygon.points)
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        return (int(polygon.layer), int(polygon.datatype), pts)

    def raw_poly_signature(self, entry: dict):
        """Return canonical (layer, datatype, tuple_of_dbu_xy) for a ProcessGDS poly dict."""
        coord = entry.get('coord') or []
        pts = tuple((int(coord[i]), int(coord[i + 1])) for i in range(0, len(coord) - 1, 2))
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        return (int(entry['layer']), int(entry['data_type']), pts)

    def annotate_gds_db(self, gds_db: dict) -> dict:
        """Annotate initial gds.json structs with deterministic IDs or uneditable markers."""
        structs = gds_db.get('structs', {})
        cell_map = {cell.name: cell for cell in self.library.cells}
        for struct_name, struct_data in structs.items():
            cell = cell_map.get(struct_name)
            sig_to_ids = {}
            if cell is not None:
                for polygon in cell.polygons:
                    sig = self.polygon_signature(self.library, polygon)
                    sig_to_ids.setdefault(sig, []).append(self.id_for(struct_name, polygon))

            poly_entries = struct_data.get('poly', [])
            source_counts = {}
            for entry in poly_entries:
                if entry.get('kind') != 'path':
                    sig = self.raw_poly_signature(entry)
                    source_counts[sig] = source_counts.get(sig, 0) + 1

            for entry in poly_entries:
                if entry.get('kind') == 'path':
                    entry['editable'] = False
                    entry.pop('id', None)
                    continue
                entry['kind'] = 'polygon'
                sig = self.raw_poly_signature(entry)
                matches = sig_to_ids.get(sig, [])
                if len(matches) == 1 and source_counts.get(sig, 0) == 1:
                    entry['id'] = matches[0]
                    entry['editable'] = True
                    entry['match_status'] = 'matched'
                else:
                    entry.pop('id', None)
                    entry['editable'] = False
                    entry['match_status'] = 'ambiguous' if (len(matches) > 1 or source_counts.get(sig, 0) > 1) else 'unmatched'
        return gds_db

    # --------------------------------------------------------------------------
    # Checkpoint Serialization and Bijective Restoration
    # --------------------------------------------------------------------------
    def save(self, path: Path, candidate_digest: str) -> None:
        """Atomically persist the ID-to-geometry mapping bound to candidate_digest."""
        if self.library is None:
            raise ValueError('Cannot save PolygonIds without a library')
        if not isinstance(candidate_digest, str) or not candidate_digest:
            raise ValueError('candidate_digest must be a non-empty string')
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        cells_payload = {}
        for cell in self.library.cells:
            occurrence_counts = {}
            entries = []
            for idx, polygon in enumerate(cell.polygons):
                polygon_id = self.id_for(cell.name, polygon)
                sig = self.polygon_signature(self.library, polygon)
                occ = occurrence_counts.get(sig, 0)
                occurrence_counts[sig] = occ + 1
                entries.append({
                    'id': polygon_id,
                    'kind': 'polygon',
                    'index': idx,
                    'layer': sig[0],
                    'datatype': sig[1],
                    'points_dbu': [list(pt) for pt in sig[2]],
                    'occurrence': occ,
                })
            cells_payload[cell.name] = entries

        payload = {
            'version': 1,
            'candidate_digest': candidate_digest,
            'cells': cells_payload,
        }
        tmp = NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent, delete=False)
        tmp_path = Path(tmp.name)
        try:
            with tmp:
                json.dump(payload, tmp, indent=2)
                tmp.write('\n')
            tmp_path.replace(target)
        finally:
            tmp_path.unlink(missing_ok=True)

    def restore(self, path: Path, library, candidate_digest: str) -> 'PolygonIds':
        """Restore and strictly verify the 1-to-1 ID mapping against library and digest."""
        source = Path(path)
        if not source.is_file():
            raise ValueError(f'Missing polygon ID mapping file: {source}')
        try:
            payload = json.loads(source.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f'Invalid polygon ID mapping file: {source}') from exc
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise ValueError('Unsupported polygon ID mapping format')
        if not candidate_digest or payload.get('candidate_digest') != candidate_digest:
            raise ValueError('Polygon ID mapping digest does not match candidate')

        saved_cells = payload.get('cells')
        if not isinstance(saved_cells, dict):
            raise ValueError('Polygon ID mapping missing cells')

        runtime_cells = {}
        for cell in library.cells:
            if cell.name in runtime_cells:
                raise ValueError(f'Duplicate cell name in library: {cell.name}')
            runtime_cells[cell.name] = cell

        if set(saved_cells.keys()) != set(runtime_cells.keys()):
            raise ValueError('Polygon ID mapping cell set does not match candidate library')

        self.library = library
        self._reset()
        for cell_name, entries in saved_cells.items():
            cell = runtime_cells[cell_name]
            if not isinstance(entries, list) or len(entries) != len(cell.polygons):
                raise ValueError(f'Polygon count mismatch for cell {cell_name}')
            occurrence_counts = {}
            seen_ids = set()
            cell_ids = []
            for idx, (entry, polygon) in enumerate(zip(entries, cell.polygons)):
                if not isinstance(entry, dict):
                    raise ValueError(f'Invalid polygon mapping entry in {cell_name}')
                polygon_id = entry.get('id')
                if not isinstance(polygon_id, str) or not polygon_id or polygon_id in seen_ids:
                    raise ValueError(f'Invalid or duplicate polygon_id in mapping for {cell_name}')
                if entry.get('index') != idx:
                    raise ValueError(f'Polygon index mismatch for {polygon_id} in {cell_name}')
                sig = self.polygon_signature(library, polygon)
                occ = occurrence_counts.get(sig, 0)
                occurrence_counts[sig] = occ + 1
                expected_pts = [list(pt) for pt in sig[2]]
                if (
                    entry.get('layer') != sig[0]
                    or entry.get('datatype') != sig[1]
                    or entry.get('points_dbu') != expected_pts
                    or entry.get('occurrence') != occ
                ):
                    raise ValueError(f'Polygon geometry mismatch for {polygon_id} in {cell_name}')
                seen_ids.add(polygon_id)
                self._register(cell_name, polygon_id, polygon)
                cell_ids.append(polygon_id)
            self._cell_order[cell_name] = cell_ids
        return self
