#!/usr/bin/env python3
"""Summarize content-free rollup JSON logs from stdin (read-only, no DB/config)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.services.task_rollup_observability import rollup_event_lines, summarize_rollup_events


if __name__ == "__main__":
    print(json.dumps(summarize_rollup_events(rollup_event_lines(sys.stdin)), sort_keys=True))
