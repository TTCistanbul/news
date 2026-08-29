#!/usr/bin/env python3
"""
render_report.py -- only fills in cells that need NO human judgment:
  - USD/TRY, EUR/TRY current rate (from fx.rates)
  - overnight funding cost (from macro.funding_cost, latest value)
  - the "generated at" timestamp shown in the top bar

Everything else in turkey-econ-brief.html (the 產業動態 industry-update
cards, 貿易意涵 analysis, 摘要, 關鍵事件與觀點 tables) is left completely
untouched -- those need a human to read the day's news and decide what
matters. This script only does dumb token replacement; it does not
invent analysis.

Usage:
    python3 render_report.py                 # uses today's data/*.json
    python3 render_report.py --date 2026-08-29
"""

import argparse
import datetime as dt
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
TEMPLATE_PATH = ROOT / "docs" / "turkey-econ-brief.html"
OUT_PATH = ROOT / "docs" / "turkey-econ-brief.html"


def load_payload(date_str: str | None) -> dict:
    if date_str:
        path = DATA_DIR / f"{date_str}.json"
    else:
        files = sorted(DATA_DIR.glob("*.json"))
        # _seen_ids.json is not a daily payload, skip it
        files = [f for f in files if re.match(r"\d{4}-\d{2}-\d{2}\.json$", f.name)]
        if not files:
            raise SystemExit("data/ 裡沒有找到任何日期格式的 JSON 檔案")
        path = files[-1]
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def fmt_rate(rates: dict, code: str) -> str:
    """Format a TRY rate as e.g. '48.16'. Uses forex_selling (賣出價，
    比較貼近進口商實際付款成本的參考值). Falls back to '資料缺漏' rather
    than silently showing a stale or fabricated number."""
    r = rates.get(code)
    if not r or r.get("per_unit_selling") is None:
        return "資料缺漏"
    return f"{r['per_unit_selling']:.4f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，預設用 data/ 裡最新的檔案")
    ap.add_argument("--template", default=str(TEMPLATE_PATH),
                     help="要套值的 HTML 範本路徑")
    ap.add_argument("--out", default=str(OUT_PATH),
                     help="輸出路徑")
    args = ap.parse_args()

    payload = load_payload(args.date)
    template_path = Path(args.template)
    out_path = Path(args.out)

    if not template_path.exists():
        raise SystemExit(
            f"找不到範本 {template_path}——這支腳本只填值，不會自己生出\n"
            f"整份 HTML，docs/turkey-econ-brief.html 要先手動放好帶有\n"
            f"{{{{TOKEN}}}} 標記的範本版本。"
        )
    html = template_path.read_text(encoding="utf-8")

    rates = (payload.get("fx") or {}).get("rates", {})
    usd = fmt_rate(rates, "USD")
    eur = fmt_rate(rates, "EUR")

    funding = payload.get("macro", {}).get("funding_cost", [])
    funding_cost = f"{funding[-1]['value']:.1f}" if funding else "資料缺漏"

    generated_label = payload.get("generated_at_label", "")

    replacements = {
        "{{USD_TRY}}": usd,
        "{{EUR_TRY}}": eur,
        "{{FUNDING_COST}}": funding_cost,
        "{{GENERATED_AT_LABEL}}": generated_label,
    }
    missing = []
    for token, value in replacements.items():
        if token not in html:
            missing.append(token)
        html = html.replace(token, value)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    print(f"-> 已套值輸出至 {out_path}")
    print(f"   USD/TRY={usd}  EUR/TRY={eur}  隔夜融資成本={funding_cost}%")
    if missing:
        print(f"   注意：範本裡找不到這些標記，代表沒有被替換到：{missing}")


if __name__ == "__main__":
    main()
