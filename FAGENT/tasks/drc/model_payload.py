"""Build the model input for one DRC marker."""

from collections import defaultdict

from shapely.geometry import Polygon

from FAGENT.tasks.drc.rule_evidence import cover_requirements


def build_request(state: dict, node_prompt: str, services, focused: dict | None) -> dict:
    """Project focused DRC evidence onto the repair prompt's input contract."""
    focused = focused or {}
    drc = focused.get('drc', {})
    evidence = drc.get('rule_evidence', {})
    layout = focused.get('layout', {})
    layer_map = {name: [item['layer'], item['datatype']]
                 for name, item in evidence.get('layer_mapping', {}).items()}
    names = defaultdict(list)
    for name, lpp in layer_map.items():
        names[tuple(lpp)].append(name)

    marker = drc.get('marker_user') or []
    bounds = _bounds(marker)
    violation = {
        'rule': focused.get('task', {}).get('rule'),
        'description': drc.get('rule_description'),
        'rule_text': evidence.get('rule_text'),
        'marker_bbox': bounds,
        'marker_metrics': _metrics(marker, bounds),
        'marker_points': marker,
        'evidence': {'status': drc.get('status', 'unavailable'),
                     'reason': drc.get('reason') or evidence.get('reason'),
                     'deck_file': evidence.get('deck_file'), 'line_start': evidence.get('line_start'),
                     'line_end': evidence.get('line_end'),
                     'mapping_diagnostics': evidence.get('mapping_diagnostics', {})},
    }
    raw_polygons = layout.get('polygons', [])
    coverage = cover_requirements(evidence, layout, raw_polygons, getattr(services, 'layout', None))
    if coverage is not None:
        violation['coverage'] = coverage
    polygons = [_polygon(item, names) for item in raw_polygons]
    present_lpps = {tuple(polygon['lpp']) for polygon in polygons}
    layer_status = {}
    for name, lpp in layer_map.items():
        if tuple(lpp) in present_lpps:
            status = 'present'
        elif layout.get('complete') and (layout.get('search_region') or {}).get('bounds_user'):
            status = 'empty'
        else:
            status = 'unknown'
        layer_status[name] = status
    diagnostics = evidence.get('mapping_diagnostics') or {}
    for name in diagnostics.get('ambiguous_layers', {}):
        layer_status[name] = 'unknown'
    cfg = state.get('drc_context') or {}
    policy = services.build_system_message(goal=cfg.get('goal'), constraints=(cfg.get('knowledge') or {}).get('constraints'))
    library = getattr(getattr(services, 'layout', None), 'library', None)
    unit = getattr(library, 'unit', None)
    precision = getattr(library, 'precision', None)
    review = state.get('review') or {}
    review_context = review.get('context') or {}
    review_input = ({'review_feedback': review['feedback'], 'review_context': review_context}
                    if review.get('decision') == 'continue'
                    and review_context.get('task_id') == state.get('current_task_id') else {})
    return {
        'system': '\n\n'.join(part for part in (node_prompt, policy) if part),
        'user': {
            'task_id': state.get('current_task_id'),
            **review_input,
            'cell': layout.get('cell'),
            'unit': 'um' if unit == 1e-6 else (f'{unit:g} m' if unit else None),
            'grid': precision / unit if unit and precision else None,
            'violation': violation,
            'layer_map': layer_map,
            'layout': {
                'search_region': (layout.get('search_region') or {}).get('bounds_user'),
                'complete': layout.get('complete', False),
                'candidate_digest': layout.get('candidate_digest'),
                'polygons': polygons,
                'layer_status_in_search_region': layer_status,
                'references_in_region': layout.get('references_in_region', []),
                'paths_in_region': layout.get('paths_in_region', []),
                'reason': layout.get('reason'),
            },
            'task_history': [],
        },
    }


def _bounds(points):
    if not points:
        return None
    xs, ys = zip(*points)
    return [min(xs), min(ys), max(xs), max(ys)]


def _metrics(points, bounds):
    if bounds is None:
        return {}
    x0, y0, x1, y1 = bounds
    shape = Polygon(points) if len(points) >= 3 else None
    area = shape.area if shape is not None and shape.is_valid else None
    return {'w': x1 - x0, 'h': y1 - y0, 'area': area}


def _polygon(item, names):
    lpp = [item['layer'], item['datatype']]
    aliases = names[tuple(lpp)]
    points = item['points']
    bounds = _bounds(points)
    metrics = _metrics(points, bounds)
    return {'id': item['id'], 'layer': aliases[0] if len(aliases) == 1 else None,
            'lpp': lpp, 'points': points, 'bbox': bounds, 'editable': item['editable'],
            'is_violating': None, **{key: metrics[key] for key in ('w', 'h', 'area')}}
