#!/usr/bin/env python3
"""Build the `stock-premarket` email payload from DSA's analysis_history table.

Reads structured fields (advice, score, stop loss, snapshot) instead of parsing
the LLM-written markdown, so the email shape does not drift with model output.
The payload schema lives in automation-email/src/emails/StockPremarket.tsx.
"""
import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ACTION_ORDER = ["卖出", "减仓", "观望", "持有", "加仓", "买入"]
DASH = "—"


def normalize_action(advice: str) -> str:
    advice = advice or ""
    for key, action in (("卖", "卖出"), ("减", "减仓"), ("加仓", "加仓"), ("买", "买入"), ("持有", "持有")):
        if key in advice:
            return action
    return "观望"


def fmt_price(value) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return DASH
    return f"{number:.2f}" if number > 0 else DASH


def fmt_change(snapshot: dict, change_pct) -> str:
    raw = str(snapshot.get("pct_chg") or "").strip()
    if re.fullmatch(r"[-+]?\d+(\.\d+)?%", raw):
        return raw if raw.startswith(("-", "+")) else f"+{raw}"
    try:
        return f"{float(change_pct):+.1f}%"
    except (TypeError, ValueError):
        return DASH


def clean(text, prefix: str = "") -> str:
    text = " ".join(str(text or "").split())
    if prefix:
        text = re.sub(rf"^{prefix}(建议|操作)?\s*[：:]\s*", "", text)
    return text or DASH


def market_fields(markdown: str) -> dict:
    """Pull the market view out of the LLM-written review.

    The model changes wording and layout every day: bold bullet labels
    ("**仓位**:" / "**仓位区间**："), or one prose paragraph ("建议仓位控制在5-6成").
    So match on the content shape inside the trading-plan section, not on labels.
    """
    text = markdown.replace("**", "")
    plan_start = text.find("交易计划")
    plan = text[plan_start:] if plan_start >= 0 else text
    next_section = plan.find("###", 1)
    plan = plan[:next_section] if next_section > 0 else plan

    signal = re.search(r"盘面信号\s*[：:]\s*(\d+/100)（([^，）]+)", text)
    summary = re.search(r"^>\s*(.+)$", markdown, re.M)
    position = re.search(r"仓位[^。\n\d]{0,12}?(\d+(?:\.\d+)?\s*[-–~至]\s*\d+(?:\.\d+)?\s*成)", plan) or re.search(
        r"(\d+(?:\.\d+)?\s*[-–~至]\s*\d+(?:\.\d+)?\s*成)", plan
    )
    invalidation = re.search(r"失效条件\s*[：:]\s*([^。\n]+。?)", plan)
    return {
        "marketSignal": f"{signal.group(1)} {signal.group(2)}" if signal else DASH,
        "marketSummary": clean(summary.group(1)) if summary else DASH,
        "marketPosition": clean(position.group(1)) if position else DASH,
        "marketInvalidation": clean(invalidation.group(1)) if invalidation else DASH,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--since", required=True, help="UTC timestamp; rows created before it are ignored")
    ap.add_argument("--stocks", required=True, help="comma-separated watchlist, defines row order")
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    watchlist = [s.strip().upper() for s in args.stocks.split(",") if s.strip()]
    con = sqlite3.connect(args.db)
    rows = con.execute(
        "select code, raw_result, stop_loss from analysis_history where created_at >= ? order by id",
        (args.since,),
    ).fetchall()
    latest = {code.upper(): (json.loads(raw or "{}"), stop) for code, raw, stop in rows}

    market_raw, _ = latest.pop("MARKET", ({}, None))
    stocks, failed, close_dates, models = [], [], [], []
    for symbol in watchlist:
        data, stop_loss = latest.get(symbol, ({}, None))
        dashboard = data.get("dashboard") or {}
        core = dashboard.get("core_conclusion") or {}
        snapshot = data.get("market_snapshot") or {}
        if not data.get("success", False) or not data.get("operation_advice") or not core.get("one_sentence"):
            failed.append(symbol)
            continue
        if snapshot.get("date"):
            close_dates.append(str(snapshot["date"]))
        if data.get("model_used"):
            models.append(str(data["model_used"]).split("/")[-1])
        stocks.append({
            "symbol": symbol,
            "name": clean(data.get("name")),
            "action": normalize_action(data.get("operation_advice")),
            "score": str(int(data.get("sentiment_score") or 0)),
            "close": fmt_price(snapshot.get("close") or data.get("current_price")),
            "change": fmt_change(snapshot, data.get("change_pct")),
            "stopLoss": fmt_price(stop_loss),
            "oneLine": clean(core.get("one_sentence")),
            "holderPlan": clean((core.get("position_advice") or {}).get("has_position"), "持仓者"),
        })

    if not stocks:
        print(f"no usable analysis rows since {args.since}; failed: {failed}", file=sys.stderr)
        return 1

    order = {s: i for i, s in enumerate(watchlist)}
    stocks.sort(key=lambda s: (ACTION_ORDER.index(s["action"]), order[s["symbol"]]))
    payload = {
        "date": datetime.now(ZoneInfo("America/New_York")).date().isoformat(),
        "closeDate": max(close_dates) if close_dates else DASH,
        **market_fields(market_raw.get("news_summary") or ""),
        "stocks": stocks,
        "failed": failed,
        "reportUrl": args.run_url,
        "model": max(set(models), key=models.count) if models else DASH,
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"{args.out}: {len(stocks)} stocks, {len(failed)} failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
