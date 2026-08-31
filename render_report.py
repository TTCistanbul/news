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


def render_industry_items(items: list[dict], as_of: dt.date, window_days: int = 7) -> str:
    # 只保留「發布日期」落在 as_of 往前推 window_days 天內的項目（含 as_of
    # 當天），超過範圍的直接丟棄，不管 Gemini 排多前面。用 as_of（這份
    # 報告對應的日期，即 resolved_date）而不是系統當下時間去算，這樣用
    # --date 補跑舊日期報告時，篩選範圍也會正確對應那一天，不會被「今天」
    # 的系統時鐘帶偏。缺日期或日期格式看不懂的項目一律丟棄，不猜測納入。
    cutoff = as_of - dt.timedelta(days=window_days - 1)
    in_window = []
    for it in items:
        raw = (it.get("date") or "").strip()
        try:
            item_date = dt.date.fromisoformat(raw)
        except ValueError:
            continue
        if cutoff <= item_date <= as_of:
            in_window.append(it)

    # 依日期新到舊排序（date 是 YYYY-MM-DD 字串，字串排序結果跟日期排序
    # 一致）
    sorted_items = sorted(in_window, key=lambda it: it.get("date", ""), reverse=True)
    lis = []
    for it in sorted_items[:5]:
        sentiment = SENTIMENT_CLASS.get(it.get("sentiment", "neu"), "neu")
        sector = html.escape(it.get("sector", ""))
        headline = html.escape(it.get("headline", ""))
        source = html.escape(it.get("source", ""))
        source_url = (it.get("source_url") or "").strip()
        if source_url:
            src_html = (
                f'<a href="{html.escape(source_url, quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{source}</a>'
            )
        else:
            src_html = source
        date = html.escape(it.get("date", ""))
        body = html.escape(it.get("body", ""))
        interp = html.escape(it.get("business_interpretation", ""))
        lis.append(f'''      <li class="sector-item {sentiment}">
        <div class="row-top">
          <span class="sector-chip">{sector}</span>
          <span class="headline">{headline}</span>
          <span class="src">{src_html}</span>
          <span class="date">{date}</span>
        </div>
        <div class="body">{body}</div>
        <div class="impact"><b>台商解讀：</b>{interp}</div>
      </li>''')
    return "\n".join(lis) if lis else "      <li>近 7 天無符合資料</li>"


def render_trade_implications(items: list[dict]) -> str:
    lis = []
    for it in items[:3]:
        title = html.escape(it.get("title", ""))
        body = html.escape(it.get("body", ""))
        lis.append(f"      <li><strong>{title}</strong>{body}</li>")
    return "\n".join(lis) if lis else "      <li>今日無資料</li>"


# ── 台灣—Türkiye 雙邊貿易 ──
# 這份不是每日自動抓取的資料，是使用者每月自己去財政部關務署查完後手動
# 維護的一個小 JSON 檔案（data/tw-tr-trade.json）。這裡只負責讀取、算出
# 月變動百分比、渲染成卡片；檔案不存在或格式壞掉都不應該讓整個 render
# 流程掛掉，只顯示提示文字即可。
TW_TR_TRADE_PATH = DATA_DIR / "tw-tr-trade.json"


def load_tw_tr_trade() -> dict | None:
    if not TW_TR_TRADE_PATH.exists():
        return None
    try:
        return json.loads(TW_TR_TRADE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! {TW_TR_TRADE_PATH.name} 讀取失敗，略過此卡片：{e}")
        return None


def _pct_change(cur, old):
    if cur is None or old is None or old == 0:
        return None
    return (cur - old) / old * 100


def render_tw_tr_trade(data: dict | None) -> str:
    months = (data or {}).get("months") or []
    if not months:
        return (
            '<p class="pending" style="font-size:0.88rem; line-height:1.8;">'
            "尚未提供台灣—Türkiye 雙邊貿易資料。這份是每月人工查詢後手動維護的"
            "（財政部關務署統計資料庫查詢系統），不是每日自動抓取——請在 "
            "<code>data/tw-tr-trade.json</code> 裡新增當月資料。</p>"
        )
    latest = months[-1]
    prev = months[-2] if len(months) >= 2 else None
    currency = html.escape(data.get("currency", "USD"))
    exp, imp = latest.get("exports_to_turkey"), latest.get("imports_from_turkey")
    bal = (exp - imp) if (exp is not None and imp is not None) else None
    exp_chg = _pct_change(exp, (prev or {}).get("exports_to_turkey"))
    imp_chg = _pct_change(imp, (prev or {}).get("imports_from_turkey"))

    def fmt_amount(v):
        return f"{v:,.0f}" if v is not None else "—"

    def fmt_chg(v):
        if v is None:
            return ""
        cls = "up" if v > 0 else ("down" if v < 0 else "flat")
        return f' <span class="{cls}">({v:+.1f}% 較上月)</span>'

    source_note = html.escape(data.get("source_note", ""))
    month_label = html.escape(latest.get("month", ""))

    return f'''    <div class="indicator-strip" style="grid-template-columns: repeat(3, 1fr); margin-bottom: 0.75rem;">
      <div class="indicator-cell">
        <div class="val">{currency} {fmt_amount(exp)}</div>
        <div class="lbl">台灣出口至 Türkiye（{month_label}）</div>
        <div class="delta">{fmt_chg(exp_chg)}</div>
      </div>
      <div class="indicator-cell">
        <div class="val">{currency} {fmt_amount(imp)}</div>
        <div class="lbl">台灣自 Türkiye 進口（{month_label}）</div>
        <div class="delta">{fmt_chg(imp_chg)}</div>
      </div>
      <div class="indicator-cell">
        <div class="val {'green' if (bal or 0) >= 0 else 'red'}">{currency} {fmt_amount(bal)}</div>
        <div class="lbl">台灣對 Türkiye 貿易餘額</div>
        <div class="delta">正值＝台灣出超</div>
      </div>
    </div>
    <p class="grp-note">資料來源：{source_note or '未註明'}</p>'''


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

    # TWD/TRY：不是任何央行的官方報價，是 fetch_daily.py 用 TCMB 官方
    # USD/TRY 跟第三方免費 API 的 USD/TWD 算出來的交叉匯率。抓取失敗時
    # 顯示跟其他「還沒接上」欄位一致的提示文字，不要留著空白或舊數字。
    twd_cross = (payload.get("fx") or {}).get("twd_try_cross")
    if twd_cross and twd_cross.get("try_per_twd") is not None:
        twd_try = f"{twd_cross['try_per_twd']:.4f}"
    else:
        twd_try = "待接即時報價"

    generated_label = payload.get("generated_at_label")
    if not generated_label:
        now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3)
        generated_label = now.strftime("%Y-%m-%d · %H:%M TRT")

    # {{DATE_ZH}}：中文日期（例如「2026年8月31日」），也拿來當「產業動態」
    # 7 天篩選窗口的基準日。用 resolved_date（這份報告實際對應的日期）
    # 而不是執行當下的系統時間去算，這樣重新補跑舊日期的報告（--date
    # 參數）時，日期顯示跟篩選範圍都會正確對應那一天，而不是永遠對應
    # 「今天」。
    report_date = dt.date.fromisoformat(resolved_date)
    date_zh = f"{report_date.year}年{report_date.month}月{report_date.day}日"

    replacements = {
        "{{USD_TRY}}": usd,
        "{{EUR_TRY}}": eur,
        "{{FUNDING_COST}}": funding_cost,
        "{{TWD_TRY}}": twd_try,
        "{{GENERATED_AT_LABEL}}": generated_label,
        "{{DATE_ZH}}": date_zh,
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
            render_industry_items(analysis.get("industry_items", []), report_date)
        )
        html_text = replace_block(
            html_text, "TRADE_IMPLICATIONS",
            render_trade_implications(analysis.get("trade_implications", []))
        )
        ai_status = f"applied ({analysis_path.name})"

    # 3. 台灣—Türkiye 雙邊貿易（人工每月維護，跟上面的 AI 區塊無關，
    #    不管 analysis 檔案存不存在都要跑）
    tw_tr_data = load_tw_tr_trade()
    html_text = replace_block(html_text, "TW_TR_TRADE", render_tw_tr_trade(tw_tr_data))

    # 4. 寫入輸出檔案（純產生物，永遠從範本重新產生，不會累積殘留內容）
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")

    print(f"-> 已從範本 {template_path} 重新產生輸出 {out_path}")
    print(f"   USD/TRY={usd}  EUR/TRY={eur}  隔夜融資成本={funding_cost}%")
    print(f"   更新時間標籤={generated_label}")
    print(f"   AI 區塊狀態：{ai_status}")
    print(f"   台土雙邊貿易：{'已提供 ' + str(len(tw_tr_data.get('months', []))) + ' 個月資料' if tw_tr_data else '尚未提供 data/tw-tr-trade.json'}")


if __name__ == "__main__":
    main()
