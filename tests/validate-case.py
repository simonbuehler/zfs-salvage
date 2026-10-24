#!/usr/bin/env python3
"""Thin wrapper: the validation lives in scripts/validate_case.py (the doctor uses it too)."""
import os
import runpy
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts'))
from validate_case import validate  # noqa: E402,F401

if __name__ == '__main__':
    runpy.run_path(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'scripts', 'validate_case.py'),
                   run_name='__main__')
