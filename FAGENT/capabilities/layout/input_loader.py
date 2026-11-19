"""Input layout and Calibre DRC report ingestion, validation, and JSON normalization."""

# ==============================================================================
# SECTION 1: Standard Library Imports
# ==============================================================================
import json
from pathlib import Path

# ==============================================================================
# SECTION 2: Internal FAGENT Module Imports
# ==============================================================================
from .process_gds import ProcessGDS
from ..drc.drc_parser import DrcParser


# ==============================================================================
# SECTION 3: Input Loader and Normalization Class
# ==============================================================================
class InputLoader(DrcParser):
    """Validates input file existence, runs GDS and DRC parsers, and serializes gds.json/drc.json."""

    # --------------------------------------------------------------------------
    # Output Filename and Artifact Constants
    # --------------------------------------------------------------------------
    GDS_OUTPUT = 'gds.json'
    DRC_OUTPUT = 'drc.json'
    ARTIFACT_FILES = ('candidate.gds', 'conversation.json', 'result.json', 'graph-checkpoints.sqlite3')
    OUTPUT_FILES = (GDS_OUTPUT, DRC_OUTPUT) + ARTIFACT_FILES

    # --------------------------------------------------------------------------
    # Main Ingestion Entry Point
    # --------------------------------------------------------------------------
    def run(self, gds_path=None, drc_path=None, output_dir=None, layer_map_files=None, verbose=None):
        for attr, val in (('gds_path', gds_path), ('drc_path', drc_path), ('output_dir', output_dir)):
            if val is not None:
                setattr(self, attr, Path(val).resolve())
        if layer_map_files is not None:
            self.layer_map_files = layer_map_files
        if verbose is not None:
            self.verbose = verbose

        if None in (self.gds_path, self.drc_path, self.output_dir):
            raise ValueError('gds_path, drc_path, and output_dir must be provided')

     
        for source in (self.gds_path, self.drc_path, *map(Path, self.layer_map_files or ())):
            if not source.is_file():
                raise ValueError(f'Input file does not exist: {source}')
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for name in self.OUTPUT_FILES:
            target = self.output_dir / name
            if target.exists() or target.is_symlink():
                raise ValueError(f'Output already exists: {target}')

        if self.verbose:
            print("[Phase 1/5] Ingesting Layout & Calibre DRC Results")
            print(f"  * Reading GDS layout: {self.gds_path.name}")
            print(f"  * Parsing Calibre DRC database: {self.drc_path.name}")

        self.gds_reader = ProcessGDS(str(self.gds_path), self.layer_map_files).run()
        self.gds_data = {
            'source': str(self.gds_path),
            'layer_maps': [str(Path(path).resolve()) for path in self.layer_map_files or ()],
            'coordinates': 'gds_database_units',
            'database': self.gds_reader.gds_db,
        }
        self.drc_data = self.read_drc(self.drc_path)
        self.write_json(self.GDS_OUTPUT, self.gds_data)
        self.write_json(self.DRC_OUTPUT, self.drc_data)

        if self.verbose:
            cells = list(self.gds_reader.gds_db.get('structs', {}).keys())
            total_viol = self.drc_data.get('total_violations', 0)
            rules = self.drc_data.get('rules', self.drc_data.get('violations_by_rule', ()))
            print(f"  * Layout parsed: {len(cells)} cell(s) {cells}")
            print(f"  * DRC violations found: {total_viol} across {len(rules)} rules")
            print(f"  * Exported intermediate files: {self.GDS_OUTPUT}, {self.DRC_OUTPUT} -> {self.output_dir}")

        return self

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self, gds_path=None, drc_path=None, output_dir=None, layer_map_files=None, verbose=True):
        self.gds_path = Path(gds_path).resolve() if gds_path else None
        self.drc_path = Path(drc_path).resolve() if drc_path else None
        self.output_dir = Path(output_dir).resolve() if output_dir else None
        self.layer_map_files = layer_map_files
        self.verbose = verbose
        self.gds_reader = None
        self.gds_data = None
        self.drc_data = None

    # --------------------------------------------------------------------------
    # Atomic JSON Serialization
    # --------------------------------------------------------------------------
    def write_json(self, name, data):
        with (self.output_dir / name).open('x') as output:
            json.dump(data, output, indent=2)
            output.write('\n')
