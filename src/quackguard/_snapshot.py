"""Atomic publication of per-guard monitoring snapshots."""

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tempfile import NamedTemporaryFile

from .types import Event, Stats


def write_snapshot(path: Path, captured: datetime, stats: Stats, events: tuple[Event, ...] | None) -> None:
    document = {"schema_version": 1, "updated_at": captured.isoformat(), "stats": asdict(stats)}
    if events is not None:
        # Use an allowlist so future event fields require an explicit publication decision.
        document["events"] = [
            {"timestamp": e.timestamp.isoformat(), "stage": e.stage, "decision": e.decision,
             "reason_code": e.reason_code, "duration_ms": e.duration_ms, "tool_name": e.tool_name,
             "proposal_id": e.proposal_id, "execution": e.execution, "permission": e.permission,
             "alignment": e.alignment}
            for e in events
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(document, stream, indent=2, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
