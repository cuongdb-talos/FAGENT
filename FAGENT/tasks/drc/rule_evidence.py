# === SECTION 1: Imports ===
import copy
from pathlib import Path
import re
import sys


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
        if not deck_paths:
            raise ValueError('deck_paths must be provided')

        rule_name = str(task['rule']).strip()
        self.task = copy.deepcopy(task)
        self.deck_paths = [Path(p).resolve() for p in deck_paths]
        self.layer_maps = [Path(p).resolve() for p in (layer_maps or [])]
        self.references = copy.deepcopy(list(references or []))

        found_deck, start_line, end_line, rule_text = self._find_rule_in_decks(rule_name)
        if not found_deck:
            self.result = {
                'rule': rule_name,
                'found': False,
                'deck_file': None,
                'line_start': None,
                'line_end': None,
                'rule_text': '',
                'layer_mapping': {},
                'references': self.references,
                'supported': False,
                'reason': f'Rule {rule_name!r} not found in provided decks',
            }
            return self.result

        layer_map_dict = self._parse_layer_maps(self.layer_maps)
        rule_info = self._resolve_rule_layers(rule_name, rule_text, layer_map_dict)
        matched_layers, is_supported, reason = rule_info['matched'], rule_info['supported'], rule_info['reason']
        required_enclosure_user = self._extract_enclosure_user(rule_text)

        self.result = {
            'rule': rule_name,
            'found': True,
            'deck_file': str(found_deck),
            'line_start': start_line,
            'line_end': end_line,
            'rule_text': rule_text,
            'target_layer': rule_info.get('target_layer'),
            'rule_type': rule_info.get('rule_type', 'unsupported'),
            'layer_mapping': matched_layers,
            'layer_mapping_provenance': {
                name: self.layer_mapping_provenance.get(name, [])
                for name in matched_layers
            },
            'mapping_diagnostics': copy.deepcopy(self.mapping_diagnostics),
            'required_enclosure_user': rule_info.get('required_enclosure_user', required_enclosure_user),
            'cover_groups': rule_info.get('cover_groups'),
            'required_area_user': rule_info.get('required_area_user'),
            'exception_regions': rule_info.get('exception_regions', []),
            'references': self.references,
            'supported': is_supported,
            'reason': reason,
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
            for start, end in self._rule_blocks(text, pattern):
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

    def density_evidence(self, rule_names: set[str], deck_paths: list) -> dict:
        """Classify exact rules from complete, unique blocks in the configured decks."""
        matches = {name: [] for name in rule_names}
        if not matches:
            return matches
        names = '|'.join(re.escape(name) for name in sorted(rule_names))
        pattern = re.compile(rf'^[ \t]*({names})[ \t]*\{{', re.MULTILINE)
        comments_and_strings = re.compile(r'/\*.*?\*/|"[^"\n]*"|//[^\n]*|^[ \t]*[@#][^\n]*', re.MULTILINE | re.DOTALL)
        unavailable = not deck_paths
        incomplete = set()
        for path in dict.fromkeys(Path(path).resolve() for path in deck_paths):
            try:
                source = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError):
                unavailable = True
                continue
            code = comments_and_strings.sub(self._mask_deck_text, source)
            blocks = list(self._rule_blocks(code, pattern))
            starts = {start for start, _ in blocks}
            incomplete.update(match[1] for match in pattern.finditer(code) if match.start() not in starts)
            for start, end in blocks:
                name = pattern.match(code, start)[1]
                matches[name].append({
                    'deck_file': str(path),
                    'line_start': source.count('\n', 0, start) + 1,
                    'line_end': source.count('\n', 0, end) + 1,
                    'density': bool(re.search(r'^\s*DENSITY\s', code[start:end].split('{', 1)[1], re.MULTILINE | re.IGNORECASE)),
                })
        evidence = {}
        for name, candidates in matches.items():
            if len(candidates) > 1:
                evidence[name] = {'reason': 'Density rule evidence is ambiguous across deck definitions'}
            elif unavailable or name in incomplete or not candidates:
                evidence[name] = {'reason': 'Density rule evidence is unavailable from configured decks'}
            else:
                evidence[name] = candidates[0]
        return evidence

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

    def _resolve_rule_layers(self, rule_name: str, rule_text: str, layer_map: dict) -> dict:
        """Identify physical layers, classify rule expressions, and emit explicit support diagnostics."""
        tokens = set(re.findall(r'\b[A-Z][A-Z0-9_]*\b', f'{rule_name} {rule_text}'.upper()))
        keywords = {
            'RECTANGLE', 'ENC', 'NOT', 'INTERACT', 'TOUCH', 'INSIDE', 'OUTSIDE',
            'ALSO', 'GOOD', 'OPPOSITE', 'ABUT', 'INT', 'INTERSECTING', 'ONLY',
            'REGION', 'COIN', 'EDGE', 'AND', 'OR', 'XOR', 'BY', 'WITH', 'LAYER',
            'CONNECT', 'ENCLOSURE', 'EXT', 'SEP', 'LENGTH', 'WIDTH', 'AREA',
        }
        candidate_layers = tokens - keywords
        matched = {}
        unmatched_derived = set()
        ambiguous_layers = self.mapping_diagnostics.get('ambiguous_layers', {})

        for cand in candidate_layers:
            if cand in layer_map:
                matched[cand] = layer_map[cand]
            elif cand.startswith('DERIVED_'):
                unmatched_derived.add(cand)

        if 'CT' in layer_map and ('CT' in rule_name or 'CT' in candidate_layers):
            matched['CT'] = layer_map['CT']
        if 'M1' in layer_map and ('M1' in rule_name or 'M1' in candidate_layers):
            matched['M1'] = layer_map['M1']

        if 'UNKNOWN_DERIVED' in rule_name or unmatched_derived:
            return {
                'matched': matched,
                'supported': False,
                'rule_type': 'unsupported',
                'reason': 'Derived or unknown layer semantics without physical layer mapping',
            }

        body = rule_text.partition('{')[2].rpartition('}')[0]
        expressions = [line.split('//', 1)[0].strip() for line in body.splitlines()]
        expressions = [line for line in expressions if line and not line.startswith(('@', '#'))]

        if not expressions:
            return {'matched': matched, 'supported': False, 'rule_type': 'unsupported', 'reason': 'No rule expression found'}

        expr_joined = ' '.join(expressions)

        # Boolean coverage with an optional sequence of explicit region exclusions.
        cover_pattern = re.compile(
            r'([A-Z0-9_]+)\s+NOT\s+\(\s*([A-Z0-9_]+)\s+AND\s+'
            r'\(\s*([A-Z0-9_]+)\s+OR\s+([A-Z0-9_]+)\s*\)\s*\)',
            re.IGNORECASE,
        )
        excluded = re.fullmatch(
            r'\(\s*\(\s*(.*?)\s*\)\s+NOT\s+INSIDE\s+([A-Z0-9_]+)\s*\)'
            r'\s+NOT\s+INSIDE\s+([A-Z0-9_]+)',
            expr_joined,
            re.IGNORECASE,
        )
        cover = cover_pattern.fullmatch(excluded[1] if excluded else expr_joined)
        exceptions = [name.upper() for name in excluded.groups()[1:]] if excluded else []
        if cover:
            inner, outer, alternative_a, alternative_b = (name.upper() for name in cover.groups())
            required = [inner, outer, alternative_a, alternative_b, *exceptions]
            err = self._check_required_layers(required, matched, ambiguous_layers)
            return {
                'matched': matched,
                'supported': not err,
                'rule_type': 'cover',
                'target_layer': inner,
                'cover_groups': [[outer], [alternative_a, alternative_b]],
                'required_enclosure_user': 0.0,
                'exception_regions': exceptions,
                'reason': err,
            }

        # 1. Bare directional enclosure (OPPOSITE without RECTANGLE) is unsupported
        bare_directional = re.fullmatch(
            r'ENC\s+([A-Z0-9_]+)\s+([A-Z0-9_]+)\s*<\s*([0-9]+(?:\.[0-9]+)?)\s+OPPOSITE',
            expressions[0],
            re.IGNORECASE,
        )
        if len(expressions) == 1 and bare_directional:
            return {
                'matched': matched,
                'supported': False,
                'rule_type': 'unsupported',
                'reason': 'Directional enclosure rule (OPPOSITE without RECTANGLE) is not supported',
            }

        # 2. Simple enclosure: ENC <INNER> <OUTER> < <val>
        simple_match = re.fullmatch(
            r'ENC\s+([A-Z0-9_]+)\s+([A-Z0-9_]+)\s*<\s*([0-9]+(?:\.[0-9]+)?)',
            expressions[0],
            re.IGNORECASE,
        )
        if len(expressions) == 1 and simple_match:
            req = [simple_match.group(1).upper(), simple_match.group(2).upper()]
            err = self._check_required_layers(req, matched, ambiguous_layers)
            if err:
                return {'matched': matched, 'supported': False, 'rule_type': 'enclosure', 'reason': err}
            return {'matched': matched, 'supported': True, 'rule_type': 'enclosure', 'reason': ''}

        # 3. Rectangular enclosure: [X = ] RECTANGLE ENC <INNER> <OUTER> < <val> [OPPOSITE]
        rect_match = re.fullmatch(
            r'(?:[A-Z_][A-Z0-9_]*\s*=\s*)?RECTANGLE\s+ENC\s+([A-Z0-9_]+)\s+([A-Z0-9_]+)\s*<\s*([0-9]+(?:\.[0-9]+)?)(?:\s+OPPOSITE)?',
            expressions[0],
            re.IGNORECASE,
        )
        if rect_match and (
            len(expressions) == 1
            or (len(expressions) == 2 and re.fullmatch(r'\([A-Z0-9_]+\s+NOT\s+[A-Z0-9_]+\)\s+NOT\s+INSIDE\s+EXCLU', expressions[1], re.IGNORECASE))
        ):
            req = [rect_match.group(1).upper(), rect_match.group(2).upper()]
            err = self._check_required_layers(req, matched, ambiguous_layers)
            if err:
                return {'matched': matched, 'supported': False, 'rule_type': 'rectangular_enclosure', 'reason': err}
            return {'matched': matched, 'supported': True, 'rule_type': 'rectangular_enclosure', 'reason': ''}

        # 4. Area rule with optional NOT INSIDE <REGION> exception regions
        area_match = re.search(r'\bAREA\s+([A-Z0-9_]+)\s*<\s*([0-9]+(?:\.[0-9]+)?)', expr_joined, re.IGNORECASE)
        if len(expressions) == 1 and area_match:
            target_layer = area_match.group(1).upper()
            area_val = float(area_match.group(2))
            exception_regions = [m.upper() for m in re.findall(r'\bNOT\s+INSIDE\s+([A-Z0-9_]+)', expr_joined, re.IGNORECASE)]
            req = [target_layer, *exception_regions]
            err = self._check_required_layers(req, matched, ambiguous_layers)
            if err:
                return {
                    'matched': matched,
                    'supported': False,
                    'rule_type': 'area',
                    'target_layer': target_layer,
                    'required_area_user': area_val,
                    'exception_regions': exception_regions,
                    'reason': err,
                }
            return {
                'matched': matched,
                'supported': True,
                'rule_type': 'area',
                'target_layer': target_layer,
                'required_area_user': area_val,
                'exception_regions': exception_regions,
                'reason': '',
            }

        if not matched:
            return {'matched': matched, 'supported': False, 'rule_type': 'unsupported', 'reason': 'No physical layers could be resolved from layer maps'}

        return {'matched': matched, 'supported': False, 'rule_type': 'unsupported', 'reason': 'Rule expression is not a supported simple enclosure'}

    @staticmethod
    def _check_required_layers(required_layers: list, matched: dict, ambiguous: dict) -> str:
        """Return explicit diagnostic reason if any required layer is ambiguous or missing."""
        ambig = [layer for layer in required_layers if layer in ambiguous]
        if ambig:
            return f'Ambiguous physical drawing mapping for layer(s): {", ".join(ambig)}'
        missing = [layer for layer in required_layers if layer not in matched]
        if missing:
            return f'Missing physical drawing mapping for layer(s): {", ".join(missing)}'
        return ''

    def _extract_enclosure_user(self, rule_text: str):
        """Extract an explicit scalar enclosure threshold in user units if unambiguous."""
        values = set()
        for raw_line in rule_text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith('@') or line.startswith('//') or line.startswith('#'):
                continue
            for match in re.finditer(r'\bENC(?:LOSURE)?\b[^<\n]*<\s*([0-9]+(?:\.[0-9]+)?)', line):
                values.add(float(match.group(1)))
        if len(values) == 1:
            return next(iter(values))
        return None
