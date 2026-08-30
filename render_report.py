#!/usr/bin/env python3
"""
render_report.py -- fills the HTML template with two kinds of content:

1. Non-judgment cells (token replacement, always safe):
   {{USD_TRY}}, {{EUR_TRY}}, {{FUNDING_COST}}, {{GENERATED_AT_LABEL}}

2. AI-generated judgment sections (block replacement, from
   data/YYYY-MM-DD-analysis.json produced by generate_analysis.py):
   the AI:TODAY_TAKE, AI:SUMMARY, AI:KEY_EVENTS, AI:INDUSTRY,
   AI:TRADE_IMPLICATIONS marker blocks in the template.

If the analysis JSON for the date doesn't exist, part 2 is skipped
and the template's existing content in those blocks is left as-is
(so a failed/skipped Gemini call doesn't blank the page -- it just
means today's page shows yesterday's analysis until the next
successful run).

Usage:
    python3 render_report.py                 # uses today's data/*.json
    python3 render_report.py --date 2026-08-29
"""

import argparse
import datetime as dt
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
TEMPLATE_PATH = ROOT / "docs" / "turkey-econ-brief.html"
OUT_PATH = ROOT / "docs" / "turkey-econ-brief.html"

DIRECTION_ICON = {"red": "🔴", "green": "🟢", "neutral": "⚪"}
DIRECTION_CLASS = {"red": "dir-red", "green": "dir-green", "neutral": "dir-neutral"}
SENTIMENT_CLASS = {"pos": "pos", "neg": "neg", "neu": "neu"}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_date_path(date_str: str | None) -> tuple[Path, str]:
    if date_str:
        path = DATA_DIR / f"{date_str}.json"
    else:
        files = sorted(DATA_DIR.glob("*.json"))
        files = [f for f in files if re.match(r"\d{4}-\d{2}-\d{2}\.json$", f.name)]
        if not files:
            raise SystemExit("data/ 裡沒有找到任何日期格式的 JSON 檔案")
        path = files[-1]
    if not path.exists():
        raise SystemExit(f"找不到 {path}")
    return path, path.stem


def fmt_rate(rates: dict, code: str) -> str:
    r = rates.get(code)
    if not r or r.get("per_unit_selling") is None:
        return "資料缺漏"
    return f"{r['per_unit_selling']:.4f}"


def replace_block(html_text: str, marker: str, new_inner: str) -> str:
    """Replace everything between <!-- AI:{marker}:START --> and
    <!-- AI:{marker}:END --> (markers kept, only the content between
    them is swapped). Raises if the marker pair isn't found exactly
    once, so a template edit that breaks the markers fails loudly
    instead of silently leaving stale content."""
    pattern = re.compile(
        rf"(<!-- AI:{marker}:START.*?-->)(.*?)(<!-- AI:{marker}:END.*?-->)",
        re.DOTALL,
    )
    matches = list(pattern.finditer(html_text))
    if len(matches) != 1:
        raise SystemExit(
            f"範本裡 AI:{marker} 標記找到 {len(matches)} 組，預期剛好 1 組——"
            f"檢查 docs/turkey-econ-brief.html 裡的 <!-- AI:{marker}:START/END --> 有沒有被改壞。"
        )
    return pattern.sub(lambda m: m.group(1) + "\n" + new_inner + "\n" + m.group(3), html_text)


def render_key_events(events: list[dict]) -> str:
    rows = []
    for i, e in enumerate(events[:5], 1):
        direction = e.get("direction", "neutral")
        icon = DIRECTION_ICON.get(direction, "⚪")
        cls = DIRECTION_CLASS.get(direction, "dir-neutral")
        importance = html.escape(e.get("importance", "中等"))
        badge_cls = "badge-major" if importance == "重大" else "badge-medium"
        source_name = html.escape(e.get("source_name", ""))
        source_url = html.escape(e.get("source_url", "") or "#", quote=True)
        headline = html.escape(e.get("headline", ""))
        summary = html.escape(e.get("summary", ""))
        impact = html.escape(e.get("business_impact", ""))
        rows.append(f'''          <tr>
            <td>{i}</td>
            <td class="{cls}">{icon}</td>
            <td><span class="badge {badge_cls}">{importance}</span></td>
            <td><a href="{source_url}" target="_blank" rel="noopener noreferrer">{source_name}</a></td>
            <td>{headline}</td>
            <td>{summary}</td>
            <td>{impact}</td>
          </tr>''')
    return "\n".join(rows) if rows else "          <tr><td colspan=\"7\">今日無資料</td></tr>"


def render_industry_items(items: list[dict]) -> str:
    lis = []
    for it in items[:5]:
        sentiment = SENTIMENT_CLASS.get(it.get("sentiment", "neu"), "neu")
        sector = html.escape(it.get("sector", ""))
        headline = html.escape(it.get("headline", ""))
        source = html.escape(it.get("source", ""))
        date = html.escape(it.get("date", ""))
        body = html.escape(it.get("body", ""))
        interp = html.escape(it.get("business_interpretation", ""))
        lis.append(f'''      <li class="sector-item {sentiment}">
        <div class="row-top">
          <span class="sector-chip">{sector}</span>
          <span class="headline">{headline}</span>
          <span class="src">{source}</span>
          <span class="date">{date}</span>
        </div>
        <div class="body">{body}</div>
        <div class="impact"><b>台商解讀：</b>{interp}</div>
      </li>''')
    return "\n".join(lis) if lis else "      <li>今日無資料</li>"


def render_trade_implications(items: list[dict]) -> str:
    lis = []
    for it in items[:3]:
        title = html.escape(it.get("title", ""))
        body = html.escape(it.get("body", ""))
        lis.append(f"      <li><strong>{title}</strong>{body}</li>")
    return "\n".join(lis) if lis else "      <li>今日無資料</li>"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，預設用 data/ 裡最新的檔案")
    ap.add_argument("--template", default=str(TEMPLATE_PATH))
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args()

    data_path, resolved_date = resolve_date_path(args.date)
    payload = load_json(data_path)
    template_path = Path(args.template)
    out_path = Path(args.out)

    if not template_path.exists():
        raise SystemExit(
            f"找不到範本 {template_path}——docs/turkey-econ-brief.html 要先手動\n"
            f"放好帶有 {{{{TOKEN}}}} 跟 <!-- AI:...:START/END --> 標記的範本版本。"
        )
    html_text = template_path.read_text(encoding="utf-8")

    # 1. 非判斷欄位：token 替換
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
    missing_tokens = []
    for token, value in replacements.items():
        if token not in html_text:
            missing_tokens.append(token)
        html_text = html_text.replace(token, value)

    # 2. AI 判斷區塊：從 {date}-analysis.json 讀，沒有就跳過（保留範本裡舊內容）
    analysis_path = DATA_DIR / f"{resolved_date}-analysis.json"
    ai_status = "skipped (no analysis file)"
    if analysis_path.exists():
        analysis = load_json(analysis_path)

        today_take = analysis.get("today_take", "")
        summary = analysis.get("summary", "")
        html_text = replace_block(
            html_text, "TODAY_TAKE",
            f'  <p class="assessment-text">\n    {today_take}\n  </p>'
        )
        html_text = replace_block(
            html_text, "SUMMARY",
            f'    <p class="summary-text">\n {summary}\n    </p>'
        )
        html_text = replace_block(
            html_text, "KEY_EVENTS",
            render_key_events(analysis.get("key_events", []))
        )
        html_text = replace_block(
            html_text, "INDUSTRY",
            render_industry_items(analysis.get("industry_items", []))
        )
        html_text = replace_block(
            html_text, "TRADE_IMPLICATIONS",
            render_trade_implications(analysis.get("trade_implications", []))
        )
        ai_status = f"applied ({analysis_path.name}, model={analysis.get('_model', '?')})"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")

    print(f"-> 已套值輸出至 {out_path}")
    print(f"   USD/TRY={usd}  EUR/TRY={eur}  隔夜融資成本={funding_cost}%")
    print(f"   AI 區塊：{ai_status}")
    if missing_tokens:
        print(f"   注意：範本裡找不到這些 token，代表沒有被替換到：{missing_tokens}")


if __name__ == "__main__":
    main()
