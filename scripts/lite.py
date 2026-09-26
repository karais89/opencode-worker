"""Backward-compatible entrypoint for the renamed Coding Worker controller."""
from worker import *  # noqa: F401,F403
from worker import main

if __name__ == "__main__":
    raise SystemExit(main())
