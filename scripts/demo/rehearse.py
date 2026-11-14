"""Rehearse the 60-second demo: ``uv run python scripts/demo/rehearse.py [--live] [--no-model]``.

Thin wrapper around :mod:`eval.rehearse` (see its docstring and ``--help``). ``--live`` drives the running
brain server (the real Mac); the default replays the demo in-process against fixtures.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # `eval` is a repo package, not installed; make it importable from here
    sys.path.insert(0, str(ROOT))

from eval.rehearse import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
