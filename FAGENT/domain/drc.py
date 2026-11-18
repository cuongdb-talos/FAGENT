"""Parse Calibre DRC results and summary files."""
import re
from pathlib import Path


class DrcParser:
    def run(self):
        return self

    @staticmethod
    def read_drc(path):
        content = Path(path).read_text(errors='replace')
        if 'CALIBRE::DRC' in content or '--- SUMMARY' in content or 'RULECHECK RESULTS STATISTICS' in content:
            return DrcParser.parse_drc_summary(path)
        data = DrcParser.parse_drc(path)
        data['clean'] = (data.get('total_violations', 0) == 0)
        return data

    @staticmethod
    def parse_drc_summary(path):
        lines = Path(path).read_text(errors='replace').splitlines()
        content = '\n'.join(lines)
        total_match = re.search(r'TOTAL DRC Results Generated:\s+(\d+)', content)
        if not total_match:
            raise ValueError('Summary missing total DRC results count')
        total_violations = int(total_match.group(1))

        data = {
            'source': str(Path(path).resolve()),
            'type': 'summary',
            'header': {},
            'violations_by_rule': {},
            'violations_by_cell': {},
            'total_violations': total_violations,
            'clean': (total_violations == 0),
        }

        for line in lines[:35]:
            if ':' in line:
                key, _, val = line.partition(':')
                key = key.strip()
                val = val.strip()
                if key in (
                    'Execution Date/Time',
                    'Calibre Version',
                    'Rule File Pathname',
                    'Layout System',
                    'Layout Primary Cell',
                    'DRC Results Database',
                ):
                    data['header'][key] = val

        data['top_cell'] = data['header'].get('Layout Primary Cell', '')

        rule_re = re.compile(r'^\s*RULECHECK\s+(\S+)\s*\.{3,}\s*TOTAL Result Count\s*=\s*(\d+)')
        cell_section = False
        current_cell = None

        for line in lines:
            if '--- RULECHECK RESULTS STATISTICS (BY CELL)' in line:
                cell_section = True
                continue
            if cell_section:
                if line.startswith('--- SUMMARY') or line.startswith('--------------------------------------------------'):
                    if current_cell:
                        break
                cell_match = re.match(r'^\s*CELL\s+(\S+)\s*\.{3,}\s*TOTAL Result Count\s*=\s*(\d+)', line)
                if cell_match:
                    current_cell = cell_match.group(1)
                    data['violations_by_cell'][current_cell] = {
                        'total_count': int(cell_match.group(2)),
                        'rules': {},
                    }
                    continue
                rule_cell_match = re.match(r'^\s*RULECHECK\s+(\S+)\s*\.{3,}\s*TOTAL Result Count\s*=\s*(\d+)', line)
                if rule_cell_match and current_cell:
                    r_name, r_cnt = rule_cell_match.group(1), int(rule_cell_match.group(2))
                    data['violations_by_cell'][current_cell]['rules'][r_name] = r_cnt
            else:
                m = rule_re.match(line)
                if m:
                    r_name, count = m.group(1), int(m.group(2))
                    if count > 0:
                        data['violations_by_rule'][r_name] = count

        return data

    @staticmethod
    def parse_drc(path):
        lines = Path(path).read_text(errors='replace').splitlines()
        if not lines or not re.fullmatch(r'\S+\s+\d+', lines[0].strip()):
            raise ValueError('Expected a Calibre ASCII results database header')
        top_cell, scale = lines[0].split()
        scale = int(scale)
        if scale <= 0:
            raise ValueError('Invalid results database coordinate scale')
        rules = []
        header = re.compile(r'^\s*(\d+)\s+(\d+)\s+(\d+)\s+\w+\s+\d+\s+')
        for i, line in enumerate(lines):
            match = header.match(line)
            if not match or i < 2:
                continue
            count, total, text_count = map(int, match.groups())
            end_text = i + 1 + text_count
            if end_text > len(lines):
                raise ValueError('Truncated rule description')
            rules.append({'name': lines[i - 1].strip(), 'reported_count': count,
                          'total_count': total, 'description': lines[i + 1:end_text],
                          'geometry': [], '_start': end_text})
        if not rules:
            raise ValueError('No Calibre rule headers found')
        for index, rule in enumerate(rules):
            end = rules[index + 1]['_start'] - 2 - len(rules[index + 1]['description']) if index + 1 < len(rules) else len(lines)
            pos = rule.pop('_start')
            while pos < end:
                match = re.fullmatch(r'\s*([pe])\s+(\d+)\s+(\d+)\s*', lines[pos])
                if not match:
                    pos += 1
                    continue
                kind, number, size = match.groups()
                pos += 1
                if pos < end and lines[pos].startswith('CN '):
                    pos += 1
                points = []
                for _ in range(int(size)):
                    if pos >= end or not re.fullmatch(r'\s*-?\d+\s+-?\d+\s*', lines[pos]):
                        raise ValueError(f'Truncated geometry in {rule["name"]}')
                    points.append([int(v) for v in lines[pos].split()])
                    pos += 1
                rule['geometry'].append({'type': kind, 'id': int(number), 'points_dbu': points})
            if len(rule['geometry']) != rule['reported_count']:
                raise ValueError(f'Unsupported or truncated geometry for {rule["name"]}')
        return {'source': str(Path(path).resolve()), 'top_cell': top_cell,
                'dbu_per_user_unit': scale, 'rules': rules,
                'total_violations': sum(rule['total_count'] for rule in rules)}
