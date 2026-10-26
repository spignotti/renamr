"""Entrypoint for one scheduled reconciliation tick.

The LaunchAgent invokes this module with an explicit private state directory.
Only sanitized counts are printed; document names, text, facts, prompts, and
environment values never reach service logs.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from renamr.core.errors import CoreError
from renamr.core.scheduler import TickResult, run_tick
from renamr.core.workspace import Workspace
from renamr.logging import setup_logging


def main() -> int:
    """Run one tick and print a sanitized summary."""
    parser = argparse.ArgumentParser(prog="renamr.service_worker")
    parser.add_argument("--state-dir", type=Path, required=True)
    arguments = parser.parse_args()
    state = arguments.state_dir.expanduser()
    try:
        workspace, lock = Workspace.open_locked(state_dir=state, blocking=False)
        if workspace is None or lock is None:
            print(json.dumps(TickResult(status="busy").model_dump(mode="json"), sort_keys=True))
            return 0
        try:
            setup_logging("INFO", False, state / "logs" / "service.log", False)
            result = run_tick(workspace, lock=lock)
        finally:
            lock.release()
    except CoreError as exc:
        print(json.dumps({"status": "error", "error": exc.category}))
        return 1
    except Exception as exc:
        print(json.dumps({"status": "error", "error": type(exc).__name__}))
        return 1
    print(json.dumps(result.model_dump(mode="json"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
