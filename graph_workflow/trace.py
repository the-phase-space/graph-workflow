"""The trace: the global, disk-persisted record of all node-state changes.

One JSON object per line, appended and flushed at every event, updated at
every commit — the substrate for fault tolerance and pause/resume. Every
record carries a monotonically increasing ``seq`` and a wall-clock ``ts``
(``ts`` is informational; replay depends only on event order and content).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .errors import TraceReplayError
from .model import UNDEFINED


def json_safe(value, _depth=0):
    """Best-effort conversion of arbitrary output values for the trace."""
    if _depth > 8:
        return repr(value)
    if value is UNDEFINED:
        return {"__undefined__": True}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [json_safe(v, _depth + 1) for v in value]
    if isinstance(value, dict):
        return {str(k): json_safe(v, _depth + 1) for k, v in value.items()}
    return repr(value)


def json_restore(value):
    """Inverse of ``json_safe`` for the UNDEFINED marker (used by replay)."""
    if isinstance(value, dict):
        if value.get("__undefined__") is True and len(value) == 1:
            return UNDEFINED
        return {k: json_restore(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_restore(v) for v in value]
    return value


class TraceWriter:
    def __init__(self, path, append: bool = False, next_seq: int = 0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a" if append else "w", encoding="utf-8")
        self._seq = next_seq

    def write(self, event: str, **payload) -> int:
        """Append one event; returns its seq."""
        seq = self._seq
        self._seq += 1
        record = {"seq": seq, "ts": round(time.time(), 3), "event": event}
        record.update({k: json_safe(v) for k, v in payload.items()})
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()
        return seq

    @property
    def next_seq(self) -> int:
        return self._seq

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def read_events(path) -> list[dict]:
    """Read a trace file back into a list of event records."""
    path = Path(path)
    if not path.exists():
        raise TraceReplayError(f"trace file not found: {path}")
    events = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                # A torn final line is expected after a crash; anything torn
                # mid-file is not.
                remainder = fh.read().strip()
                if remainder:
                    raise TraceReplayError(f"corrupt trace record at line {i + 1} of {path}")
                break
    if not events:
        raise TraceReplayError(f"trace file is empty: {path}")
    return events
