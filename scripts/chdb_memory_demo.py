#!/usr/bin/env python3
"""Demo: chDB-native agent memory with time-travel (the blog's Pillar 1).

Shows the memory lifecycle the blog describes — remember -> recall -> conflict ->
revise — kept as append-only history, then queries the *past* of a belief
("what did the agent believe before I corrected it?"). On AWS Lambda MicroVMs this
table lives on the VM's persistent disk, so it survives suspend/resume — the
agent's brain is frozen and thawed with its full revision history intact.

Usage:
  python scripts/chdb_memory_demo.py                 # uses a throwaway local store
  python scripts/chdb_memory_demo.py --path ./store  # persist (e.g. to prove suspend/resume)
"""
from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root on path
from chdb_memory import MemoryStore  # noqa: E402


def _show(title: str, beliefs) -> None:
    print(f"\n{title}")
    for b in beliefs:
        print(f"  [{b.key}] v{b.version}: {b.content}")


def main() -> int:
    ap = argparse.ArgumentParser(description="chDB memory time-travel demo")
    ap.add_argument("--path", default=None, help="chDB store dir (default: temp)")
    args = ap.parse_args()
    path = args.path or str(Path(tempfile.mkdtemp(prefix="chdb_mem_")) / "store")

    mem = MemoryStore(path=path)
    print(f"chDB memory store: {path}")

    # 1. remember — the agent records explicit beliefs as it works.
    mem.remember("build-tool", "this repo uses Poetry")
    mem.remember("test-cmd", "run tests with `pytest -q`")
    mem.remember("py-version", "targets Python 3.11")
    _show("after remembering 3 beliefs — recall (current state):", mem.recall())

    # 2. conflict + revise — new information supersedes an old belief.
    print("\n>>> user corrects the agent: 'we migrated to uv, and we're on 3.13 now'")
    mem.revise("build-tool", "this repo standardizes on uv")
    mem.revise("py-version", "targets Python 3.13")
    _show("after revision — recall (current state):", mem.recall())

    # 3. history — the evolution is preserved, not overwritten.
    _show("full history of 'build-tool' (append-only revisions):", mem.history("build-tool"))

    # 4. time travel — what did the agent believe at version 3 (before corrections)?
    _show("TIME TRAVEL — beliefs as of version 3 (before the corrections):", mem.as_of(3))

    # 5. forget — soft-delete keeps the audit trail.
    mem.forget("test-cmd")
    _show("after forgetting 'test-cmd' — recall (it's gone from current state):", mem.recall())
    _show("...but its history (incl. the deletion) is still auditable:", mem.history("test-cmd"))

    print(
        "\nOn Lambda MicroVMs this table is on the VM's persistent disk — "
        "suspend the MicroVM, resume hours later, and every belief + its full\n"
        "revision history is exactly as left. Memory, observability, and history "
        "on one local query surface (chDB)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
