"""The Apex's programs (program.xml) as dated versions, and the difference between two versions, line by line.

The worker reads program.xml once a day and stores a new version only when something changed; the pages and the MCP
tools show the versions and what changed. This project never writes the programs: they are edited in Fusion
or on the Apex's own pages."""

from __future__ import annotations

import difflib
import hashlib
import json
from typing import Any

Programs = dict[str, list[str]]  # output name -> its program lines


def digest(programs: Programs) -> str:
    return hashlib.sha256(json.dumps(programs, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def diff(before: Programs | None, after: Programs) -> list[dict[str, Any]]:
    """Per output that changed: {output, change: added | removed | changed, lines: [[op, text]]}, op "+", "-" or " "
    (an unchanged line kept for context). Outputs in name order; nothing for a first version (before is None)."""
    if before is None:
        return []
    out: list[dict[str, Any]] = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if old is None:
            out.append({"output": name, "change": "added", "lines": [["+", line] for line in new or []]})
        elif new is None:
            out.append({"output": name, "change": "removed", "lines": [["-", line] for line in old]})
        else:
            lines: list[list[str]] = []
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
                if tag == "equal":
                    lines.extend([" ", line] for line in old[i1:i2])
                    continue
                lines.extend(["-", line] for line in old[i1:i2])
                lines.extend(["+", line] for line in new[j1:j2])
            out.append({"output": name, "change": "changed", "lines": lines})
    return out


def summary(changes: list[dict[str, Any]]) -> str:
    """One line for the events list: "uv_3_7 changed (+1 -1), Topoff_9_2 added"."""
    parts = []
    for c in changes:
        if c["change"] != "changed":
            parts.append(f"{c['output']} {c['change']}")
            continue
        plus = sum(1 for op, _ in c["lines"] if op == "+")
        minus = sum(1 for op, _ in c["lines"] if op == "-")
        parts.append(f"{c['output']} changed (+{plus} -{minus})")
    return ", ".join(parts) or "no change"
