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
    def run(self, report: dict = None, proposed_tasks: list = None, goal: str = '') -> 'Checklist':
        """Reconcile report violations against proposed_tasks and build the initial checklist."""
        if report is None:
            raise ValueError('DRC report must be provided')
        if proposed_tasks is None:
            proposed_tasks = []
        if not isinstance(proposed_tasks, list):
            raise ValueError('proposed_tasks must be a list')

        expected_items = self._extract_report_items(report)
        matched, unresolved_missing = self._reconcile(expected_items, proposed_tasks, goal)
        tasks = []
        for idx, item in enumerate(matched + unresolved_missing, start=1):
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
        """Match proposed tasks 1-to-1 against report items and enforce skip/unresolved rules."""
        remaining = list(expected_items)
        matched = []
        for task in proposed_tasks:
            if not isinstance(task, dict):
                raise ValueError('Each proposed task must be an object')
            rule = task.get('rule')
            vid = task.get('violation_id')
            occ = task.get('occurrence')
            idx = self._find_match_index(remaining, rule, vid, occ)
            if idx is None:
                raise ValueError(f'Proposed task does not match any remaining report occurrence: {rule} ({vid})')
            expected = remaining.pop(idx)
            status = task.get('status', 'pending')
            reason = str(task.get('reason') or '')
            work = str(task.get('work') or f'Inspect {rule}')
            if expected['missing_geometry']:
                status = 'unresolved'
                reason = reason or expected['missing_reason']
            elif status == 'skipped':
                if not self._skip_allowed(rule, reason, goal):
                    raise ValueError(f'Skip not permitted by configured goal for rule {rule}')
            elif status not in ('pending', 'unresolved'):
                raise ValueError(f'Invalid initial plan status {status!r} for rule {rule}')
            matched.append({
                'rule': expected['rule'],
                'violation_id': expected['violation_id'],
                'occurrence': expected['occurrence'],
                'work': work,
                'status': status,
                'reason': reason,
                'polygon_ids': list(task.get('polygon_ids') or []),
                'evidence': [],
            })

        unresolved_missing = []
        for expected in remaining:
            if not expected['missing_geometry']:
                raise ValueError(f'Plan does not cover report violation {expected["rule"]} id={expected["violation_id"]}')
            unresolved_missing.append({
                'rule': expected['rule'],
                'violation_id': None,
                'occurrence': expected['occurrence'],
                'work': f'Investigate missing geometry for {expected["rule"]}',
                'status': 'unresolved',
                'reason': expected['missing_reason'],
                'polygon_ids': [],
                'evidence': [],
            })
        return matched, unresolved_missing

    def _find_match_index(self, remaining: list, rule: str, vid, occ):
        """Find matching expected item index by rule, violation_id, and optional occurrence."""
        for idx, item in enumerate(remaining):
            if item['rule'] != rule or item['violation_id'] != vid:
                continue
            if occ is None or item['occurrence'] == occ:
                return idx
        return None

    def _skip_allowed(self, rule: str, reason: str, goal: str) -> bool:
        """Allow skipping only when both reason and configured goal authorize the skipped category."""
        if not reason.strip() or not isinstance(goal, str) or not goal.strip():
            return False
        goal_lower = goal.lower()
        if not any(kw in goal_lower for kw in self.SKIP_KEYWORDS):
            return False
        exclusion_terms = set(re.findall(r'[a-z0-9_]+', goal_lower)) - set(self.SKIP_KEYWORDS) - {
            'all', 'drc', 'errors', 'error', 'about', 'only', 'the', 'but', 'and', 'or', 'fix', 'rules', 'rule', 'of', 'for',
        }
        target_text = f'{rule} {reason}'.lower()
        return any(term in target_text for term in exclusion_terms if len(term) >= 3)
