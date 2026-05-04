#!/usr/bin/env python3
"""Compatibility entrypoint for the Codex usage tracker CLI."""

from codex_usage_tracker.core import *  # noqa: F401,F403
from codex_usage_tracker.core import main


if __name__ == "__main__":
    raise SystemExit(main())
