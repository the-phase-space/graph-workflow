"""The monitor component: live run state for humans and the GUI.

Writes ``status.json`` (atomic replace) in the run directory after every
state-changing event — the interchange surface a monitor GUI consumes — and
optionally narrates key events to the console. External processes command the
runtime through ``control.json`` in the same directory (see engine).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


class Monitor:
    def __init__(self, run_dir, console: bool = True):
        self.run_dir = Path(run_dir)
        self.status_path = self.run_dir / "status.json"
        self.console = console
        self._subscribers = []

    def subscribe(self, fn):
        """fn(event_record) is called after every trace event."""
        self._subscribers.append(fn)

    def emit(self, record: dict, snapshot: dict | None = None):
        if snapshot is not None:
            self.write_status(snapshot)
        for fn in self._subscribers:
            try:
                fn(record)
            except Exception:
                pass
        if self.console:
            line = self._format(record)
            if line:
                print(line, file=sys.stderr, flush=True)

    def write_status(self, snapshot: dict):
        snapshot = dict(snapshot)
        snapshot["ts"] = round(time.time(), 3)
        tmp = self.status_path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snapshot, fh, ensure_ascii=False, indent=1)
            for _ in range(3):
                try:
                    os.replace(tmp, self.status_path)
                    return
                except PermissionError:
                    # On Windows, sync clients and scanners briefly hold
                    # status.json open, denying the replace. The trace is
                    # authoritative; a skipped snapshot is harmless — the
                    # next event rewrites it.
                    time.sleep(0.03)
        except OSError:
            pass

    @staticmethod
    def _format(r: dict) -> str | None:
        e = r.get("event")
        if e == "run_start":
            return f"[gw] run {r.get('run_id')} seed={r.get('seed')}"
        if e == "generator_fire":
            return "[gw] generator fired"
        if e == "ready":
            return f"[gw] ready   {r.get('node')}"
        if e == "demoted":
            return f"[gw] demoted {r.get('node')}"
        if e == "start":
            inv = r.get("invocation")
            tag = " (resume)" if r.get("resumed") else ""
            return f"[gw] start   {r.get('node')}#{inv}{tag}"
        if e == "commit":
            return (
                f"[gw] commit  {r.get('node')}#{r.get('invocation')} "
                f"output_state={r.get('output_state')}"
                + (f" error={r.get('error')!r}" if r.get("error") else "")
            )
        if e == "warning":
            return f"[gw] warning: {r.get('message')}"
        if e in (
            "pause_requested",
            "node_interrupted",
            "node_pause_acked",
            "paused",
            "resume_requested",
            "resumed",
            "resumed_from_disk",
        ):
            node = r.get("node")
            return f"[gw] {e}" + (f" {node}" if node else "")
        if e == "run_end":
            return f"[gw] run over: {r.get('reason')}"
        return None
