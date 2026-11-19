"""Minimal package entry point for `python -m FAGENT`."""

# ==============================================================================
# SECTION 1: Package Execution Environment Setup
# ==============================================================================
from pathlib import Path
import sys

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = 'FAGENT'

# ==============================================================================
# SECTION 2: Application Entry Point Execution
# ==============================================================================
from .fagent import Fagent

if __name__ == '__main__':
    raise SystemExit(Fagent.main())
