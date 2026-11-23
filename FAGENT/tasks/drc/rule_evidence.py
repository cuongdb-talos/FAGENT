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
        matched_layers, is_supported, reason = self._resolve_rule_layers(rule_name, rule_text, layer_map_dict)

        self.result = {
            'rule': rule_name,
            'found': True,
            'deck_file': str(found_deck),
            'line_start': start_line,
            'line_end': end_line,
            'rule_text': rule_text,
            'layer_mapping': matched_layers,
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
            match = pattern.search(text)
            if not match:
                continue
            start_pos = match.start()
            start_line = text.count('\n', 0, start_pos) + 1
            # Find matching closing brace
            brace_count = 0
            end_pos = start_pos
            for i in range(match.end() - 1, len(text)):
                if text[i] == '{':
                    brace_count += 1
                elif text[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_pos = i
                        break
            end_line = text.count('\n', 0, end_pos) + 1
            rule_text = text[start_pos:end_pos + 1]
            return deck_path, start_line, end_line, rule_text

        return None, None, None, ''

    def _parse_layer_maps(self, layer_map_paths: list) -> dict:
        """Parse layer mapping files into a name -> (layer, datatype) mapping."""
        mapping = {}
        for path in layer_map_paths:
            if not path.is_file():
                continue
            for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('//'):
                    continue
                tokens = line.split()
                # Format A: CT drawing 50 0
                if len(tokens) >= 4 and tokens[2].isdigit() and tokens[3].isdigit():
                    name = tokens[0].upper()
                    layer = int(tokens[2])
                    datatype = int(tokens[3])
                    mapping[name] = {'layer': layer, 'datatype': datatype}
                # Format B: 61 0 M1 drawing
                elif len(tokens) >= 3 and tokens[0].isdigit() and tokens[1].isdigit():
                    layer = int(tokens[0])
                    datatype = int(tokens[1])
                    name = tokens[2].upper()
                    mapping[name] = {'layer': layer, 'datatype': datatype}
        return mapping

    def _resolve_rule_layers(self, rule_name: str, rule_text: str, layer_map: dict):
        """Identify physical layers and check for unsupported derived layer semantics."""
        # Find all uppercase alphanumeric identifier tokens in rule text and name
        tokens = set(re.findall(r'\b[A-Z][A-Z0-9_]*\b', f'{rule_name} {rule_text}'))
        # Exclude common Calibre DRC keywords
        keywords = {
            'RECTANGLE', 'ENC', 'NOT', 'INTERACT', 'TOUCH', 'INSIDE', 'OUTSIDE',
            'ALSO', 'GOOD', 'OPPOSITE', 'ABUT', 'INT', 'INTERSECTING', 'ONLY',
            'REGION', 'COIN', 'EDGE', 'AND', 'OR', 'XOR', 'BY', 'WITH', 'LAYER',
            'CONNECT', 'ENCLOSURE', 'EXT', 'SEP', 'LENGTH', 'WIDTH', 'AREA',
        }
        candidate_layers = tokens - keywords
        matched = {}
        unmatched_derived = set()

        for cand in candidate_layers:
            if cand in layer_map:
                matched[cand] = layer_map[cand]
            elif cand.startswith('DERIVED_') or cand in {'EXCLU', 'INST'}:
                unmatched_derived.add(cand)

        # Specifically for CT / M1 rules
        if 'CT' in layer_map and ('CT' in rule_name or 'CT' in candidate_layers):
            matched['CT'] = layer_map['CT']
        if 'M1' in layer_map and ('M1' in rule_name or 'M1' in candidate_layers):
            matched['M1'] = layer_map['M1']

        if 'UNKNOWN_DERIVED' in rule_name or any(u.startswith('DERIVED_') for u in unmatched_derived):
            return matched, False, 'Derived or unknown layer semantics without physical layer mapping'

        if not matched:
            return matched, False, 'No physical layers could be resolved from layer maps'

        return matched, True, ''
