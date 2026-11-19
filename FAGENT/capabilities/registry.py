"""Workflow actions implemented by the engine."""
from types import MappingProxyType


ACTIONS = MappingProxyType({
    'read_drc': 'drc',
    'repair_drc': 'drc',
    'validate_drc': 'drc',
    'read_lvs': 'lvs',
    'repair_lvs': 'lvs',
    'validate_lvs': 'lvs',
    'review': None,
})
