#!/usr/bin/env python3
"""
render_report.py -- fills the HTML template with two kinds of content:

1. Non-judgment cells (token replacement, always safe):
   {{USD_TRY}}, {{EUR_TRY}}, {{FUNDING_COST}}, {{GENERATED_AT_LABEL}}

2. AI-generated judgment sections (block replacement, from
   data/YYYY-MM-DD-analysis.json produced by generate_analysis.py):
   the AI:TODAY_TAKE, AI:SUMMARY, AI:KEY_EVENTS, AI:INDUSTRY,
   AI:TRADE_IMPLICATIONS marker blocks in the template.

IMPORTANT: the template and the output are two DIFFERENT files.
templates/report-template.html is the hand-maintained source of truth
(kept in git, never edited by this script) -- it still has the literal
{{TOKEN}} placeholders and empty AI:...:START/END blocks. docs/index.html
is pure generated output, overwritten from the template on every run.

Previously this script read FROM docs/index.html and wrote back TO
docs/index.html. That meant every {{TOKEN}} got permanently baked in
after the first successful run (the literal "{{FUNDING_COST}}" string
is gone after being replaced once, so the next run has nothing left to
replace and the value freezes forever at whatever it was the first
time). Splitting template vs. output fixes that: every run starts fresh
from the never-touched template.

If templates/report-template.html doesn't exist yet, copy your current
docs/index.html there once, strip out any hand-added content that
shouldn't be regenerated (e.g. stray hardcoded archive entries), and
commit it. From that point on, only edit templates/report-template.html
by hand -- never docs/index.html directly, it will just get overwritten.

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

# 範本來源：固定不動的手工維護檔案，跟輸出檔案分開
TEMPLATE_PATH = ROOT / "templates" / "report-template.html"

# 輸出目的地：GitHub Pages 實際發布的檔案，每次執行都是全新產生，
# 不應該手動編輯這個檔案（改了也會在下次執行時被蓋掉）
OUT_PATH = ROOT / "docs" / "index.html"

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


def fmt_rate(rates: dict, code: str, fallback: str = "34.15") -> str:
    r = rates.get(code)
    if not r or r.get("per_unit_selling") is None:
        return fallback
    return f"{r['per_unit_selling']:.4f}"


def replace_block(html_text: str, marker: str, new_inner: str) -> str:
    """Replace everything between <!-- AI:{marker}:START --> and
    <!-- AI:{marker}:END -->. If marker not found, returns original text."""
    pattern = re.compile(
        rf"(<!-- AI:{marker}:START.*?-->)(.*?)(<!-- AI:{marker}:END.*?-->)",
        re.DOTALL,
    )
    if not pattern.search(html_text):
        return html_text
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
    return "\n".join(rows) if rows else '          <tr><td colspan="7">今日無資料</td></tr>'


def render_industry_items(items: list[dict]) -> str:
    # 依日期新到舊排序（date 是 YYYY-MM-DD 字串，字串排序結果跟日期排序
    # 一致）。缺日期的項目排到最後，不讓它們因為空字串排序跑到最前面。
    sorted_items = sorted(
        items,
        key=lambda it: it.get("date") or "0000-00-00",
        reverse=True,
    )
    lis = []
    for it in sorted_items[:5]:
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
            f"找不到範本檔案: {template_path}\n"
            f"這支腳本現在從固定的範本檔案讀取，不再讀取輸出檔案本身。\n"
            f"請把一份帶有 {{{{TOKEN}}}} 佔位符跟 <!-- AI:...:START/END --> "
            f"標記、且不含任何寫死假資料的乾淨版本存成 {template_path}，"
            f"commit 進 repo 一次即可（之後只手動改這份範本，不要改 "
            f"{out_path}，它每次執行都會被覆蓋）。"
        )

    html_text = template_path.read_text(encoding="utf-8")

    # 1. 非判斷欄位：數值解析與安全預設值
    rates = (payload.get("fx") or {}).get("rates", {})
    usd = fmt_rate(rates, "USD", fallback="34.15")
    eur = fmt_rate(rates, "EUR", fallback="37.80")

    funding = payload.get("macro", {}).get("funding_cost", [])
    if funding and isinstance(funding, list) and "value" in funding[-1]:
        funding_cost = f"{funding[-1]['value']:.1f}"
    else:
        funding_cost = "37.0"

    generated_label = payload.get("generated_at_label")
    if not generated_label:
        now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)
        generated_label = now.strftime("%Y-%m-%d · %H:%M TRT")

    replacements = {
        "{{USD_TRY}}": usd,
        "{{EUR_TRY}}": eur,
        "{{FUNDING_COST}}": funding_cost,
        "{{GENERATED_AT_LABEL}}": generated_label,
    }

    for token, value in replacements.items():
        html_text = html_text.replace(token, str(value))

    # 2. AI 判斷區塊：從 {date}-analysis.json 讀取並替換
    analysis_path = DATA_DIR / f"{resolved_date}-analysis.json"
    ai_status = "skipped (no analysis file)"
    if analysis_path.exists():
        analysis = load_json(analysis_path)

        today_take = analysis.get("today_take", "")
        summary = analysis.get("summary", "")

        if today_take:
            html_text = replace_block(
                html_text, "TODAY_TAKE",
                f'  <p class="assessment-text">\n    {today_take}\n  </p>'
            )
        if summary:
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
        ai_status = f"applied ({analysis_path.name})"

    # 3. 寫入輸出檔案（純產生物，永遠從範本重新產生，不會累積殘留內容）
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")

    print(f"-> 已從範本 {template_path} 重新產生輸出 {out_path}")
    print(f"   USD/TRY={usd}  EUR/TRY={eur}  隔夜融資成本={funding_cost}%")
    print(f"   更新時間標籤={generated_label}")
    print(f"   AI 區塊狀態：{ai_status}")


if __name__ == "__main__":
    main()
