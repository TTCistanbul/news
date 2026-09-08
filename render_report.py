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


# ── Türkiye 整體月度貿易統計（出口／進口／貿易差額／涵蓋率）──
# 2026-09 發現原本用 EVDS「出口/進口」時間序列自己相減算出的貿易差額，
# 跟貿易部/TÜİK 官方新聞稿公布的數字對不起來（差距超過 6 成，不是單純
# 月份落後可以解釋），懷疑是統計口徑不同。改成跟 tw-tr-trade.json 同一套
# 做法：每月人工對照貿易部新聞稿（ticaret.gov.tr/istatistikler/
# dis-ticaret-istatistikleri）手動填一次 data/turkey-trade-manual.json。
# 格式（單位：百萬美元，跟新聞稿原始單位一致，換算成「億」在渲染時才做）：
# {
#   "months": [
#     {
#       "month": "2026-08",
#       "exports": 23467,
#       "imports": 28706,
#       "coverage_pct": 81.8,        // 出口對進口涵蓋率，新聞稿裡的「karşılama oranı」
#       "exports_yoy_pct": 8.1,      // 新聞稿裡的出口年增率，直接抄，不要自己重算
#       "imports_yoy_pct": 10.5,     // 新聞稿裡的進口年增率
#       "balance_yoy_pct": 22.3      // 新聞稿裡的貿易逆差年增率（如果有提到的話，沒有就留 null）
#     }
#   ]
# }
# 年增率優先採用新聞稿裡直接寫的數字，不用自己拿 12 個月前的手動資料去
# 算——一來手動資料要累積滿 13 個月才算得出來，二來官方新聞稿的年增率
# 本身可能用了修正後的基期數字，比我們自己算的更準。
TURKEY_TRADE_MANUAL_PATH = DATA_DIR / "turkey-trade-manual.json"


def load_turkey_trade_manual() -> dict | None:
    if not TURKEY_TRADE_MANUAL_PATH.exists():
        return None
    try:
        return json.loads(TURKEY_TRADE_MANUAL_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! {TURKEY_TRADE_MANUAL_PATH.name} 讀取失敗，略過：{e}")
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


def render_tw_tr_trade(data: dict | None, sectors_data: dict | None = None,
                        monthly_data: dict | None = None) -> str:
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
    currency = html.escape(data.get("currency", "USD"))

    # 2026-09 改成「年初至今累計」而不是單月數字——使用者要求開頭的卡片
    # 顯示「1-7月」這種累計值，不是只顯示最新一個月（7月）單月進出口。
    # 期間標籤直接從實際存在的月份資料反推（取最小/最大月份），不是寫死
    # 「1-N月」，這樣如果 tw-tr-trade.json 裡今年的月份有缺（例如漏填某
    # 個月），標籤會誠實反映實際加總的範圍，不會謊稱涵蓋到還沒填的月份。
    latest_month_str = latest.get("month", "")
    cur_year = latest_month_str.split("-")[0] if "-" in latest_month_str else ""
    ytd_months = sorted(
        (m for m in months if m.get("month", "").startswith(f"{cur_year}-")),
        key=lambda m: m["month"],
    )
    if ytd_months:
        exp = sum((m.get("exports_to_turkey") or 0) for m in ytd_months)
        imp = sum((m.get("imports_from_turkey") or 0) for m in ytd_months)
        first_m = int(ytd_months[0]["month"].split("-")[1])
        last_m = int(ytd_months[-1]["month"].split("-")[1])
        period_label = (
            f"{cur_year}年{last_m}月" if first_m == last_m
            else f"{cur_year}年{first_m}-{last_m}月累計"
        )
    else:
        exp = imp = None
        period_label = latest_month_str

    bal = (exp - imp) if (exp is not None and imp is not None) else None

    # 跟去年同期比較（年增率）：來源是 tw-tr-trade-sectors.json 裡標記
    # comparison_only 的那筆同期資料（例如額外查的「114年1-7月」）。只有
    # 涵蓋月數（months_covered）跟今年這裡實際加總的月數完全一致時才拿來
    # 算年增率，避免「7個月」跟「6個月」硬比出一個誤導的百分比；月數對
    # 不上或找不到這筆資料，年增率就不顯示。sectors 檔案單位是千美元，
    # 這裡要乘 1000 換算成原始美元金額才能跟 tw-tr-trade.json 的數字比。
    exp_chg = imp_chg = None
    if sectors_data and ytd_months:
        comp_entry = next(
            (y for y in (sectors_data.get("years") or [])
             if y.get("comparison_only") and y.get("months_covered") == len(ytd_months)),
            None
        )
        if comp_entry:
            prev_exp = sum((comp_entry.get("exports_by_section") or {}).values()) * 1000
            prev_imp = sum((comp_entry.get("imports_by_section") or {}).values()) * 1000
            exp_chg = _pct_change(exp, prev_exp)
            imp_chg = _pct_change(imp, prev_imp)

    def fmt_amount(v):
        return f"{v:,.0f}" if v is not None else "—"

    def fmt_chg(v):
        if v is None:
            return ""
        cls = "up" if v > 0 else ("down" if v < 0 else "flat")
        return f' <span class="{cls}">({v:+.1f}% 較去年同期)</span>'

    source_note = html.escape(data.get("source_note", ""))
    period_label_html = html.escape(period_label)

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
        <div class="lbl">台灣出口至 Türkiye（{period_label_html}）</div>
        <div class="delta">{fmt_chg(exp_chg)}</div>
      </div>
      <div class="indicator-cell">
        <div class="val">{currency} {fmt_amount(imp)}</div>
        <div class="lbl">台灣自 Türkiye 進口（{period_label_html}）</div>
        <div class="delta">{fmt_chg(imp_chg)}</div>
      </div>
      <div class="indicator-cell">
        <div class="val {'green' if (bal or 0) >= 0 else 'red'}">{currency} {fmt_amount(bal)}</div>
        <div class="lbl">台灣對 Türkiye 貿易餘額（{period_label_html}）</div>
        <div class="delta">正值＝台灣出超</div>
      </div>
    </div>
    {render_tw_tr_annual_chart(sectors_data)}
    {render_tw_tr_sector_table(sectors_data)}
    {render_tw_tr_monthly_sector_chart(monthly_data, (sectors_data or {}).get("sections") or {})}
    <p class="grp-note">資料來源：{source_note or '未註明'}　·　
      <a href="https://portal.sw.nat.gov.tw/APGA/GA30" target="_blank" rel="noopener noreferrer">
      查看財政部關務署官方查詢系統（可自行查核或查詢更細分類）</a>{exim_line}</p>'''


# ── 台灣—Türkiye 貿易：主要出口產業（HS 21 類）＋ 年度趨勢 ──
# 2026-09 新增。跟 tw-tr-trade.json（單月出口/進口總額）是分開的檔案，
# 因為這份是按 HS 21 類拆分、且橫跨多個年度（GA30 查詢系統可以查「按年」
# 不限當年），資料形狀完全不同：tw-tr-trade.json 是「逐月一筆」，這份是
# 「逐年一筆，年內再拆 21 類」。一樣是人工查詢後手動維護，不是自動抓取。
#
# 格式（單位：千美元，年份用西元年）：
# {
#   "sections": {"01": "活動物；動物產品", "02": "植物產品", ...},  // HS 21 類官方名稱，GA30 查詢畫面下拉選單裡就有
#   "years": [
#     {
#       "year": "2026",
#       "period_label": "115年1-7月（初步值）",
#       "partial": true,            // 是否為未滿一整年的資料
#       "months_covered": 7,        // 涵蓋幾個月，用來判斷能不能跟另一年比較年增率
#       "exports_by_section": {"01": 123, "02": 456, ...},
#       "imports_by_section": {"01": 78, ...}
#     },
#     ...
#   ]
# }
TW_TR_SECTORS_PATH = DATA_DIR / "tw-tr-trade-sectors.json"


def load_tw_tr_sectors() -> dict | None:
    if not TW_TR_SECTORS_PATH.exists():
        return None
    try:
        return json.loads(TW_TR_SECTORS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! {TW_TR_SECTORS_PATH.name} 讀取失敗，略過：{e}")
        return None


# ── Türkiye 貿易：前五大產業「逐月」變化（跟 tw-tr-trade-sectors.json
#    不同，那份是「逐年一筆、年內累計」；這份是「逐月一筆、不累計」）──
# 格式（單位：千美元）：
# {
#   "months": [
#     {"month": "2026-01", "exports_by_section": {"01": ..., ...}, "imports_by_section": {...}},
#     ...
#   ]
# }
TW_TR_MONTHLY_PATH = DATA_DIR / "tw-tr-trade-sectors-monthly.json"


def load_tw_tr_monthly() -> dict | None:
    if not TW_TR_MONTHLY_PATH.exists():
        return None
    try:
        return json.loads(TW_TR_MONTHLY_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"! {TW_TR_MONTHLY_PATH.name} 讀取失敗，略過：{e}")
        return None


def _tw_tr_top5(direction_key: str, latest: dict, prev: dict | None,
                 sections: dict, comparable: bool) -> tuple[list[dict], int]:
    """算某一年（latest）某個方向（出口或進口）的前五大類別排名。
    年增率只在 comparable=True（跟上一筆資料涵蓋的月數一致）時才計算，
    否則整批都回傳 None，前端顯示「—」，不要拿「7個月」跟「12個月」
    硬算出一個看似合理、實則誤導的成長率。"""
    by_sec = latest.get(direction_key) or {}
    total = sum(by_sec.values())
    ranked = sorted(by_sec.items(), key=lambda kv: kv[1], reverse=True)[:5]
    rows = []
    for code, val in ranked:
        pct = (val / total * 100) if total else 0
        yoy = None
        if comparable and prev:
            prev_val = (prev.get(direction_key) or {}).get(code)
            if prev_val:
                yoy = (val - prev_val) / prev_val * 100
        rows.append({
            "code": code,
            "name": sections.get(code, f"第{code}類"),
            "value": val,
            "share_pct": pct,
            "yoy_pct": yoy,
        })
    return rows, total


TW_TR_CHART_DIR = ROOT / "docs" / "images"
TW_TR_CHART_FILENAME = "tw-tr-annual-chart.png"

_CJK_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
_CJK_FONT_FAMILY = "Noto Sans CJK JP"  # 這個 .ttc 檔案登記的字型家族名稱是
                                        # "JP"，即使拿來畫繁體中文也是用這個
                                        # 名字取用，親測過不會變成方框亂碼。


def render_tw_tr_annual_chart(sectors_data: dict | None) -> str:
    """年度出口／進口／貿易餘額趨勢圖。

    2026-09 從 Chart.js（瀏覽器端 JavaScript 畫圖）改成伺服器端用
    matplotlib 直接畫成一張 PNG 存檔、用 <img> 嵌入——不再需要瀏覽器執行
    任何 JS 才看得到圖，徹底避開「Chart.js 到底有沒有正確載入」這種難以
    排查的不確定性，做法上更接近網站其他部分「先算好、存成靜態內容」的
    一貫風格。

    只畫「全年度」資料（111–114年），不含當年度還沒滿一年的 115年1-7月
    ——一根只有 7 個月的短棒夾在四根全年的長棒中間，會讓人誤以為貿易額
    暴跌，其實只是月份數不一樣（跟表 02 貿易差額那邊踩過的同一個坑）。
    當年度的進度另外由上面的三張指標卡跟前五大產業表呈現。
    """
    years = (sectors_data or {}).get("years") or []
    years = [y for y in years if not y.get("comparison_only") and not y.get("partial")]
    if not years:
        return ""

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm

        if Path(_CJK_FONT_PATH).exists():
            fm.fontManager.addfont(_CJK_FONT_PATH)
            plt.rcParams["font.sans-serif"] = [_CJK_FONT_FAMILY]
        plt.rcParams["axes.unicode_minus"] = False

        labels = [y.get("period_label") or y.get("year", "") for y in years]
        exp_vals = [round(sum((y.get("exports_by_section") or {}).values()) / 1000, 1) for y in years]
        imp_vals = [round(sum((y.get("imports_by_section") or {}).values()) / 1000, 1) for y in years]
        bal_vals = [round(e - i, 1) for e, i in zip(exp_vals, imp_vals)]

        fig, ax1 = plt.subplots(figsize=(10, 5.8), dpi=150)
        x = range(len(labels))
        width = 0.35

        bars1 = ax1.bar([i - width / 2 for i in x], exp_vals, width,
                         label="出口金額（百萬美元）", color="#3b82f6")
        bars2 = ax1.bar([i + width / 2 for i in x], imp_vals, width,
                         label="進口金額（百萬美元）", color="#f59e0b")
        ax1.set_ylabel("進出口金額（百萬美元）", fontsize=11, fontweight="bold")
        ax1.set_xticks(list(x))
        ax1.set_xticklabels(labels, fontsize=10)
        ax1.set_ylim(0, max(exp_vals) * 1.25 if exp_vals else 1)

        for b in list(bars1) + list(bars2):
            ax1.annotate(f"${b.get_height():.1f}M", (b.get_x() + b.get_width() / 2, b.get_height()),
                         textcoords="offset points", xytext=(0, 4), ha="center", fontsize=9)

        ax2 = ax1.twinx()
        ax2.plot(list(x), bal_vals, color="#16a34a", marker="o", linewidth=2.5,
                 markersize=7, label="貿易餘額／出超（百萬美元）")
        ax2.set_ylabel("貿易餘額 出超（百萬美元）", fontsize=11, fontweight="bold", color="#16a34a")
        ax2.tick_params(axis="y", labelcolor="#16a34a")
        ax2.set_ylim(0, max(bal_vals) * 1.6 if bal_vals else 1)
        for xi, v in zip(x, bal_vals):
            ax2.annotate(f"${v:.1f}M", (xi, v), textcoords="offset points", xytext=(0, 10),
                         ha="center", fontsize=9, color="#16a34a", fontweight="bold")

        lines1, lbls1 = ax1.get_legend_handles_labels()
        lines2, lbls2 = ax2.get_legend_handles_labels()
        ax1.legend(lines1 + lines2, lbls1 + lbls2, loc="upper right", fontsize=9, framealpha=0.9)

        year_range = f"{labels[0]}—{labels[-1]}" if len(labels) > 1 else labels[0]
        plt.title(f"{year_range} 台灣對 Türkiye 貿易走勢與貿易餘額變化圖",
                  fontsize=13, fontweight="bold", pad=12)
        plt.tight_layout()

        TW_TR_CHART_DIR.mkdir(parents=True, exist_ok=True)
        out_path = TW_TR_CHART_DIR / TW_TR_CHART_FILENAME
        plt.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
    except Exception as e:
        print(f"! 年度趨勢圖產生失敗，略過此圖表：{e}")
        return ""

    # 圖片路徑要用絕對路徑（帶 SITE_BASE_PATH），理由跟 taitra-logo.png
    # 那次踩過的坑一樣：docs/archive/*.html 是整份頁面的快照，比
    # docs/index.html 多一層目錄，相對路徑在那邊會抓不到圖檔。
    img_src = f"{SITE_BASE_PATH}/images/{TW_TR_CHART_FILENAME}"
    return f'''    <div style="margin-top: 1.25rem; margin-bottom: 0.5rem;">
      <img src="{img_src}" alt="{html.escape(year_range)} 台灣對 Türkiye 貿易走勢圖"
           style="max-width: 100%; height: auto; display: block; margin: 0 auto;">
    </div>'''


TW_TR_MONTHLY_CHART_FILENAME = "tw-tr-monthly-top5-chart.png"
_TW_TR_LINE_COLORS = ["#3b82f6", "#f59e0b", "#16a34a", "#dc2626", "#8b5cf6"]


def render_tw_tr_monthly_sector_chart(monthly_data: dict | None, sections: dict) -> str:
    """出口前五大產業「逐月」變化折線圖（跟 render_tw_tr_sector_table()
    的排名表不同——那張只看單一期間的排名快照，這張看的是每個月怎麼
    變化）。前五大的認定方式：把 tw-tr-trade-sectors-monthly.json 裡所有
    月份的出口金額加總，取加總後最大的 5 個類別，確保跟排名表用同一套
    「前五大」定義，兩處不會兜不起來。"""
    months = (monthly_data or {}).get("months") or []
    if not months:
        return ""

    totals = {}
    for m in months:
        for code, val in (m.get("exports_by_section") or {}).items():
            totals[code] = totals.get(code, 0) + val
    top5_codes = sorted(totals, key=totals.get, reverse=True)[:5]
    if not top5_codes:
        return ""

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.font_manager as fm

        if Path(_CJK_FONT_PATH).exists():
            fm.fontManager.addfont(_CJK_FONT_PATH)
            plt.rcParams["font.sans-serif"] = [_CJK_FONT_FAMILY]
        plt.rcParams["axes.unicode_minus"] = False

        month_labels = [m["month"] for m in months]
        x = range(len(month_labels))

        fig, ax = plt.subplots(figsize=(10, 5.8), dpi=150)
        for i, code in enumerate(top5_codes):
            vals = [round((m.get("exports_by_section") or {}).get(code, 0) / 1000, 1) for m in months]
            name = sections.get(code, f"第{code}類")
            short_name = name.split("；")[0].split("，")[0]  # 官方名稱很長，圖例只取分號前的第一段
            ax.plot(list(x), vals, marker="o", linewidth=2.2, markersize=6,
                    color=_TW_TR_LINE_COLORS[i % len(_TW_TR_LINE_COLORS)],
                    label=f"第{code}類 {short_name}")

        ax.set_xticks(list(x))
        ax.set_xticklabels([f"{int(ml.split('-')[1])}月" for ml in month_labels], fontsize=10)
        ax.set_ylabel("出口金額（百萬美元）", fontsize=11, fontweight="bold")
        ax.legend(loc="upper left", fontsize=8.5, framealpha=0.9)
        ax.grid(axis="y", linestyle="--", alpha=0.4)

        period = f"{month_labels[0]} ～ {month_labels[-1]}"
        plt.title(f"台灣出口 Türkiye 前五大產業逐月變化（{period}）",
                  fontsize=13, fontweight="bold", pad=12)
        plt.tight_layout()

        TW_TR_CHART_DIR.mkdir(parents=True, exist_ok=True)
        out_path = TW_TR_CHART_DIR / TW_TR_MONTHLY_CHART_FILENAME
        plt.savefig(out_path, dpi=150, bbox_inches="tight")
        plt.close(fig)
    except Exception as e:
        print(f"! 前五大產業逐月變化圖產生失敗，略過此圖表：{e}")
        return ""

    img_src = f"{SITE_BASE_PATH}/images/{TW_TR_MONTHLY_CHART_FILENAME}"
    return f'''    <div style="margin-top: 1rem; margin-bottom: 0.5rem;">
      <img src="{img_src}" alt="台灣出口 Türkiye 前五大產業逐月變化"
           style="max-width: 100%; height: auto; display: block; margin: 0 auto;">
    </div>'''


def render_tw_tr_sector_table(sectors_data: dict | None) -> str:
    """出口前五大 HS 類別排名表，放在「06 台灣—Türkiye 雙邊貿易」卡片裡，
    緊接在貿易出超那三張指標卡下面。"""
    years = (sectors_data or {}).get("years") or []
    if not years:
        return ""
    sections = sectors_data.get("sections") or {}
    latest = years[-1]
    prev = years[-2] if len(years) >= 2 else None
    comparable = (
        prev is not None
        and prev.get("partial") == latest.get("partial")
        and prev.get("months_covered") == latest.get("months_covered")
    )
    rows, _total = _tw_tr_top5("exports_by_section", latest, prev, sections, comparable)
    if not rows:
        return ""

    period_label = html.escape(latest.get("period_label") or latest.get("year", ""))
    trs = []
    for i, r in enumerate(rows, 1):
        name = html.escape(r["name"])
        if r["yoy_pct"] is None:
            yoy_html = '<span class="flat">—</span>'
        else:
            cls = "up" if r["yoy_pct"] > 0 else ("down" if r["yoy_pct"] < 0 else "flat")
            yoy_html = f'<span class="{cls}">{r["yoy_pct"]:+.1f}%</span>'
        trs.append(f'''          <tr>
            <td>{i}</td>
            <td>第{r["code"]}類</td>
            <td>{name}</td>
            <td>{r["value"]:,} 千美元</td>
            <td>{r["share_pct"]:.2f}%</td>
            <td>{yoy_html}</td>
          </tr>''')

    yoy_note = "" if comparable else (
        f'<p class="grp-note" style="margin-top:0.4rem;">'
        f'年增率因對比期間資料涵蓋月份不一致（例如今年僅到 {latest.get("months_covered", "?")} 月、'
        f'去年為全年 12 個月），無法公平比較，暫顯示「—」；如需完整年增率，'
        f'請額外查詢去年同期（1 月至 {latest.get("months_covered", "?")} 月）資料補齊。</p>'
    )

    return f'''    <p class="grp-note" style="margin-top:1.1rem; margin-bottom:0.4rem; font-weight:600;">
      台灣出口 Türkiye 前五大產業（{period_label}）</p>
    <div class="table-wrapper">
      <table class="tbl-narrow">
        <thead>
          <tr>
            <th>排名</th><th>貨品號列</th><th>產業別</th>
            <th>累計出口金額</th><th>出口占比</th><th>年增率</th>
          </tr>
        </thead>
        <tbody>
{chr(10).join(trs)}
        </tbody>
      </table>
    </div>
    {yoy_note}'''


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
        href = html.escape(f"{SITE_BASE_PATH}/reports/{e.get('file','')}", quote=True)
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

    def _quarter_label(date_str: str) -> str:
        """EVDS 季度序列的日期字串沒實機驗證過確切格式，寬鬆解析
        「YYYY-MM」開頭的部分，用月份推回是第幾季（常見做法是用該季
        最後一個月當日期戳，例如 Q2 標成 2026-06）；解析不出來就直接
        顯示原始字串，不要讓整段掛掉或算出錯的季別。"""
        if not date_str:
            return "最新"
        m = re.match(r"^(\d{4})-(\d{1,2})", date_str)
        if m:
            year, month = int(m.group(1)), int(m.group(2))
            q = (month - 1) // 3 + 1
            return f"{year} Q{q}"
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

    # 貿易差額 = 出口 - 進口。
    # 2026-09 發現：EVDS「出口/進口」時間序列跟貿易部/TÜİK 官方新聞稿公布
    # 的月度數字對不起來（差距達 6 成以上，不像單純的月份落後可以解釋，
    # 較可能是統計口徑不同——EVDS 序列與貿易部「一般貿易系統(GTS)」新聞
    # 稿用的可能是不同版本/計價基礎）。與其冒著顯示錯誤數字的風險，改成
    # 跟 06 台灣—Türkiye 雙邊貿易（tw-tr-trade.json）同一套做法：每月人工
    # 對照貿易部新聞稿（ticaret.gov.tr/istatistikler/dis-ticaret-istatistikleri）
    # 手動填一次 data/turkey-trade-manual.json，不再依賴 EVDS 這兩個序列。
    turkey_trade_data = load_turkey_trade_manual()
    tt_months = (turkey_trade_data or {}).get("months") or []

    if tt_months:
        tt_latest = tt_months[-1]
        tt_exp, tt_imp = tt_latest.get("exports"), tt_latest.get("imports")
        tt_month = tt_latest.get("month", "")
        tb_month_label = _month_label(tt_month)
        if tt_exp is not None and tt_imp is not None:
            tt_bal = tt_exp - tt_imp
            tb_label = "順差" if tt_bal >= 0 else "逆差"
            tb_value = f"{abs(tt_bal) / 100:.1f} 億美元"
            tb_color = "green" if tt_bal >= 0 else "red"
        else:
            tb_value, tb_label, tb_color = "—", "", "amber"
            tt_bal = None
        if len(tt_months) >= 2:
            tt_prev = tt_months[-2]
            p_exp, p_imp = tt_prev.get("exports"), tt_prev.get("imports")
            if p_exp is not None and p_imp is not None and tt_bal is not None:
                p_bal = p_exp - p_imp
                p_label = "順差" if p_bal >= 0 else "逆差"
                tb_delta = f"上月：{abs(p_bal) / 100:.1f} 億美元{p_label}　（資料月份：{tt_month}）"
            else:
                tb_delta = f"上月資料不完整　（資料月份：{tt_month}）"
        else:
            tb_delta = f"尚無前一期資料可比較　（資料月份：{tt_month}）"
    else:
        tb_value, tb_label, tb_color = "—", "", "amber"
        tb_month_label = "—"
        tb_delta = (
            "尚未提供 data/turkey-trade-manual.json——請對照貿易部新聞稿"
            "（ticaret.gov.tr/istatistikler/dis-ticaret-istatistikleri）手動填入"
        )

    # Türkiye 本年度累計出口／進口——直接加總 turkey_trade_data 裡「今年」
    # 已經有的月份，不假設 1 月到最新月都填齊，標籤誠實反映實際加總的
    # 月份範圍（跟 06 台灣—Türkiye 雙邊貿易卡片「年初至今累計」用的是
    # 同一套邏輯）。這裡只是加總既有的 exports/imports 欄位，沒有引入
    # 新的資料來源，你原本每月填一筆的維護方式不用變。
    if tt_months:
        cur_year_prefix = tt_months[-1].get("month", "")[:4]
        ytd_tt_months = sorted(
            (m for m in tt_months if m.get("month", "").startswith(f"{cur_year_prefix}-")),
            key=lambda m: m["month"],
        )
    else:
        ytd_tt_months = []

    if ytd_tt_months:
        _ytd_exp_sum = sum((m.get("exports") or 0) for m in ytd_tt_months)
        _ytd_imp_sum = sum((m.get("imports") or 0) for m in ytd_tt_months)
        _first_m = int(ytd_tt_months[0]["month"].split("-")[1])
        _last_m = int(ytd_tt_months[-1]["month"].split("-")[1])
        ytd_period_label = (
            f"{cur_year_prefix}年{_last_m}月" if _first_m == _last_m
            else f"{cur_year_prefix}年{_first_m}-{_last_m}月累計"
        )
        export_ytd_value = f"{_ytd_exp_sum / 100:.1f} 億美元"
        import_ytd_value = f"{_ytd_imp_sum / 100:.1f} 億美元"
    else:
        ytd_period_label = "—"
        export_ytd_value = import_ytd_value = "—"

    # 「核心指標」第三格：2026-09 從「台灣—Türkiye 雙邊貿易餘額」換成
    # 「土耳其季度 GDP 成長率」——前兩格本來就是土耳其總經數據（CPI、
    # 政策利率），雙邊貿易餘額放這裡風格上比較跳；雙邊貿易的完整版本
    # 本來就在下面「06 台灣—Türkiye 雙邊貿易」那張卡片，拿掉這裡不影響
    # 資訊完整性。
    #
    # 資料來源：EVDS「TP.GSYIH20.CY.B1GQ」（TÜİK 支出面法、鏈式不變價格
    # 季度 GDP，未經季節調整）算年增率——跟去年同一季比，季節性因素會
    # 互相抵銷，2026-09-08 用官方新聞稿核對過 2026 Q2 算出 +2.3%，跟
    # TÜİK 公布的數字一致。
    #
    # 季增率改用另一條序列「TP.GSYIH30.HY.B1GQ」（季節與工作日調整後
    # 版本）——2026-09-08 一開始直接拿上面那條未調整序列算季增率，
    # 算出 +7.2%，但 TÜİK 官方公布的季增率其實是 +1.1%，差了 6 倍，
    # 因為土耳其 GDP 季節性明顯（Q1 傳統低、Q2/Q3 回溫是常態），未調整
    # 序列的季增率會把季節性誤判成真實成長。換成這條調整後序列重算，
    # 2026 Q2 (245.18) vs 2026 Q1 (242.47) 得到 +1.1%，跟官方一致——
    # 這也是 TÜİK 自己的算法慣例：年增率用原始序列、季增率用調整後
    # 序列，兩條序列分工明確，不要混用（例如不要拿調整後序列算年增
    # 率，官方公布年增率時本來就是用原始序列算的）。
    gdp_series = payload.get("macro", {}).get("gdp_growth") or []
    gdp_vals = [e["value"] for e in gdp_series if "value" in e]
    gdp_sa_series = payload.get("macro", {}).get("gdp_growth_sa") or []
    gdp_sa_vals = [e["value"] for e in gdp_sa_series if "value" in e]

    def _series_change(vals: list, back: int):
        j = -1 - back
        if len(vals) >= abs(j) and vals[j]:
            return (vals[-1] - vals[j]) / vals[j] * 100
        return None

    gdp_yoy = _series_change(gdp_vals, 4) if gdp_vals else None
    gdp_qoq = _series_change(gdp_sa_vals, 1) if gdp_sa_vals else None
    gdp_quarter_label = _quarter_label(gdp_series[-1].get("date", "")) if gdp_series else "—"

    # 2026-09 改成跟隔壁「政策利率 / 隔夜拆借」卡片同樣的格式並排顯示
    # （年增 / 季增），token 本身不含 % 符號，% 寫死在範本裡跟在 token
    # 後面——寫法要跟 {{FUNDING_COST}} 那組一致，不要自己在這裡加 %。
    if gdp_yoy is not None:
        gdp_value = f"{gdp_yoy:+.1f}"
        gdp_color = "green" if gdp_yoy >= 0 else "red"
    else:
        gdp_value, gdp_color = "—", "amber"

    gdp_qoq_value = f"{gdp_qoq:+.1f}" if gdp_qoq is not None else "—"

    if gdp_yoy is not None and gdp_qoq is not None:
        gdp_delta = "年增／季增（季增已經季節調整）"
    elif gdp_yoy is not None:
        gdp_delta = "季增率資料尚未取得（gdp_growth_sa 序列抓取失敗）"
    elif gdp_vals or gdp_sa_vals:
        gdp_delta = "尚無足夠歷史資料計算年增／季增率"
    else:
        gdp_delta = "資料尚未取得（尚未設定 EVDS_API_KEY 或序列抓取失敗）"

    # 台灣—Türkiye 雙邊貿易的資料照樣讀進來（下面 06 那張卡片、跟這裡
    # 拿掉的 twtr_* 核心指標卡是兩回事，不要一起刪掉）。
    tw_tr_data_early = load_tw_tr_trade()

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

    # 第 5、6、7 列：貿易差額、出口、涵蓋率。
    # 改用上面 turkey_trade_data／tt_months（人工維護，見該處註解說明原因），
    # 不再用 EVDS 的 exp_vals/imp_vals。年增率優先用新聞稿裡直接寫的數字
    # （exports_yoy_pct 等），沒有才留 —，不用自己算（手動資料要累積 13
    # 個月才夠算年增率，而且官方數字通常比自算的更準）。
    if tt_months:
        cur_exp = tt_latest.get("exports")
        cur_imp = tt_latest.get("imports")
        cur_cov = tt_latest.get("coverage_pct")
        prev_month = tt_months[-2] if len(tt_months) >= 2 else None

        if cur_exp is not None and cur_imp is not None:
            cur_bal = cur_exp - cur_imp
            tb_row_name = "月貿易順差" if cur_bal >= 0 else "月貿易逆差"
            tb_row_value = f"{abs(cur_bal) / 100:.1f} 億美元"
            if prev_month is not None:
                p_exp, p_imp = prev_month.get("exports"), prev_month.get("imports")
                if p_exp is not None and p_imp is not None:
                    d = (abs(cur_bal) - abs(p_exp - p_imp)) / 100
                    tb_row_delta = f"{d:+.1f} 億".replace("-", "−")
                    tb_row_delta_cls = "up" if d > 0 else ("down" if d < 0 else "flat")
                else:
                    tb_row_delta, tb_row_delta_cls = "—", "flat"
            else:
                tb_row_delta, tb_row_delta_cls = "—", "flat"
            balance_yoy = tt_latest.get("balance_yoy_pct")
            tb_row_yoy, tb_row_yoy_cls = _pct(balance_yoy), _delta_cls(balance_yoy)
        else:
            tb_row_name = "月貿易差額"
            tb_row_value = tb_row_delta = tb_row_yoy = "—"
            tb_row_delta_cls = tb_row_yoy_cls = "flat"

        if cur_exp is not None:
            export_row_value = f"{cur_exp / 100:.1f} 億美元"
            if prev_month is not None and prev_month.get("exports") is not None:
                d = (cur_exp - prev_month["exports"]) / 100
                export_row_delta = f"{d:+.1f} 億".replace("-", "−")
                export_row_delta_cls = "up" if d > 0 else ("down" if d < 0 else "flat")
            else:
                export_row_delta, export_row_delta_cls = "—", "flat"
            exports_yoy = tt_latest.get("exports_yoy_pct")
            export_row_yoy, export_row_yoy_cls = _pct(exports_yoy), _delta_cls(exports_yoy)
        else:
            export_row_value = export_row_delta = export_row_yoy = "—"
            export_row_delta_cls = export_row_yoy_cls = "flat"

        if cur_imp is not None:
            import_row_value = f"{cur_imp / 100:.1f} 億美元"
            if prev_month is not None and prev_month.get("imports") is not None:
                d = (cur_imp - prev_month["imports"]) / 100
                import_row_delta = f"{d:+.1f} 億".replace("-", "−")
                import_row_delta_cls = "up" if d > 0 else ("down" if d < 0 else "flat")
            else:
                import_row_delta, import_row_delta_cls = "—", "flat"
            imports_yoy = tt_latest.get("imports_yoy_pct")
            import_row_yoy, import_row_yoy_cls = _pct(imports_yoy), _delta_cls(imports_yoy)
        else:
            import_row_value = import_row_delta = import_row_yoy = "—"
            import_row_delta_cls = import_row_yoy_cls = "flat"

        if cur_cov is not None:
            coverage_value = f"{cur_cov:.1f}%"
            prev_cov = prev_month.get("coverage_pct") if prev_month is not None else None
            pp = (cur_cov - prev_cov) if prev_cov is not None else None
            coverage_delta, coverage_delta_cls = _pp(pp), _delta_cls(pp)
            # 涵蓋率的年增率貿易部新聞稿沒有直接給，沒有 13 個月人工資料
            # 累積起來之前就先留 —，不硬算。
            coverage_yoy, coverage_yoy_cls = "—", "flat"
        else:
            coverage_value = coverage_delta = coverage_yoy = "—"
            coverage_delta_cls = coverage_yoy_cls = "flat"
    else:
        tb_row_name = "月貿易差額"
        tb_row_value = tb_row_delta = tb_row_yoy = "—"
        tb_row_delta_cls = tb_row_yoy_cls = "flat"
        export_row_value = export_row_delta = export_row_yoy = "—"
        export_row_delta_cls = export_row_yoy_cls = "flat"
        import_row_value = import_row_delta = import_row_yoy = "—"
        import_row_delta_cls = import_row_yoy_cls = "flat"
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
        "{{GDP_VALUE}}": gdp_value,
        "{{GDP_QOQ_VALUE}}": gdp_qoq_value,
        "{{GDP_COLOR}}": gdp_color,
        "{{GDP_QUARTER_LABEL}}": gdp_quarter_label,
        "{{GDP_DELTA}}": gdp_delta,
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
        "{{IMPORT_ROW_VALUE}}": import_row_value,
        "{{IMPORT_ROW_DELTA}}": import_row_delta,
        "{{IMPORT_ROW_DELTA_CLS}}": import_row_delta_cls,
        "{{IMPORT_ROW_YOY}}": import_row_yoy,
        "{{IMPORT_ROW_YOY_CLS}}": import_row_yoy_cls,
        "{{EXPORT_YTD_VALUE}}": export_ytd_value,
        "{{IMPORT_YTD_VALUE}}": import_ytd_value,
        "{{YTD_PERIOD_LABEL}}": ytd_period_label,
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
    tw_tr_sectors_data = load_tw_tr_sectors()
    tw_tr_monthly_data = load_tw_tr_monthly()
    html_text = replace_block(
        html_text, "TW_TR_TRADE",
        render_tw_tr_trade(tw_tr_data, tw_tr_sectors_data, tw_tr_monthly_data)
    )

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
