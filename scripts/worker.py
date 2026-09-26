"""Canonical Coding Worker CLI entrypoint.

The implementation remains in lite.py for import compatibility with existing
integrations and tests. New documentation should invoke this file.
"""
from lite import main

if __name__ == "__main__":
    raise SystemExit(main())
