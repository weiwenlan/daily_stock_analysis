#!/usr/bin/env python3
"""Merge the first-pass stock report with a retry run (which overwrites reports/report_*.md)."""
import re
import sys
from pathlib import Path

first = Path(sys.argv[1]).read_text(encoding="utf-8")
target = sorted(Path(sys.argv[2]).glob("report_*.md"))[-1]
retry = target.read_text(encoding="utf-8") if target.exists() else ""


def blocks(text: str) -> dict[str, str]:
    out = {}
    for block in re.split(r"\n(?=## \S+ .+ \([\w.\-]+\)\s*$)", text, flags=re.M)[1:]:
        m = re.match(r"## \S+ .+? \(([\w.\-]+)\)", block)
        if m:
            out[m.group(1)] = block.rstrip() + "\n"
    return out


merged = blocks(first)
merged.update(blocks(retry))
head = first.split("\n## 📊")[0].rstrip()
target.write_text(head + "\n\n" + "\n".join(merged.values()), encoding="utf-8")
print(f"merged {len(merged)} stocks into {target}")
