import json
import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_payload  # noqa: E402

SCRIPT = Path(__file__).parent / "build_payload.py"


def _row(code, advice, sentence="ok", success=True, stop=100.0):
    raw = {
        "success": success,
        "name": code,
        "operation_advice": advice,
        "sentiment_score": 40,
        "model_used": "deepseek/deepseek-v4-flash",
        "market_snapshot": {"date": "2026-11-27", "close": "10", "pct_chg": "0.80%"},
        "dashboard": {"core_conclusion": {"one_sentence": sentence, "position_advice": {"has_position": "持仓者建议：跌破 9 减仓"}}},
    }
    return code, json.dumps(raw, ensure_ascii=False), stop


def test_failed_and_missing_rows_are_reported_and_order_is_fixed(tmp_path):
    db = tmp_path / "a.db"
    con = sqlite3.connect(db)
    con.execute("create table analysis_history (id integer primary key, code text, raw_result text, stop_loss real, created_at text)")
    rows = [_row("AAA", "观望"), _row("BBB", "减仓"), _row("CCC", "减仓", success=False), _row("MARKET", "查看复盘")]
    con.executemany("insert into analysis_history (code, raw_result, stop_loss, created_at) values (?,?,?,'2026-11-28 00:10:00')", rows)
    con.execute("insert into analysis_history (code, raw_result, stop_loss, created_at) values ('DDD', '{}', null, '2026-11-01 00:00:00')")
    con.commit()
    out = tmp_path / "p.json"

    subprocess.run([sys.executable, str(SCRIPT), "--db", str(db), "--since", "2026-11-28 00:00:00",
                    "--stocks", "AAA,BBB,CCC,DDD", "--run-url", "https://x/y", "--out", str(out)], check=True)
    payload = json.loads(out.read_text())

    assert [s["symbol"] for s in payload["stocks"]] == ["BBB", "AAA"]
    assert payload["failed"] == ["CCC", "DDD"]
    assert payload["stocks"][0]["holderPlan"] == "跌破 9 减仓"
    assert payload["stocks"][0]["change"] == "+0.80%"


def test_market_labels_match_across_wordings():
    old = "> 一句话\n- **盘面信号**：49/100（震荡，需观察）\n### 四、明日交易计划\n- **仓位**:4-5成,不加杠杆。\n- **失效条件**:跌破7100。"
    new = "- **盘面信号**：57/100（偏暖，需观察）\n### 四、明日交易计划\n- **仓位区间**：4–5 成。理由是…\n- **触发失效条件**：回吐全部涨幅。"
    prose = (
        "- **操作建议**：控制仓位。\n### 四、明日交易计划\n结论：**均衡偏进攻**。建议仓位控制在**5-6成**，激进者不超过7成。"
        "触发失效条件：若标普500回落至7200下方，仓位应降至4-5成并转为防守。\n### 五、风险提示\n- 跌破失效条件：别的"
    )

    assert build_payload.market_fields(old)["marketPosition"] == "4-5成"
    assert build_payload.market_fields(new)["marketPosition"] == "4–5 成"
    assert build_payload.market_fields(new)["marketInvalidation"] == "回吐全部涨幅。"
    assert build_payload.market_fields(new)["marketSignal"] == "57/100 偏暖"
    assert build_payload.market_fields(prose)["marketPosition"] == "5-6成"
    assert build_payload.market_fields(prose)["marketInvalidation"] == "若标普500回落至7200下方，仓位应降至4-5成并转为防守。"
    assert build_payload.market_fields(old)["marketInvalidation"] == "跌破7100。"
    assert build_payload.market_fields("")["marketPosition"] == "—"
