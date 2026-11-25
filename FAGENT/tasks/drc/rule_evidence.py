# === SECTION 1: Imports ===
import copy
from pathlib import Path
import re


# === SECTION 2: Rule Evidence Service ===
class RuleEvidence:
    """Extracts exact rule definitions, lines, and physical layer maps from DRC decks."""

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, task: dict, deck_paths: list, layer_maps: list = None, references: list = None) -> dict:
        """Execute rule discovery and layer mapping provenance."""
        if not isinstance(task, dict) or not task.get('rule'):
            raise ValueError('Task must be a dict containing a rule name')

        rule_name = str(task['rule']).strip()
        self.task = copy.deepcopy(task)
        self.deck_paths = [Path(p).resolve() for p in deck_paths]
        self.layer_maps = [Path(p).resolve() for p in (layer_maps or [])]
        self.references = copy.deepcopy(list(references or []))

        found_deck, start_line, end_line, rule_text = self._find_rule_in_decks(rule_name)
        mapping = self._parse_layer_maps(self.layer_maps)
        self.result = {
            'rule': rule_name,
            'found': found_deck is not None,
            'deck_file': str(found_deck) if found_deck else None,
            'line_start': start_line,
            'line_end': end_line,
            'rule_text': rule_text,
            'layer_mapping': mapping,
            'layer_mapping_provenance': copy.deepcopy(self.layer_mapping_provenance),
            'mapping_diagnostics': copy.deepcopy(self.mapping_diagnostics),
            'references': self.references,
            'reason': '' if found_deck else (
                f'Rule {rule_name!r} not found in provided decks' if deck_paths else 'No DRC rule deck configured'
            ),
        }
        return self.result

    # --------------------------------------------------------------------------
    # Initialization
    # --------------------------------------------------------------------------
    def __init__(self):
        """Initialize rule evidence attributes."""
        self.task = None
        self.deck_paths = []
        self.layer_maps = []
        self.references = []
        self.layer_mapping_provenance = {}
        self.mapping_diagnostics = {'missing_sources': [], 'ambiguous_layers': {}, 'non_drawing_entries': {}}
        self.result = None

    # --------------------------------------------------------------------------
    # Step Methods
    # --------------------------------------------------------------------------
    def _find_rule_in_decks(self, rule_name: str):
        """Scan DRC deck files to find the exact rule block and line range."""
        pattern = re.compile(rf'^[ \t]*{re.escape(rule_name)}[ \t]*\{{', re.MULTILINE)
        for deck_path in self.deck_paths:
            if not deck_path.is_file():
                continue
            text = deck_path.read_text(encoding='utf-8', errors='replace')
            code = re.sub(r'/\*.*?\*/|"[^"\n]*"|//[^\n]*|^[ \t]*[@#][^\n]*',
                          self._mask_deck_text, text, flags=re.MULTILINE | re.DOTALL)
            for start, end in self._rule_blocks(code, pattern):
                return deck_path, text.count('\n', 0, start) + 1, text.count('\n', 0, end) + 1, text[start:end]

        return None, None, None, ''

    @staticmethod
    def _rule_blocks(text, pattern):
        for match in pattern.finditer(text):
            depth = 1
            for index in range(match.end(), len(text)):
                if text[index] == '{':
                    depth += 1
                elif text[index] == '}':
                    depth -= 1
                    if depth == 0:
                        yield match.start(), index + 1
                        break

    @staticmethod
    def _mask_deck_text(match):
        return re.sub(r'[^\n]', ' ', match[0])

    def _parse_layer_maps(self, layer_map_paths: list) -> dict:
        """Parse configured layer mapping files with provenance, rejecting text/ambiguous substitutions."""
        mapping = {}
        self.layer_mapping_provenance = {}
        self.mapping_diagnostics = {'missing_sources': [], 'ambiguous_layers': {}, 'non_drawing_entries': {}}
        for path in layer_map_paths:
            if not path.is_file():
                self.mapping_diagnostics['missing_sources'].append(str(path))
                continue
            for line_no, raw_line in enumerate(path.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
                line = raw_line.strip()
                if not line or line.startswith('#') or line.startswith('//'):
                    continue
                tokens = line.split()
                name = purpose = None
                layer = datatype = None
                # Format A: CT drawing 50 0
                if len(tokens) >= 4 and tokens[2].isdigit() and tokens[3].isdigit():
                    name, purpose = tokens[0].upper(), tokens[1].lower()
                    layer, datatype = int(tokens[2]), int(tokens[3])
                # Format B: 61 0 M1 drawing
                elif len(tokens) >= 4 and tokens[0].isdigit() and tokens[1].isdigit():
                    layer, datatype = int(tokens[0]), int(tokens[1])
                    name, purpose = tokens[2].upper(), tokens[3].lower()
                # Format B (no explicit purpose): 61 0 M1
                elif len(tokens) == 3 and tokens[0].isdigit() and tokens[1].isdigit():
                    layer, datatype = int(tokens[0]), int(tokens[1])
                    name, purpose = tokens[2].upper(), 'unknown'
                else:
                    continue

                entry = {
                    'layer': layer,
                    'datatype': datatype,
                    'purpose': purpose,
                    'source': str(path),
                    'line': line_no,
                }
                if purpose != 'drawing':
                    self.mapping_diagnostics['non_drawing_entries'].setdefault(name, []).append(entry)
                    continue

                if name in self.mapping_diagnostics['ambiguous_layers']:
                    self.mapping_diagnostics['ambiguous_layers'][name].append(entry)
                    continue

                if name in mapping:
                    existing = mapping[name]
                    if (existing['layer'], existing['datatype']) != (layer, datatype):
                        prior = self.layer_mapping_provenance.pop(name, [])
                        del mapping[name]
                        self.mapping_diagnostics['ambiguous_layers'][name] = [*prior, entry]
                    else:
                        self.layer_mapping_provenance[name].append(entry)
                else:
                    mapping[name] = {'layer': layer, 'datatype': datatype}
                    self.layer_mapping_provenance[name] = [entry]
        return mapping


def cover_requirements(evidence, layout, polygons, layout_service=None):
    """Resolve simple SVRF cover rules into ordered groups of polygon IDs."""
    rule = evidence.get('rule_text') or ''
    body = re.sub(r'(?m)^\s*@[^\n]*', '', rule.partition('{')[2].rpartition('}')[0]).strip()
    name = r'[A-Za-z_][A-Za-z_0-9]*'
    match = re.fullmatch(
        rf'(\(*)(?P<inner>{name})\s+NOT\s+(?P<cover>.+)', body, re.S,
    )
    if not match:
        return None
    cover = match['cover'].strip()
    exceptions = []
    for _ in match[1]:
        exception = re.fullmatch(rf'(.*)\)\s+NOT\s+INSIDE\s+({name})', cover, re.S)
        if not exception:
            return None
        cover, layer = exception.groups()
        exceptions.append(layer)
    cover = cover.strip()
    if cover.startswith('(') and cover.endswith(')'):
        cover = cover[1:-1]
    groups = []
    for clause in re.split(r'\s+AND\s+', cover):
        clause = clause.strip()
        if re.fullmatch(name, clause):
            groups.append([clause])
        elif re.fullmatch(rf'\(\s*{name}(?:\s+OR\s+{name})+\s*\)', clause):
            groups.append(re.split(r'\s+OR\s+', clause[1:-1].strip()))
        else:
            return {'reason': 'Coverage expression cannot be represented by fix_cover groups'}

    mapping = evidence.get('layer_mapping') or {}
    layers = [match['inner'], *exceptions, *(layer for group in groups for layer in group)]
    bounds = (layout.get('search_region') or {}).get('bounds_user')
    if not bounds or any(layer not in mapping for layer in layers):
        return {'reason': 'Coverage requires a search region and mapped rule layers'}

    inner_lpp = mapping[match['inner']]
    targets = {p['id']: p['points'] for p in polygons
               if (p['layer'], p['datatype']) == (inner_lpp['layer'], inner_lpp['datatype'])}
    target_points = [point for points in targets.values() for point in points]
    if target_points and layout_service is not None and layout.get('cell'):
        xs, ys = zip(*target_points)
        expanded = [min(bounds[0], min(xs)), min(bounds[1], min(ys)),
                    max(bounds[2], max(xs)), max(bounds[3], max(ys))]
        if expanded != list(bounds):
            from FAGENT.capabilities.layout.violation_geometry import ViolationGeometry

            layout = ViolationGeometry().query_region(layout_service, layout['cell'], expanded, layout.get('candidate_digest'))
            polygons = layout['polygons']
            bounds = expanded
    if not layout.get('complete'):
        return {'reason': 'Coverage requires complete region geometry'}

    def ids(layer):
        lpp = mapping[layer]
        return [p['id'] for p in polygons if (p['layer'], p['datatype']) == (lpp['layer'], lpp['datatype'])]

    if any(ids(layer) for layer in exceptions):
        return {'reason': 'Exception geometry requires a rule-aware coverage check'}
    inners = [
        p['id'] for p in polygons if p['id'] in targets
        and all(bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3] for x, y in p['points'])
    ]
    group_ids = [list(dict.fromkeys(pid for layer in group for pid in ids(layer))) for group in groups]
    if not inners or any(not group for group in group_ids):
        return {'reason': 'Missing target polygon or required coverage group in the search region'}
    return {'inner_ids': inners, 'group_polygon_ids': group_ids, 'minimum_enclosure': 0}
