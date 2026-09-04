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


def load_recent_industry_items(resolved_date: str, window_days: int = 7) -> list[dict]:
    """產業動態原本只看「今天」這一份分析檔案，今天新聞剛好抓到 0 則的話
    整段就是空的，即使過去 6 天明明有資料也不會被用到。這裡改成真的把
    過去 window_days 天、每一天的 -analysis.json 都讀進來，把 industry_items
    全部彙整在一起，再交給 render_industry_items() 做日期篩選＋排序。
    某一天的分析檔案不存在或壞掉就跳過，不讓整段掛掉。"""
    try:
        as_of = dt.date.fromisoformat(resolved_date)
    except ValueError:
        return []
    items: list[dict] = []
    for i in range(window_days):
        d = as_of - dt.timedelta(days=i)
        p = DATA_DIR / f"{d.isoformat()}-analysis.json"
        if not p.exists():
            continue
        try:
            day_analysis = load_json(p)
        except Exception as e:
            print(f"! {p.name} 讀取失敗，跳過：{e}")
            continue
        items.extend(day_analysis.get("industry_items", []) or [])
    return items


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
    """Replace everything between <!-- [AI:]{marker}:START --> and
    <!-- [AI:]{marker}:END -->. The "AI:" prefix is optional so this works
    for both Gemini-generated blocks (<!-- AI:TODAY_TAKE:START -->) and
    non-AI blocks like TW_TR_TRADE / REPORTS_LIST (<!-- TW_TR_TRADE:START -->
    with no "AI:" prefix, deliberately, to signal they're not AI-generated).
    2026-08-31 實測踩到的坑：這兩個規則沒對齊之前，TW_TR_TRADE 跟
    REPORTS_LIST 這兩個區塊的 replace 永遠靜默失敗、原封不動回傳，不會報
    錯，看起來像是資料沒讀到，其實是標記格式對不起來。
    If marker not found, returns original text."""
    pattern = re.compile(
        rf"(<!-- (?:AI:)?{marker}:START.*?-->)(.*?)(<!-- (?:AI:)?{marker}:END.*?-->)",
        re.DOTALL,
    )
    if not pattern.search(html_text):
        print(f"! replace_block 警告：範本裡找不到 {marker} 的標記註解，"
              f"這段內容沒有被套進去（保留範本原本的預設文字）")
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


# ── 表 02 裡沒有自動資料來源的欄位 ──
# 核心通膨（B 指標）與政策利率（一週附賣回）目前沒有任何自動抓取的來源，
# 原本是直接寫死在範本 HTML 裡的靜態文字，永遠不會更新。改成從這份小
# JSON 讀取，維護方式跟 tw-tr-trade.json 一樣：TÜİK 或 TCMB 公布後手動
# 改一次。檔案不存在或壞掉時，欄位顯示「待更新」而不是舊數字，這樣至少
# 看得出來是過期，不會拿一個看起來很正常的錯數字誤導人。
MANUAL_INDICATORS_PATH = DATA_DIR / "indicators-manual.json"


def load_manual_indicators() -> dict:
    if not MANUAL_INDICATORS_PATH.exists():
        print(f"! 找不到 {MANUAL_INDICATORS_PATH.name}，表 02 的核心通膨與政策利率顯示為待更新")
        return {}
    try:
        return json.loads(MANUAL_INDICATORS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! {MANUAL_INDICATORS_PATH.name} 讀取失敗：{e}")
        return {}


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
            "<code>data/tw-tr-trade.json</code> 裡新增當月資料。"
            '<br><a href="https://portal.sw.nat.gov.tw/APGA/GA30" target="_blank" '
            'rel="noopener noreferrer">前往官方查詢系統</a></p>'
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

    exim_line = ""
    exim = load_eximclub_state()
    if exim and exim.get("id"):
        exim_url = (
            f"https://www.eximclub.com.tw/innerContent.aspx?"
            f"Type=Publish&ID={html.escape(str(exim['id']), quote=True)}"
            f"&Continen=3&Country=%E5%9C%9F%E8%80%B3%E5%85%B6"
        )
        exim_title = html.escape(exim.get("title") or "土耳其政經概況")
        exim_date = html.escape(exim.get("date", ""))
        exim_line = (
            f'<br>延伸閱讀：<a href="{exim_url}" target="_blank" '
            f'rel="noopener noreferrer">《{exim_title}》（{exim_date}，中國輸出入銀行貿易俱樂部）</a>'
        )

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
    <p class="grp-note">資料來源：{source_note or '未註明'}　·　
      <a href="https://portal.sw.nat.gov.tw/APGA/GA30" target="_blank" rel="noopener noreferrer">
      查看財政部關務署官方查詢系統（可自行查核或查詢更細分類）</a>{exim_line}</p>'''


# 貿易俱樂部（輸出入銀行）土耳其報告——這是獨立的狀態檔案
# （data/_eximclub_seen.json，由 fetch_daily.py 每天檢查、只在真的出現
# 新報告時更新），不是某一天的每日資料，所以不管今天有沒有新報告，
# 永遠顯示「目前已知最新一期」，不會因為不是剛好更新的那天就消失。
EXIMCLUB_STATE_PATH = DATA_DIR / "_eximclub_seen.json"


def load_eximclub_state() -> dict | None:
    if not EXIMCLUB_STATE_PATH.exists():
        return None
    try:
        return json.loads(EXIMCLUB_STATE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! {EXIMCLUB_STATE_PATH} 讀取失敗，略過：{e}")
        return None


# ── 期間報告列表 ──
# generate_period_report.py 每次產生週/月/季/年報告時，會順便維護
# docs/reports/_index.json 這份索引。這裡只負責讀取、渲染成連結列表，
# 索引檔不存在或是空的都要正常顯示「尚無報告」，不能讓整個 render 掛掉。
REPORTS_INDEX_PATH = ROOT / "docs" / "reports" / "_index.json"
PERIOD_LABEL_ZH = {"week": "週報", "month": "月報", "quarter": "季報", "year": "年報"}

# ── 歷史簡報封存 ──
# 每次 render 都把當天的完整輸出另存一份到 docs/archive/YYYY-MM-DD.html，
# 並在 docs/archive/_index.json 維護清單（同一天重複執行要覆蓋，不要
# 累積重複項）。跟 docs/reports/_index.json（週期報告）是分開的兩份索引，
# 這份是「每日」的。
ARCHIVE_DIR = ROOT / "docs" / "archive"
ARCHIVE_INDEX_PATH = ARCHIVE_DIR / "_index.json"

# 範本裡的「歷史簡報」面板不是伺服器端組 <li> HTML，是前端 JS 讀取
# script 裡一個 ARCHIVE_ITEMS 陣列自己 render（見 report-template.html
# 裡的 render()/safeHref()）。safeHref() 只放行以 "/" 或 "#" 開頭的
# 站內絕對路徑（刻意擋掉外部連結與 javascript: 之類的危險 href），所以
# 這裡產生的 href 一定要包含 GitHub Pages 專案路徑這一層，不能只給
# "archive/xxx.html" 這種相對路徑（會被 safeHref 擋成 "#"，點了沒反應）。
# 如果之後改了 repo 名稱或換成自訂網域，這個常數要跟著改。
SITE_BASE_PATH = "/news"
_WEEKDAY_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def load_reports_index() -> list:
    if not REPORTS_INDEX_PATH.exists():
        return []
    try:
        return json.loads(REPORTS_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! {REPORTS_INDEX_PATH} 讀取失敗，略過此區塊：{e}")
        return []


def render_reports_list(index: list) -> str:
    if not index:
        return (
            '<p class="pending" style="font-size:0.88rem;">尚未產生過任何期間報告。'
            "到 GitHub Actions 手動觸發 “Generate Period Report” 即可產生。</p>"
        )
    sorted_idx = sorted(index, key=lambda e: e.get("end", ""), reverse=True)[:12]
    lis = []
    for e in sorted_idx:
        label = html.escape(PERIOD_LABEL_ZH.get(e.get("period", ""), e.get("period", "")))
        href = html.escape(f"reports/{e.get('file','')}", quote=True)
        start = html.escape(e.get("start", ""))
        end = html.escape(e.get("end", ""))
        lis.append(f'''      <li class="sector-item neu">
        <div class="row-top">
          <span class="sector-chip">{label}</span>
          <span class="headline"><a href="{href}" target="_blank" rel="noopener noreferrer">{start} ～ {end}</a></span>
        </div>
      </li>''')
    return "\n".join(lis)


def load_archive_index() -> list:
    if not ARCHIVE_INDEX_PATH.exists():
        return []
    try:
        return json.loads(ARCHIVE_INDEX_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! {ARCHIVE_INDEX_PATH} 讀取失敗，視為空清單：{e}")
        return []


def save_to_archive(rendered_html: str, resolved_date: str, headline: str) -> list:
    """把當天最終產生的完整報告另存一份到 docs/archive/，並更新索引。
    同一天重複執行（例如手動補跑 --date）要覆蓋舊的那一筆，不能累積
    出兩筆同一天的紀錄。"""
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    (ARCHIVE_DIR / f"{resolved_date}.html").write_text(rendered_html, encoding="utf-8")

    entries = load_archive_index()
    entries = [e for e in entries if e.get("date") != resolved_date]
    entries.append({"date": resolved_date, "file": f"{resolved_date}.html", "headline": headline})
    entries.sort(key=lambda e: e.get("date", ""), reverse=True)

    ARCHIVE_INDEX_PATH.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return entries


def render_archive_items_js(entries: list, resolved_date: str, limit: int = 60) -> str:
    """把 archive/_index.json 的內容轉成範本 <script> 裡
    `const ARCHIVE_ITEMS = [...]` 這行要用的 JS 陣列。用 json.dumps
    產生，不用手拼字串，避免標題裡出現引號、反斜線等字元時把 JS 弄壞。
    resolved_date 對應的那一筆標成 current，面板上會顯示「● CURRENT」。"""
    items = []
    for e in entries[:limit]:
        date_str = e.get("date", "")
        try:
            day_label = f"週{_WEEKDAY_ZH[dt.date.fromisoformat(date_str).weekday()]}"
        except ValueError:
            day_label = ""
        items.append({
            "date": date_str,
            "day": day_label,
            "title": e.get("headline", ""),
            "href": f"{SITE_BASE_PATH}/archive/{e.get('file', '')}",
            "current": date_str == resolved_date,
        })
    return "const ARCHIVE_ITEMS = " + json.dumps(items, ensure_ascii=False, indent=2) + ";"

# ── 表 03「市場價格快照」的週／月／年初至今變動 ──
# 這三欄原本是範本裡寫死的「—」，從來沒有算過。EVDS 那邊沒有現成的歷史
# 匯率序列可用，但 data/ 裡每天都留了一份當日快照，把過去的檔案讀回來
# 就是現成的時間序列。
#
# 找基準日的規則：取「日期 <= 目標日」之中最接近的那一份，而且不能離目標
# 日太遠（tolerance）。假日或抓取失敗會讓某幾天沒有檔案，容忍幾天是必要
# 的；但容忍過頭就會拿一個月前的數字當「週變動」，所以寧可顯示 — 。
def load_daily_snapshots() -> list[tuple[dt.date, dict]]:
    out = []
    for f in sorted(DATA_DIR.glob("*.json")):
        if not re.match(r"\d{4}-\d{2}-\d{2}\.json$", f.name):
            continue
        try:
            out.append((dt.date.fromisoformat(f.stem), load_json(f)))
        except Exception:
            continue
    return out


def _metric_value(payload: dict, key: str):
    """從一份每日快照裡取出某個市場價格。取不到回 None。"""
    try:
        if key == "USD":
            return (payload.get("fx") or {}).get("rates", {}).get("USD", {}).get("per_unit_selling")
        if key == "EUR":
            return (payload.get("fx") or {}).get("rates", {}).get("EUR", {}).get("per_unit_selling")
        if key == "TWD":
            return (payload.get("fx") or {}).get("twd_try_cross", {}).get("try_per_twd")
        if key == "BRENT":
            return (payload.get("brent_oil") or {}).get("usd_per_barrel")
    except Exception:
        return None
    return None


def _snapshot_on_or_before(snaps, target: dt.date, tolerance_days: int):
    best = None
    for d, payload in snaps:
        if d <= target and (target - d).days <= tolerance_days:
            if best is None or d > best[0]:
                best = (d, payload)
    return best


def market_changes(snaps, key: str, current, as_of: dt.date) -> dict:
    """回傳 {week, week_cls, month, month_cls, ytd, ytd_cls}，算不出來就是 —。"""
    out = {}

    def pct(old):
        if old in (None, 0) or current is None:
            return None
        return (current - old) / abs(old) * 100

    def cell(v):
        if v is None:
            return "—", "flat"
        txt = f"{v:+.1f}%".replace("-", "\u2212")
        return txt, ("up" if v > 0 else ("down" if v < 0 else "flat"))

    for name, delta, tol in (("week", 7, 4), ("month", 30, 8)):
        snap = _snapshot_on_or_before(snaps, as_of - dt.timedelta(days=delta), tol)
        out[name], out[name + "_cls"] = cell(pct(_metric_value(snap[1], key)) if snap else None)

    # 年初至今：要有夠靠近年初的快照才算，否則「年初至今」名不副實。
    jan = [(d, p) for d, p in snaps if d.year == as_of.year]
    if jan and min(d for d, _ in jan) <= dt.date(as_of.year, 1, 15):
        first = min(jan, key=lambda x: x[0])
        out["ytd"], out["ytd_cls"] = cell(pct(_metric_value(first[1], key)))
    else:
        out["ytd"], out["ytd_cls"] = "—", "flat"
    return out


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

    # 核心指標區塊：CPI、商品貿易差額。這兩個原本是 8/29 手寫的靜態樣本
    # 文字，從來沒有真的接資料，這裡改成讀 EVDS 抓到的真實序列。日期字串
    # 格式來自 borsapy，實機沒驗證過長怎樣，所以月份標籤用寬鬆的方式解析，
    # 解析不出來就直接顯示原始日期字串，不要讓整段掛掉或顯示錯誤月份。
    def _month_label(date_str: str) -> str:
        if not date_str:
            return "最新"
        m = re.match(r"^(\d{4})-(\d{1,2})", date_str)
        if m:
            return f"{int(m.group(2))}月"
        m = re.match(r"^(\d{1,2})-\d{4}", date_str)
        if m and int(m.group(1)) <= 12:
            return f"{int(m.group(1))}月"
        return date_str

    # EVDS 抓回來的 cpi 序列是「消費者物價指數點數」，不是年通膨率。
    # 2026-09-05 確認：2025-07=100.42、2025-08=102.47，是重新基期後的指數。
    # 之前這裡直接把指數點數當百分比顯示，首頁核心指標因此出現「年通膨率
    # 134.75%」這種不可能的數字（真正的年通膨率約 31.5%）。現在改成拿當月
    # 指數除以去年同月指數算年增率。
    #
    # 去年同月一律用位置索引（往前數 12 筆）。序列不足 13 個月時不硬算，
    # 顯示「—」，寧可空白也不要放一個錯的通膨率。
    cpi_series = payload.get("macro", {}).get("cpi") or []
    cpi_vals = [e["value"] for e in cpi_series if "value" in e]

    def _cpi_yoy(i: int):
        """第 i 筆（負索引）相對去年同月的年增率，資料不足回 None。"""
        j = i - 12
        if len(cpi_vals) >= abs(j) and cpi_vals[j]:
            return (cpi_vals[i] - cpi_vals[j]) / cpi_vals[j] * 100
        return None

    cpi_yoy_now = _cpi_yoy(-1) if cpi_vals else None
    cpi_yoy_prev = _cpi_yoy(-2) if len(cpi_vals) >= 2 else None

    if cpi_vals:
        cpi_month_label = _month_label(cpi_series[-1].get("date", ""))
    else:
        cpi_month_label = "—"

    if cpi_yoy_now is not None:
        cpi_value = f"{cpi_yoy_now:.2f}"
        if cpi_yoy_prev is not None:
            cpi_delta = f"前值 {cpi_yoy_prev:.2f}%"
            cpi_color = "green" if cpi_yoy_now <= cpi_yoy_prev else "red"
        else:
            cpi_delta = "尚無前一期可比較"
            cpi_color = "amber"
    elif cpi_vals:
        cpi_value = "—"
        cpi_delta = f"指數序列僅 {len(cpi_vals)} 個月，不足以計算年通膨率"
        cpi_color = "amber"
    else:
        cpi_value, cpi_delta, cpi_color = "—", "資料尚未取得", "amber"

    # 貿易差額 = 出口序列 - 進口序列（EVDS 沒有現成乾淨的「淨額」序列可用，
    # 2026-08-31 使用者確認過 Q16「Net Exports...Merchanting」其實是轉口
    # 貿易這種小眾特殊項目，改用出口/進口兩支序列自己相減）。兩個序列都
    # 是 borsapy 抓回來的，用日期字串對齊，不能直接假設兩邊筆數/順序一樣。
    exp_series = payload.get("macro", {}).get("trade_exports") or []
    imp_series = payload.get("macro", {}).get("trade_imports") or []
    exp_by_date = {e["date"]: e["value"] for e in exp_series if "date" in e and "value" in e}
    imp_by_date = {e["date"]: e["value"] for e in imp_series if "date" in e and "value" in e}
    common_dates = sorted(set(exp_by_date) & set(imp_by_date))
    tb_points = [(d, exp_by_date[d] - imp_by_date[d]) for d in common_dates]

    if tb_points:
        tb_date, tb_val = tb_points[-1]  # 單位：百萬美元
        tb_label = "順差" if tb_val >= 0 else "逆差"
        tb_value = f"{abs(tb_val) / 100:.1f} 億美元"
        tb_color = "green" if tb_val >= 0 else "red"
        tb_month_label = _month_label(tb_date)
        if len(tb_points) >= 2:
            prev_date, prev_val = tb_points[-2]
            prev_label = "順差" if prev_val >= 0 else "逆差"
            tb_delta = f"上月：{abs(prev_val) / 100:.1f} 億美元{prev_label}　（資料月份：{tb_date}）"
        else:
            tb_delta = f"尚無前一期資料可比較　（資料月份：{tb_date}）"
    else:
        tb_value, tb_label, tb_color = "—", "", "amber"
        tb_month_label, tb_delta = "—", "資料尚未取得"

    # 「核心指標」第三格改顯示台灣—Türkiye 雙邊貿易餘額的簡短版（不是
    # Türkiye 對全世界的整體貿易差額——那個算好了但不放在這一格顯示，
    # tb_value 等變數保留著沒有刪，下面完整版卡片＝06 台灣—Türkiye 雙邊
    # 貿易，或之後有別的地方要用整體差額還可以接）。
    tw_tr_data_early = load_tw_tr_trade()
    tw_months = (tw_tr_data_early or {}).get("months") or []
    if tw_months:
        tw_latest = tw_months[-1]
        tw_exp, tw_imp = tw_latest.get("exports_to_turkey"), tw_latest.get("imports_from_turkey")
        if tw_exp is not None and tw_imp is not None:
            tw_bal = tw_exp - tw_imp
            twtr_label = "出超" if tw_bal >= 0 else "入超"
            twtr_value = f"${abs(tw_bal) / 1_000_000:,.1f}M"
            twtr_color = "green" if tw_bal >= 0 else "red"
            twtr_month_label = html.escape(tw_latest.get("month", ""))
            if len(tw_months) >= 2:
                prev = tw_months[-2]
                p_exp, p_imp = prev.get("exports_to_turkey"), prev.get("imports_from_turkey")
                if p_exp is not None and p_imp is not None:
                    p_bal = p_exp - p_imp
                    p_label = "出超" if p_bal >= 0 else "入超"
                    twtr_delta = f"上月：台灣{p_label} ${abs(p_bal) / 1_000_000:,.1f}M"
                else:
                    twtr_delta = "上月資料不完整"
            else:
                twtr_delta = "尚無前一期資料可比較"
        else:
            twtr_value, twtr_label, twtr_color = "—", "", "amber"
            twtr_month_label, twtr_delta = "—", "當月出口/進口資料不完整"
    else:
        twtr_value, twtr_label, twtr_color = "—", "", "amber"
        twtr_month_label, twtr_delta = "—", "尚未提供 data/tw-tr-trade.json"

    # TWD/TRY：不是任何央行的官方報價，是 fetch_daily.py 用 TCMB 官方
    # USD/TRY 跟第三方免費 API 的 USD/TWD 算出來的交叉匯率。抓取失敗時
    # 顯示跟其他「還沒接上」欄位一致的提示文字，不要留著空白或舊數字。
    twd_cross = (payload.get("fx") or {}).get("twd_try_cross")
    if twd_cross and twd_cross.get("try_per_twd") is not None:
        twd_try = f"{twd_cross['try_per_twd']:.4f}"
    else:
        twd_try = "待接即時報價"

    brent = payload.get("brent_oil")
    if brent and brent.get("usd_per_barrel") is not None:
        brent_value = f"${brent['usd_per_barrel']:.2f}"
        brent_note = f"每桶美元，資料日期 {brent.get('date','')}"
        if brent.get("stale"):
            brent_note += "（超過 5 天沒更新，留意可能過期）"
    else:
        brent_value = "待接即時報價"
        brent_note = "能源進口是 Türkiye 逆差主因"

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

    # ── 表 02「主要經濟指標追蹤」──
    # 第 1、5、6、7 列改用 EVDS 真實序列計算，不再是範本裡的寫死文字。
    # 第 2、3 列沒有自動來源，讀 data/indicators-manual.json。
    # 第 4 列（隔夜融資成本）本來就已經是 {{FUNDING_COST}}。
    #
    # 注意：序列的日期字串格式來自 borsapy，沒有實機驗證過，所以「去年
    # 同期」一律用位置索引（往前數 12 筆）而不是解析日期去找，避免格式
    # 一變就整段掛掉。序列有缺月時這個做法會失準，寧可顯示 — 也不要
    # 算出一個看起來合理的錯誤年增率。
    def _delta_cls(v, lower_is_better=False):
        if v is None:
            return "flat"
        if abs(v) < 1e-9:
            return "flat"
        good = v < 0 if lower_is_better else v > 0
        return "down" if (v < 0) else "up"

    def _pp(v):
        return f"{v:+.2f}pp".replace("+", "+").replace("-", "−") if v is not None else "—"

    def _pct(v):
        return f"{v:+.1f}%".replace("-", "−") if v is not None else "—"

    def _yoy(series_vals, idx=-1, span=12):
        """往前數 span 筆當作去年同期。筆數不足就回 None。"""
        if len(series_vals) >= span + 1:
            cur, old = series_vals[idx], series_vals[idx - span]
            if old:
                return (cur - old) / abs(old) * 100
        return None

    # 第 1 列：CPI。跟首頁核心指標同一組計算，兩處數字保證一致。
    # 最新值顯示年通膨率（＝年增率），所以「年增率」欄改放月增率，
    # 兩欄才不會重複同一個數字。
    if cpi_yoy_now is not None:
        cpi_row_name = f"消費者物價指數（CPI，{cpi_month_label}）"
        cpi_row_value = f"{cpi_yoy_now:.2f}%"
        pp = (cpi_yoy_now - cpi_yoy_prev) if cpi_yoy_prev is not None else None
        cpi_row_delta = _pp(pp)
        cpi_row_delta_cls = _delta_cls(pp)
        mom = ((cpi_vals[-1] - cpi_vals[-2]) / cpi_vals[-2] * 100
               if len(cpi_vals) >= 2 and cpi_vals[-2] else None)
        cpi_row_yoy = (f"月增 {mom:+.2f}%".replace("-", "−")
                       if mom is not None else "—")
    else:
        cpi_row_name = "消費者物價指數（CPI）"
        cpi_row_value = cpi_row_delta = cpi_row_yoy = "—"
        cpi_row_delta_cls = "flat"

    # 第 2、3 列：手動維護
    manual = load_manual_indicators()

    def _manual(key, field, default="待更新"):
        v = (manual.get(key) or {}).get(field)
        return html.escape(str(v)) if v not in (None, "") else default

    core_cpi_value = _manual("core_cpi", "value")
    core_cpi_delta = _manual("core_cpi", "delta", "—")
    core_cpi_delta_cls = _manual("core_cpi", "delta_class", "flat")
    core_cpi_yoy = _manual("core_cpi", "yoy", "—")
    policy_rate_value = _manual("policy_rate", "value")
    policy_rate_delta = _manual("policy_rate", "delta", "—")
    policy_rate_delta_cls = _manual("policy_rate", "delta_class", "flat")
    policy_rate_expect = _manual("policy_rate", "expectation", "—")

    # 第 5、6、7 列：出口、進口序列（單位：百萬美元）
    exp_vals = [exp_by_date[d] for d in common_dates]
    imp_vals = [imp_by_date[d] for d in common_dates]
    tb_vals = [e - i for e, i in zip(exp_vals, imp_vals)]

    if tb_vals:
        cur_tb = tb_vals[-1]
        tb_row_name = "月貿易順差" if cur_tb >= 0 else "月貿易逆差"
        tb_row_value = f"{abs(cur_tb) / 100:.1f} 億美元"
        if len(tb_vals) >= 2:
            d = (abs(cur_tb) - abs(tb_vals[-2])) / 100
            tb_row_delta = f"{d:+.1f} 億".replace("-", "−")
            tb_row_delta_cls = "up" if d > 0 else ("down" if d < 0 else "flat")
        else:
            tb_row_delta, tb_row_delta_cls = "—", "flat"
        y = _yoy([abs(v) for v in tb_vals])
        tb_row_yoy, tb_row_yoy_cls = _pct(y), _delta_cls(y)
    else:
        tb_row_name = "月貿易差額"
        tb_row_value = tb_row_delta = tb_row_yoy = "—"
        tb_row_delta_cls = tb_row_yoy_cls = "flat"

    if exp_vals:
        export_row_value = f"{exp_vals[-1] / 100:.1f} 億美元"
        if len(exp_vals) >= 2:
            d = (exp_vals[-1] - exp_vals[-2]) / 100
            export_row_delta = f"{d:+.1f} 億".replace("-", "−")
            export_row_delta_cls = "up" if d > 0 else ("down" if d < 0 else "flat")
        else:
            export_row_delta, export_row_delta_cls = "—", "flat"
        y = _yoy(exp_vals)
        export_row_yoy, export_row_yoy_cls = _pct(y), _delta_cls(y)
    else:
        export_row_value = export_row_delta = export_row_yoy = "—"
        export_row_delta_cls = export_row_yoy_cls = "flat"

    cov = [e / i * 100 for e, i in zip(exp_vals, imp_vals) if i]
    if cov:
        coverage_value = f"{cov[-1]:.1f}%"
        pp = (cov[-1] - cov[-2]) if len(cov) >= 2 else None
        coverage_delta, coverage_delta_cls = _pp(pp), _delta_cls(pp)
        pp_y = (cov[-1] - cov[-13]) if len(cov) >= 13 else None
        coverage_yoy, coverage_yoy_cls = _pp(pp_y), _delta_cls(pp_y)
    else:
        coverage_value = coverage_delta = coverage_yoy = "—"
        coverage_delta_cls = coverage_yoy_cls = "flat"

    # 表 03 的週／月／年初至今變動
    snaps = load_daily_snapshots()
    mkt = {
        "USD":   market_changes(snaps, "USD",   (rates.get("USD") or {}).get("per_unit_selling"), report_date),
        "EUR":   market_changes(snaps, "EUR",   (rates.get("EUR") or {}).get("per_unit_selling"), report_date),
        "TWD":   market_changes(snaps, "TWD",   (twd_cross or {}).get("try_per_twd"), report_date),
        "BRENT": market_changes(snaps, "BRENT", (brent or {}).get("usd_per_barrel"), report_date),
    }

    replacements = {
        "{{USD_TRY}}": usd,
        "{{EUR_TRY}}": eur,
        "{{FUNDING_COST}}": funding_cost,
        "{{TWD_TRY}}": twd_try,
        "{{BRENT_VALUE}}": brent_value,
        "{{BRENT_NOTE}}": brent_note,
        "{{CPI_VALUE}}": cpi_value,
        "{{CPI_COLOR}}": cpi_color,
        "{{CPI_MONTH_LABEL}}": cpi_month_label,
        "{{CPI_DELTA}}": cpi_delta,
        "{{TRADE_BALANCE_VALUE}}": tb_value,
        "{{TRADE_BALANCE_COLOR}}": tb_color,
        "{{TRADE_BALANCE_LABEL}}": tb_label,
        "{{TRADE_BALANCE_MONTH_LABEL}}": tb_month_label,
        "{{TRADE_BALANCE_DELTA}}": tb_delta,
        "{{TWTR_VALUE}}": twtr_value,
        "{{TWTR_COLOR}}": twtr_color,
        "{{TWTR_LABEL}}": twtr_label,
        "{{TWTR_MONTH_LABEL}}": twtr_month_label,
        "{{TWTR_DELTA}}": twtr_delta,
        "{{GENERATED_AT_LABEL}}": generated_label,
        "{{DATE_ZH}}": date_zh,
        "{{CPI_ROW_NAME}}": cpi_row_name,
        "{{CPI_ROW_VALUE}}": cpi_row_value,
        "{{CPI_ROW_DELTA}}": cpi_row_delta,
        "{{CPI_ROW_DELTA_CLS}}": cpi_row_delta_cls,
        "{{CPI_ROW_YOY}}": cpi_row_yoy,
        "{{CORE_CPI_VALUE}}": core_cpi_value,
        "{{CORE_CPI_DELTA}}": core_cpi_delta,
        "{{CORE_CPI_DELTA_CLS}}": core_cpi_delta_cls,
        "{{CORE_CPI_YOY}}": core_cpi_yoy,
        "{{POLICY_RATE_VALUE}}": policy_rate_value,
        "{{POLICY_RATE_DELTA}}": policy_rate_delta,
        "{{POLICY_RATE_DELTA_CLS}}": policy_rate_delta_cls,
        "{{POLICY_RATE_EXPECT}}": policy_rate_expect,
        "{{TB_ROW_NAME}}": tb_row_name,
        "{{TB_ROW_VALUE}}": tb_row_value,
        "{{TB_ROW_DELTA}}": tb_row_delta,
        "{{TB_ROW_DELTA_CLS}}": tb_row_delta_cls,
        "{{TB_ROW_YOY}}": tb_row_yoy,
        "{{TB_ROW_YOY_CLS}}": tb_row_yoy_cls,
        "{{EXPORT_ROW_VALUE}}": export_row_value,
        "{{EXPORT_ROW_DELTA}}": export_row_delta,
        "{{EXPORT_ROW_DELTA_CLS}}": export_row_delta_cls,
        "{{EXPORT_ROW_YOY}}": export_row_yoy,
        "{{EXPORT_ROW_YOY_CLS}}": export_row_yoy_cls,
        "{{COVERAGE_VALUE}}": coverage_value,
        "{{COVERAGE_DELTA}}": coverage_delta,
        "{{COVERAGE_DELTA_CLS}}": coverage_delta_cls,
        "{{COVERAGE_YOY}}": coverage_yoy,
        "{{COVERAGE_YOY_CLS}}": coverage_yoy_cls,
        "{{USD_WEEK}}": mkt["USD"]["week"],
        "{{USD_WEEK_CLS}}": mkt["USD"]["week_cls"],
        "{{USD_MONTH}}": mkt["USD"]["month"],
        "{{USD_MONTH_CLS}}": mkt["USD"]["month_cls"],
        "{{USD_YTD}}": mkt["USD"]["ytd"],
        "{{USD_YTD_CLS}}": mkt["USD"]["ytd_cls"],
        "{{EUR_WEEK}}": mkt["EUR"]["week"],
        "{{EUR_WEEK_CLS}}": mkt["EUR"]["week_cls"],
        "{{EUR_MONTH}}": mkt["EUR"]["month"],
        "{{EUR_MONTH_CLS}}": mkt["EUR"]["month_cls"],
        "{{EUR_YTD}}": mkt["EUR"]["ytd"],
        "{{EUR_YTD_CLS}}": mkt["EUR"]["ytd_cls"],
        "{{TWD_WEEK}}": mkt["TWD"]["week"],
        "{{TWD_WEEK_CLS}}": mkt["TWD"]["week_cls"],
        "{{TWD_MONTH}}": mkt["TWD"]["month"],
        "{{TWD_MONTH_CLS}}": mkt["TWD"]["month_cls"],
        "{{TWD_YTD}}": mkt["TWD"]["ytd"],
        "{{TWD_YTD_CLS}}": mkt["TWD"]["ytd_cls"],
        "{{BRENT_WEEK}}": mkt["BRENT"]["week"],
        "{{BRENT_WEEK_CLS}}": mkt["BRENT"]["week_cls"],
        "{{BRENT_MONTH}}": mkt["BRENT"]["month"],
        "{{BRENT_MONTH_CLS}}": mkt["BRENT"]["month_cls"],
        "{{BRENT_YTD}}": mkt["BRENT"]["ytd"],
        "{{BRENT_YTD_CLS}}": mkt["BRENT"]["ytd_cls"],
    }

    for token, value in replacements.items():
        html_text = html_text.replace(token, str(value))

    # 2. AI 判斷區塊：從 {date}-analysis.json 讀取並替換
    analysis_path = DATA_DIR / f"{resolved_date}-analysis.json"
    ai_status = "skipped (no analysis file)"
    archive_headline = date_zh  # 歷史簡報用的標題，沒有分析檔案就退回顯示日期
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
            html_text, "TRADE_IMPLICATIONS",
            render_trade_implications(analysis.get("trade_implications", []))
        )
        ai_status = f"applied ({analysis_path.name})"

        # 歷史簡報用的 headline：優先取第一則關鍵事件標題，沒有就維持
        # 上面設定的日期字串
        _events = analysis.get("key_events", [])
        if _events and _events[0].get("headline"):
            archive_headline = _events[0]["headline"]

    # 3. 台灣—Türkiye 雙邊貿易（人工每月維護，跟上面的 AI 區塊無關，
    #    不管 analysis 檔案存不存在都要跑）
    tw_tr_data = tw_tr_data_early
    html_text = replace_block(html_text, "TW_TR_TRADE", render_tw_tr_trade(tw_tr_data))

    # 產業動態：彙整過去 7 天各天的分析檔案，獨立於「今天」有沒有分析檔案，
    # 就算今天 Gemini 那步失敗、沒有今天的 -analysis.json，前幾天有資料
    # 一樣要能顯示出來
    html_text = replace_block(
        html_text, "INDUSTRY",
        render_industry_items(load_recent_industry_items(resolved_date), report_date)
    )

    reports_index = load_reports_index()
    html_text = replace_block(html_text, "REPORTS_LIST", render_reports_list(reports_index))

    # 歷史簡報：先更新索引（用今天的 headline），再把清單渲染進 html_text，
    # 最後才把這個「完成品」存成 archive 裡的今日快照
    archive_entries = save_to_archive(html_text, resolved_date, archive_headline)
    html_text = replace_block(
        html_text, "ARCHIVE_ITEMS",
        render_archive_items_js(archive_entries, resolved_date)
    )

    # 4. 寫入輸出檔案（純產生物，永遠從範本重新產生，不會累積殘留內容）
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_text, encoding="utf-8")

    print(f"-> 已從範本 {template_path} 重新產生輸出 {out_path}")
    print(f"   USD/TRY={usd}  EUR/TRY={eur}  隔夜融資成本={funding_cost}%")
    print(f"   更新時間標籤={generated_label}")
    print(f"   AI 區塊狀態：{ai_status}")
    print(f"   台土雙邊貿易：{'已提供 ' + str(len(tw_tr_data.get('months', []))) + ' 個月資料' if tw_tr_data else '尚未提供 data/tw-tr-trade.json'}")
    print(f"   歷史簡報：共 {len(archive_entries)} 篇（今日已封存為 archive/{resolved_date}.html）")


if __name__ == "__main__":
    main()
