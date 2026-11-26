#!/usr/bin/env python3
"""Condense DSA reports into a short email payload for send.py: {"subject","html","text"}."""
import json
import re
import sys
from pathlib import Path

import markdown
from premailer import transform

reports = Path(sys.argv[1] if len(sys.argv) > 1 else "reports")
out = Path(sys.argv[2] if len(sys.argv) > 2 else "/tmp/dsa_email.json")
run_url = sys.argv[3] if len(sys.argv) > 3 else ""

stock = sorted(reports.glob("report_*.md"))[-1]
raw_date = re.search(r"(\d{8})", stock.name).group(1)
date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
s = stock.read_text(encoding="utf-8")


def section(text: str, start: str, end: str) -> str:
    i = text.find(start)
    if i < 0:
        return ""
    j = text.find(end, i + len(start))
    return text[i + len(start): j if j > 0 else None].strip()


def cell(value: str | None) -> str:
    return value.replace("|", "/").strip() if value else "—"


lines = [f"# 美股盘前分析 {date}", ""]
verdicts = re.findall(r"### 📌 核心结论\s+\*\*\S+ ([^*]+)\*\*", s)
if verdicts:
    tally = {v.strip(): verdicts.count(v) for v in dict.fromkeys(verdicts)}
    lines += ["**" + f"{len(verdicts)} 只：" + " · ".join(f"{k} {n}" for k, n in tally.items()) + "**", ""]

review = sorted(reports.glob("market_review_*.md"))
if review:
    r = review[-1].read_text(encoding="utf-8")
    quote = re.search(r"^> (.+)$", r, re.M)
    plan = section(r, "### 四、明日交易计划", "### ")
    lines += ["## 大盘", ""]
    if quote:
        lines += [quote.group(1).strip(), ""]
    if plan:
        lines += [plan, ""]

lines += ["## 个股", "", "| 股票 | 结论 | 一句话 | 持仓者怎么做 |", "|---|---|---|---|"]
for block in re.split(r"\n## (?=\S+ .+ \([\w.\-]+\)\s*$)", s, flags=re.M)[1:]:
    m = re.match(r"\S+ (.+?) \((\w[\w.\-]*)\)", block.splitlines()[0])
    if not m:
        continue
    verdict = re.search(r"\*\*(\S+ [^*]+)\*\* \| (\S+)", block)
    one = re.search(r"一句话决策\*\*: (.+)", block)
    hold = re.search(r"💼 \*\*持仓者\*\* \| (.+?) \|", block)
    lines.append(
        f"| **{m.group(2)}** "
        f"| {cell(verdict.group(1) + ' · ' + verdict.group(2) if verdict else None)} "
        f"| {cell(one.group(1) if one else None)} "
        f"| {cell(re.sub(r'^持仓者：', '', hold.group(1)) if hold else None)} |"
    )
lines += ["", "数字来自免费数据源，下单前核对价位。不构成投资建议。"]
if run_url:
    lines.append(f"完整报告：[GitHub Actions run]({run_url})（Artifacts）")

text = "\n".join(lines)
body = markdown.markdown(text, extensions=["tables"])
html = f"""<html><head><style>
body{{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;font-size:14px;color:#1f2328;max-width:760px}}
h1{{font-size:20px}} h2{{font-size:16px;border-bottom:1px solid #d0d7de;padding-bottom:4px}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #d0d7de;padding:6px 8px;vertical-align:top;text-align:left}}
th{{background:#f6f8fa}}
</style></head><body>{body}</body></html>"""
out.write_text(json.dumps({
    "subject": f"美股盘前分析 {date}",
    "html": transform(html, keep_style_tags=False, disable_validation=True),
    "text": text,
}, ensure_ascii=False), encoding="utf-8")
print(out)
