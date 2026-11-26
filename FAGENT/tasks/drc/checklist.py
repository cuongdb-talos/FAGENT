"""Run-scoped DRC violation checklist creation, validation, and atomic persistence."""

# ==============================================================================
# SECTION 1: Standard Library Imports
# ==============================================================================
import copy
from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


# ==============================================================================
# SECTION 2: DRC Checklist Manager Class
# ==============================================================================
class Checklist:
    """Maintains per-violation DRC repair tasks, evidence, staleness, and digest-bound persistence."""

    VALID_STATES = ('pending', 'in_progress', 'pass', 'skipped', 'unresolved')
    SKIP_KEYWORDS = ('ignore', 'skip', 'exclude', 'omit')

    # --------------------------------------------------------------------------
    # Main Execution Entry Point
    # --------------------------------------------------------------------------
    def run(self, report: dict = None, proposed_tasks: list = None, goal: str = '', rule_evidence: dict | None = None, *, selected_rules: set[str] | None = None) -> 'Checklist':
        """Reconcile report violations against proposed_tasks or build deterministic report items."""
        if report is None:
            raise ValueError('DRC report must be provided')
        if proposed_tasks is not None and not isinstance(proposed_tasks, list):
            raise ValueError('proposed_tasks must be a list')

        expected_items = self._extract_report_items(report)
        if selected_rules is not None:
            expected_items = [item for item in expected_items if item['rule'] in selected_rules]
        if proposed_tasks is None:
            tasks = []
            for idx, item in enumerate(expected_items, start=1):
                if item['missing_geometry']:
                    status = 'unresolved'
                    reason = item.get('missing_reason', 'Missing geometry in report')
                    work = f'Investigate missing geometry for {item["rule"]}'
                elif selected_rules is None and self._skip_allowed(item['rule'], '', goal, rule_evidence):
                    status = 'skipped'
                    evidence = (rule_evidence or {}).get(item['rule'], {})
                    if evidence.get('density') and self.excludes_density(goal):
                        reason = f"Excluded by goal: density in {evidence['deck_file']}, lines {evidence['line_start']}-{evidence['line_end']}"
                    else:
                        reason = f'Excluded by goal: {goal}'
                    work = ''
                else:
                    status = 'pending'
                    reason = (rule_evidence or {}).get(item['rule'], {}).get('reason', '')
                    work = ''
                tasks.append({
                    'task_id': f'task-{idx}',
                    'rule': item['rule'],
                    'violation_id': item['violation_id'],
                    'occurrence': item['occurrence'],
                    'work': work,
                    'status': status,
                    'reason': reason,
                    'polygon_ids': [],
                    'evidence': [],
                    'stale': False,
                })
            self.tasks = tasks
            return self

        reconciled = self._reconcile(expected_items, proposed_tasks, goal)
        tasks = []
        for idx, item in enumerate(reconciled, start=1):
            tasks.append({
                'task_id': f'task-{idx}',
                'rule': item['rule'],
                'violation_id': item['violation_id'],
                'occurrence': item['occurrence'],
                'work': item['work'],
                'status': item['status'],
                'reason': item['reason'],
                'polygon_ids': list(item.get('polygon_ids') or []),
                'evidence': list(item.get('evidence') or []),
                'stale': False,
            })
        self.tasks = tasks
        return self

    # --------------------------------------------------------------------------
    # Initialization and Constructors
    # --------------------------------------------------------------------------
    def __init__(self, tasks: list = None):
        """Initialize checklist state."""
        self.tasks = copy.deepcopy(tasks) if tasks is not None else []

    def from_dict(cls, payload: dict) -> 'Checklist':
        """Reconstruct a Checklist instance from a serialized read() dict."""
        if not isinstance(payload, dict) or not isinstance(payload.get('tasks'), list):
            raise ValueError('Invalid checklist state payload')
        return cls(tasks=payload['tasks'])

    from_dict = classmethod(from_dict)

    # --------------------------------------------------------------------------
    # Read, Digest, and Summary Operations
    # --------------------------------------------------------------------------
    def digest(self) -> str:
        """Return deterministic SHA-256 digest of current checklist tasks."""
        return self._compute_digest(self.tasks)

    def _compute_digest(self, tasks: list) -> str:
        """Compute canonical SHA-256 digest for a task list."""
        canonical = json.dumps(tasks, sort_keys=True, separators=(',', ':'))
        return sha256(canonical.encode('utf-8')).hexdigest()

    def summary(self) -> dict:
        """Return counts by task status and stale count."""
        counts = {state: 0 for state in self.VALID_STATES}
        stale_count = 0
        for task in self.tasks:
            status = task.get('status')
            if status in counts:
                counts[status] += 1
            if task.get('stale'):
                stale_count += 1
        return {
            'total': len(self.tasks),
            'counts': counts,
            'stale': stale_count,
            'all_resolved': (
                counts['pending'] == 0
                and counts['in_progress'] == 0
                and counts['unresolved'] == 0
                and stale_count == 0
            ),
        }

    def read(self) -> dict:
        """Return a serializable snapshot of the checklist."""
        return {
            'tasks': copy.deepcopy(self.tasks),
            'summary': self.summary(),
            'digest': self.digest(),
        }

    def queue_ids(self) -> list[str]:
        """Return active task IDs in report order, excluding read-time skipped or unresolved items."""
        return [
            task['task_id']
            for task in self.tasks
            if task.get('status') in ('pending', 'in_progress')
        ]

    # --------------------------------------------------------------------------
    # Task Updates and Conservative Invalidation
    # --------------------------------------------------------------------------
    def update(
        self,
        task_id: str,
        status: str,
        work: str = None,
        reason: str = '',
        polygon_ids: list = None,
        evidence: list = None,
    ) -> dict:
        """Update an existing checklist task without deleting tasks or losing prior evidence."""
        if status not in self.VALID_STATES:
            raise ValueError(f'Invalid checklist status: {status}')
        target = next((task for task in self.tasks if task.get('task_id') == task_id), None)
        if target is None:
            raise ValueError(f'Unknown checklist task_id: {task_id}')
        if not isinstance(reason, str):
            raise ValueError('reason must be a string')

        merged_evidence = copy.deepcopy(target.get('evidence', []))
        if evidence is not None:
            if not isinstance(evidence, list) or any(not isinstance(item, dict) for item in evidence):
                raise ValueError('evidence must be a list of objects')
            merged_evidence.extend(copy.deepcopy(evidence))

        if status in ('skipped', 'unresolved') and not reason.strip():
            raise ValueError(f'Status {status!r} requires a non-empty reason')
        if status == 'pass' and not reason.strip() and not merged_evidence:
            raise ValueError('Status "pass" requires a reason or recorded evidence')

        if work is not None:
            if not isinstance(work, str) or not work.strip():
                raise ValueError('work must be a non-empty string')
            target['work'] = work
        if polygon_ids is not None:
            if not isinstance(polygon_ids, list) or any(not isinstance(pid, str) or not pid for pid in polygon_ids):
                raise ValueError('polygon_ids must be a list of non-empty strings')
            target['polygon_ids'] = list(polygon_ids)

        target['status'] = status
        target['reason'] = reason
        target['evidence'] = merged_evidence
        if status == 'pass':
            target['stale'] = False
        return self.read()

    def invalidate(self, cell_name: str, polygon_id: str) -> dict:
        """Conservatively mark passed tasks stale after a geometry edit."""
        cell_prefix = f'{cell_name}:'
        for task in self.tasks:
            if task.get('status') != 'pass':
                continue
            pids = task.get('polygon_ids') or []
            if not pids or polygon_id in pids or any(pid.startswith(cell_prefix) for pid in pids):
                task['stale'] = True
                task['status'] = 'pending'
                for item in task.get('evidence', []):
                    if isinstance(item, dict):
                        item['stale'] = True
                note = f'Invalidated after edit to {polygon_id} in {cell_name}'
                task['reason'] = f'{task["reason"]}; {note}' if task.get('reason') else note
        return self.read()

    # --------------------------------------------------------------------------
    # Atomic Persistence and Digest-Bound Restoration
    # --------------------------------------------------------------------------
    def save(self, path: Path) -> None:
        """Atomically persist checklist JSON with its integrity digest."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            'version': 1,
            'digest': self.digest(),
            'tasks': self.tasks,
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

    def restore(self, path: Path, expected_digest: str) -> 'Checklist':
        """Restore and verify checklist integrity against expected_digest."""
        source = Path(path)
        if not source.is_file():
            raise ValueError(f'Missing checklist file: {source}')
        try:
            payload = json.loads(source.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f'Corrupt checklist file: {source}') from exc
        if not isinstance(payload, dict) or payload.get('version') != 1:
            raise ValueError('Unsupported checklist format')
        tasks = payload.get('tasks')
        if not isinstance(tasks, list):
            raise ValueError('Checklist missing tasks list')
        computed = self._compute_digest(tasks)
        if payload.get('digest') != computed or not expected_digest or computed != expected_digest:
            raise ValueError('Checklist digest mismatch')
        self.tasks = copy.deepcopy(tasks)
        return self

    # --------------------------------------------------------------------------
    # Report Reconciliation and Goal Skip Validation
    # --------------------------------------------------------------------------
    def rule_summary(self, report: dict) -> list[dict]:
        """List violated rules with report descriptions and counts for scope selection."""
        if not isinstance(report, dict):
            raise ValueError('DRC report must be a dict')
        counts = {}
        for item in self._extract_report_items(report):
            rule = item['rule']
            counts[rule] = counts.get(rule, 0) + 1
        descriptions = {
            str(rule.get('name', '')).strip(): rule.get('description')
            for rule in report.get('rules') or []
        }
        return [
            {'rule': rule, 'description': descriptions.get(rule), 'violation_count': count}
            for rule, count in counts.items()
        ]

    def _extract_report_items(self, report: dict) -> list:
        """Extract all geometry occurrences and count-mismatch placeholders from a DRC report."""
        items = []
        rules = report.get('rules')
        if isinstance(rules, list):
            for rule in rules:
                name = str(rule.get('name', '')).strip()
                if not name:
                    continue
                geometries = rule.get('geometry') or []
                occ_counts = {}
                for geom in geometries:
                    vid = geom.get('id')
                    occ = occ_counts.get(vid, 0)
                    occ_counts[vid] = occ + 1
                    items.append({'rule': name, 'violation_id': vid, 'occurrence': occ, 'missing_geometry': False})
                expected = max(int(rule.get('total_count', 0) or 0), int(rule.get('reported_count', 0) or 0))
                if expected > len(geometries):
                    for missing_idx in range(expected - len(geometries)):
                        occ = occ_counts.get(None, 0)
                        occ_counts[None] = occ + 1
                        items.append({
                            'rule': name,
                            'violation_id': None,
                            'occurrence': occ,
                            'missing_geometry': True,
                            'missing_reason': f'Report count ({expected}) exceeds geometry records ({len(geometries)})',
                        })
            return items

        by_rule = report.get('violations_by_rule') or {}
        for name, count in by_rule.items():
            for occ in range(int(count or 0)):
                items.append({
                    'rule': str(name),
                    'violation_id': None,
                    'occurrence': occ,
                    'missing_geometry': True,
                    'missing_reason': 'Summary report contains counts without geometry records',
                })
        return items

    def _reconcile(self, expected_items: list, proposed_tasks: list, goal: str):
        """Match proposed tasks 1-to-1 against report items and preserve report order."""
        available_indices = list(range(len(expected_items)))
        matched_by_idx = {}
        for task in proposed_tasks:
            if not isinstance(task, dict):
                raise ValueError('Each proposed task must be an object')
            rule = task.get('rule')
            vid = task.get('violation_id')
            occ = task.get('occurrence')
            match_pos = None
            for pos, idx in enumerate(available_indices):
                exp = expected_items[idx]
                if exp['rule'] == rule and exp['violation_id'] == vid:
                    if occ is None or exp['occurrence'] == occ:
                        match_pos = pos
                        break
            if match_pos is None:
                raise ValueError(f'Proposed task does not match any remaining report occurrence: {rule} ({vid})')
            target_idx = available_indices.pop(match_pos)

            status = task.get('status', 'pending')
            reason = str(task.get('reason') or '')
            work = str(task.get('work') or f'Inspect {rule}')
            if expected_items[target_idx]['missing_geometry']:
                status = 'unresolved'
                reason = reason or expected_items[target_idx]['missing_reason']
            elif status == 'skipped':
                if not self._skip_allowed(rule, reason, goal):
                    raise ValueError(f'Skip not permitted by configured goal for rule {rule}')
            elif status not in ('pending', 'unresolved'):
                raise ValueError(f'Invalid initial task status {status!r} for rule {rule}')
            matched_by_idx[target_idx] = {
                'rule': expected_items[target_idx]['rule'],
                'violation_id': expected_items[target_idx]['violation_id'],
                'occurrence': expected_items[target_idx]['occurrence'],
                'work': work,
                'status': status,
                'reason': reason,
                'polygon_ids': list(task.get('polygon_ids') or []),
                'evidence': [],
            }

        reconciled = []
        for idx, expected in enumerate(expected_items):
            if idx in matched_by_idx:
                reconciled.append(matched_by_idx[idx])
            else:
                if expected['missing_geometry']:
                    status = 'unresolved'
                    reason = expected['missing_reason']
                    work = f'Investigate missing geometry for {expected["rule"]}'
                else:
                    status = 'unresolved'
                    reason = 'No task supplied by model for this reported violation'
                    work = f'Inspect {expected["rule"]}'
                reconciled.append({
                    'rule': expected['rule'],
                    'violation_id': expected['violation_id'],
                    'occurrence': expected['occurrence'],
                    'work': work,
                    'status': status,
                    'reason': reason,
                    'polygon_ids': [],
                    'evidence': [],
                })
        return reconciled

    def _find_match_index(self, remaining: list, rule: str, vid, occ):
        """Find matching expected item index by rule, violation_id, and optional occurrence."""
        for idx, item in enumerate(remaining):
            if item['rule'] != rule or item['violation_id'] != vid:
                continue
            if occ is None or item['occurrence'] == occ:
                return idx
        return None

    @classmethod
    def excludes_density(cls, goal: str) -> bool:
        if not isinstance(goal, str):
            return False
        verbs = '|'.join(cls.SKIP_KEYWORDS)
        return bool(re.search(
            rf'(?:^|[.!?;\n]|\bbut\b|\band\b)\s*(?:please\s+)?(?:{verbs})\s+'
            r'(?:the\s+)?(?:(?:drc\s+)?errors?\s+about\s+)?density\b',
            goal, re.IGNORECASE,
        ))

    def _skip_allowed(self, rule: str, reason: str, goal: str, rule_evidence: dict | None = None) -> bool:
        """Allow skipping only when configured goal authorizes the skipped category."""
        if not isinstance(goal, str) or not goal.strip():
            return False
        goal_lower = goal.lower()
        if not any(kw in goal_lower for kw in self.SKIP_KEYWORDS):
            return False
        exclusion_terms = set(re.findall(r'[a-z0-9_]+', goal_lower)) - set(self.SKIP_KEYWORDS) - {
            'all', 'drc', 'errors', 'error', 'about', 'only', 'the', 'but', 'and', 'or', 'fix', 'rules', 'rule', 'of', 'for',
        }
        if rule_evidence is not None and self.excludes_density(goal):
            if rule_evidence.get(rule, {}).get('density'):
                return True
            verbs = '|'.join(self.SKIP_KEYWORDS)
            clauses = re.split(r'[.!?;\n]|\b(?:but|then)\b', goal_lower)
            targets = (
                re.findall(r'[a-z0-9_]+', match[1])
                for clause in clauses
                if (match := re.search(rf'\b(?:{verbs})\s+(.+)', clause))
            )
            return rule.lower() != 'density' and any(rule.lower() in words for words in targets)
        target_text = f'{rule} {reason}'.lower().strip()
        return any(term in target_text for term in exclusion_terms if len(term) >= 3)
