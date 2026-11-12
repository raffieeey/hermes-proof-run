"""Subprocess driver for crash/reopen recovery tests. Not collected."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main() -> None:
    here = Path(__file__).resolve()
    plugin_root = here.parents[2]
    fixtures = here.parent
    sys.path.insert(0, str(plugin_root))
    sys.path.insert(0, str(fixtures))
    from proof_run.engine import WorkflowRunner
    from proof_run.store import WorkflowStore
    from runtime_support import FileFollower, FileHost

    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--follower", default="")
    args = parser.parse_args()
    store = WorkflowStore(Path(args.root))
    host = FileHost(Path(args.state_dir), store)
    follower = FileFollower(Path(args.follower)) if args.follower else None
    runner = WorkflowRunner(store, host, follower)
    runner.drive(args.run_id)


if __name__ == "__main__":
    main()
