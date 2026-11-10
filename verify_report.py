#!/usr/bin/env python3
"""
verify_report.py -- 發布前檢查閘門（publish gate）

用法（GitHub Actions 裡，render 到暫存區之後、複製到 docs/ 之前執行）：
    python verify_report.py --html build/index.html [--date 2026-11-10] [--alert]

任何一項沒過 -> 結束碼 1（workflow 就不發布），並把原因寫進
build/verify-report.md（當 GitHub Issue 內文）與 data/YYYY-MM-DD-verify.json（追溯紀錄）。

檢查項目
 1. 新鮮度      ：當天原始資料與 AI 分析檔存在、日期一致、不是舊分析
 2. 數字可追溯  ：AI 寫的每一個數字，都要能在原始 JSON（TCMB 匯率、EVDS 總經、
                  Brent/EIA 油價、新聞原文、人工指標檔）找到來源；找不到 = 可能是模型編的
 3. 事件有來源  ：每個重點事件、產業動態都要有 http(s) 連結，且該連結必須是
                  當天原始資料裡真的有的新聞網址（防止模型編網址）；產業動態的
                  日期要跟那則新聞的發布日一致
 4. 簡體字掃描  ：用 OpenCC 掃描最終 HTML 可見文字（簡體字、大陸用語）
 5. 頁面完整性  ：沒有殘留 {{TOKEN}}、沒有「資料缺漏」、匯率與融資成本確實寫進頁面、
                  AI 區塊沒有被跳過

數字比對規則（刻意寫成可預測、可解釋，不用 AI 判斷 AI）
 - 允許單位換算：百萬美元 -> 億美元、$300B -> 3000 億美元、小數 -> 百分比
 - 允許「同一序列相鄰兩期」的差與漲跌幅（例如利率變動 pp、指數月增率）
 - 不允許模型自己湊出來的數字（其他加減乘除）
 - 免檢：年份、日期、第N類/N則等計數、緊貼英文字母的代號（COP31、G20）、
         沒有單位的 100 以下整數
 - 例外請登記在 verify_allowlist.json（{"numbers": [29.77, ...]}），要有人確認過才能放行
"""

import argparse
import datetime as dt
import html as htmllib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")

# 這些欄位的內容是網址 / 時間戳 / 代號，裡面的數字不是「事實」，不放進可追溯數字庫
SKIP_KEYS = {"url", "link", "id", "published", "date", "date_raw", "bulletin", "generated_at",
             "generated_at_label", "source", "source_url", "source_kind", "no_topic_samples",
             "base_date", "previous_date", "changes_anchor_date", "_generated_at", "_model",
             "_source_date", "month", "period", "status"}
SKIP_TOP = {"news_diagnostics", "errors"}

# 新聞原文裡的數字單位（用來把 "$300 billion" 換成 3e11 去比對 "3000 億美元"）
TEXT_UNITS = [
    (re.compile(r"^\s?(trilyon|trillion|tn)\b", re.I), 1e12),
    (re.compile(r"^\s?(milyar|billion|bn|b)\b", re.I), 1e9),
    (re.compile(r"^\s?(milyon|million|mn|m)\b", re.I), 1e6),
    (re.compile(r"^\s?(bin|thousand|k)\b", re.I), 1e3),
    (re.compile(r"^\s?(兆)"), 1e12),
    (re.compile(r"^\s?(千億)"), 1e11),
    (re.compile(r"^\s?(百億)"), 1e10),
    (re.compile(r"^\s?(億)"), 1e8),
    (re.compile(r"^\s?(千萬)"), 1e7),
    (re.compile(r"^\s?(百萬)"), 1e6),
    (re.compile(r"^\s?(萬)"), 1e4),
]

NUM_RE = re.compile(r"(?<![A-Za-z0-9_.])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?")

# 大陸用語（用的是繁體字，OpenCC 字元層級抓不到，所以另外列；只列沒有歧義的）
MAINLAND_TERMS = ["軟件", "硬件", "視頻", "網絡", "默認", "激光", "打印機", "鼠標", "U盤",
                  "這里", "那里", "哪里", "里面"]

# OpenCC 字元層級會把這幾個「繁體裡本來就合法」的字當成簡體（台灣、里拉、占比）。
# 預設放行；真正的簡體誤用（這里、里面）由上面的詞組清單另外抓。
DEFAULT_WHITELIST = set("台里占")


# ───────────────────────── 讀檔 ─────────────────────────
def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def resolve_date(date_str):
    if date_str:
        return date_str
    files = sorted(f for f in DATA_DIR.glob("*.json") if DATE_RE.match(f.name))
    if not files:
        raise SystemExit("data/ 裡沒有任何日期格式的 JSON")
    return files[-1].stem


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style).*?</\1>", " ", h)
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    return re.sub(r"\s+", " ", htmllib.unescape(h)).strip()


def strip_ai_blocks(h: str) -> str:
    return re.sub(r"(?s)<!-- AI:(\w+):START.*?-->.*?<!-- AI:\1:END.*?-->", " ", h)


# ───────────────────────── 可追溯數字庫 ─────────────────────────
def norm_url(u: str) -> str:
    u = (u or "").strip()
    u = re.sub(r"[?#].*$", "", u)
    return u.rstrip("/").lower()


COMPOUND_RE = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(milyar|billion|bn)\s+(\d+(?:[.,]\d+)?)\s*(milyon|million|mn)\b"
    r"|(\d+(?:[.,]\d+)?)\s*(milyon|million|mn)\s+(\d+(?:[.,]\d+)?)\s*(bin|thousand)\b", re.I)


def numbers_in_text(text: str):
    """從一段文字抽出所有數字（含單位換算後的版本、逗號小數與千分位兩種讀法）。
    土耳其媒體常寫「4 milyar 32 milyon dolar」= 40.32 億，這種複合寫法先合併。"""
    out = []
    for cm in COMPOUND_RE.finditer(text):
        g = cm.groups()
        a, ua, b, ub = (g[0], g[1], g[2], g[3]) if g[0] else (g[4], g[5], g[6], g[7])
        big = {"milyar": 1e9, "billion": 1e9, "bn": 1e9, "milyon": 1e6, "million": 1e6, "mn": 1e6}
        small = {"milyon": 1e6, "million": 1e6, "mn": 1e6, "bin": 1e3, "thousand": 1e3}
        fa, fb = float(a.replace(",", ".")), float(b.replace(",", "."))
        out.append(fa * big[ua.lower()] + fb * small[ub.lower()])
    for m in NUM_RE.finditer(text):
        raw_int, frac = m.group(1), m.group(2) or ""
        readings = []
        if "," in raw_int:
            readings.append(float(raw_int.replace(",", "") + frac))
        else:
            readings.append(float(raw_int + frac))
        # 土耳其寫法：29,77 = 29.77、1,5 = 1.5（逗號後 1–2 位）
        after = text[m.end(): m.end() + 3]
        cm = re.match(r",(\d{1,2})(?!\d)", after)
        if cm and not frac and "," not in raw_int:
            readings.append(float(f"{raw_int}.{cm.group(1)}"))
        tail = text[m.end(): m.end() + 14]
        mult = [1.0]
        for rx, k in TEXT_UNITS:
            if rx.match(tail):
                mult.append(k)
                break
        for r in readings:
            for k in mult:
                out.append(r * k)
    return out


def news_item_numbers(n: dict):
    """AI 實際看到的新聞內容 = 標題 + 摘要前 300 字（見 generate_analysis.build_user_content）。"""
    text = f"{n.get('title', '')} {(n.get('summary') or '')[:300]}"
    lab = f"新聞「{(n.get('title') or '')[:30]}」（{n.get('source', '')}）"
    return [(abs(v), lab) for v in numbers_in_text(text)]


def build_truth(payload: dict, allow: list):
    """回傳 (結構化資料數字庫, {新聞網址: 該則新聞數字}, 全部入選新聞的數字庫)。

    結構化資料 = 匯率（TCMB）、總經序列（EVDS）、油價（Brent／EIA）…等原始 JSON 的數值；
    新聞數字則分開管：事件裡的數字只准對回「它引用的那則新聞」，不能拿別則新聞湊數，
    否則 140 則新聞的數字太多，隨便編一個小數也會碰巧對上。"""
    truth = []

    def add(v, label):
        try:
            truth.append((abs(float(v)), label))
        except (TypeError, ValueError):
            pass

    def walk(obj, path):
        if isinstance(obj, bool):
            return
        if isinstance(obj, (int, float)):
            add(obj, path)
        elif isinstance(obj, list):
            for i, x in enumerate(obj):
                walk(x, f"{path}[{i}]")
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if k in SKIP_KEYS:
                    continue
                walk(v, f"{path}.{k}")

    for k, v in payload.items():
        if k in SKIP_TOP or k == "news":
            continue
        walk(v, f"payload.{k}")

    # 序列的相鄰兩期差與漲跌幅（利率變動 pp、指數月增率…）
    for k, series in (payload.get("macro") or {}).items():
        vals = [p.get("value") for p in series if isinstance(p, dict) and isinstance(p.get("value"), (int, float))] \
            if isinstance(series, list) else []
        for i in range(max(1, len(vals) - 4), len(vals)):
            a, b = vals[i], vals[i - 1]
            add(a - b, f"payload.macro.{k}:相鄰差")
            if b:
                add((a / b - 1) * 100, f"payload.macro.{k}:相鄰漲跌幅%")
        if len(vals) > 12 and vals[-13]:
            add((vals[-1] / vals[-13] - 1) * 100, f"payload.macro.{k}:對12期前%")
    for v in allow:
        add(v, "verify_allowlist.json（人工確認放行）")

    news = payload.get("news", [])
    per_url = {norm_url(n["url"]): news_item_numbers(n) for n in news if n.get("url")}
    domestic = [n for n in news if n.get("scope") == "domestic"]
    selected = domestic or news  # 與 generate_analysis.build_user_content 一致
    pool = []
    for n in selected:
        pool += news_item_numbers(n)
    return truth, per_url, pool


# ───────────────────────── AI 文字裡的數字 ─────────────────────────
UNIT_MULT = [("千億", 1e11), ("百億", 1e10), ("兆", 1e12), ("億", 1e8), ("千萬", 1e7),
             ("百萬", 1e6), ("萬", 1e4)]
EXEMPT_AFTER = re.compile(r"^\s?(年|月|日|號|季|週|周|天|個月|小時|分鐘|世紀)")
EXEMPT_RANGE_MONTH = re.compile(r"^\s?[–\-~～至到]\s?\d+\s?月")
COUNT_AFTER = re.compile(r"^\s?(則|項|家|位|名|次|場|筆|條|所|個|間|座|人|檔|期|類|章|款|張|件)")
CURRENCY_AFTER = re.compile(r"^\s?(美元|美金|里拉|歐元|TL|USD|EUR|基點|bp)", re.I)


def ai_tokens(text: str):
    """回傳要檢查的數字 token：dict(raw, value, decimals, mult, kind, ctx)"""
    res = []
    for m in NUM_RE.finditer(text):
        raw_int, frac = m.group(1), m.group(2) or ""
        raw = raw_int + frac
        v = float(raw_int.replace(",", "") + frac)
        d = len(frac) - 1 if frac else 0
        after = text[m.end(): m.end() + 8]
        before = text[max(0, m.start() - 2): m.start()]
        ctx = text[max(0, m.start() - 12): m.end() + 10]
        is_int = not frac
        if EXEMPT_AFTER.match(after) or EXEMPT_RANGE_MONTH.match(after):
            continue
        if before.endswith("第") or before.endswith("Q"):
            continue
        mult, unit_hit = 1.0, False
        for u, k in UNIT_MULT:
            if after.lstrip().startswith(u):
                mult, unit_hit = k, True
                break
        pct = bool(re.match(r"^\s?(%|％|個百分點|pp|百分點)", after, re.I))
        bp = bool(re.match(r"^\s?(基點|bp)", after, re.I))
        cur = bool(CURRENCY_AFTER.match(after))
        if is_int and COUNT_AFTER.match(after) and not (unit_hit or pct or bp or cur):
            continue
        if is_int and 1990 <= v <= 2100 and not (unit_hit or pct or bp or cur):
            continue
        if is_int and v < 100 and not (unit_hit or pct or bp or cur):
            continue
        res.append({"raw": raw, "value": v, "decimals": d, "mult": mult, "pct": pct,
                    "bp": bp, "ctx": ctx})
    return res


def match_token(tok, truth):
    v, d = tok["value"], tok["decimals"]
    tol_unit = 0.5 * 10 ** (-d)
    hits = []
    if tok["bp"]:
        target, tol = v / 100.0, tol_unit / 100.0 + 1e-9
        for x, lab in truth:
            if abs(x - target) <= tol:
                hits.append(lab)
            if len(hits) >= 3:
                break
        return hits
    target, tol = v * tok["mult"], tol_unit * tok["mult"] + 1e-9
    units = (1.0, 1e3, 1e6, 1e9) if tok["mult"] > 1 else (1.0,)
    for x, lab in truth:
        ok = any(abs(x * u - target) <= tol for u in units)
        if not ok and tok["pct"] and x < 1.5:
            ok = abs(x * 100 - target) <= tol
        if ok:
            hits.append(lab)
            if len(hits) >= 3:
                break
    return hits


# ───────────────────────── 各項檢查 ─────────────────────────
class Result:
    def __init__(self):
        self.checks = []  # (name, passed, [detail lines])
        self.warnings = {}  # name -> [提醒（不擋發布）]

    def add(self, name, problems, ok_note="", warnings=None):
        self.checks.append((name, not problems, problems or ([ok_note] if ok_note else [])))
        if warnings:
            self.warnings[name] = warnings

    @property
    def ok(self):
        return all(p for _, p, _ in self.checks)


def ai_text_fields(a: dict):
    """(欄位路徑, 文字)；HTML 標籤先拿掉再檢查。"""
    def clean(s):
        return html_to_text(s) if isinstance(s, str) else ""
    yield "today_take", clean(a.get("today_take"))
    yield "summary", clean(a.get("summary"))
    for i, e in enumerate(a.get("key_events", [])):
        for k in ("headline", "summary", "business_impact"):
            yield f"key_events[{i}].{k}", clean(e.get(k))
    for i, e in enumerate(a.get("industry_items", [])):
        for k in ("headline", "body", "business_interpretation"):
            yield f"industry_items[{i}].{k}", clean(e.get(k))
    for i, e in enumerate(a.get("trade_implications", [])):
        for k in ("title", "body"):
            yield f"trade_implications[{i}].{k}", clean(e.get(k))


def check_freshness(res, date, payload_path, analysis_path, analysis, max_age):
    probs = []
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()
    d = dt.date.fromisoformat(date)
    if (today - d).days > max_age:
        probs.append(f"原始資料日期 {date} 距今天（{today}）超過 {max_age} 天，抓資料步驟可能失敗")
    if not payload_path.exists():
        probs.append(f"找不到原始資料 {payload_path.name}")
    if analysis is None:
        probs.append(f"找不到今天的 AI 分析 {analysis_path.name}（Gemini 可能失敗，頁面會顯示舊分析）")
    else:
        if analysis.get("_source_date") not in (None, date):
            probs.append(f"AI 分析的 _source_date={analysis.get('_source_date')} 不等於 {date}")
        for k in ("today_take", "summary", "key_events", "industry_items", "trade_implications"):
            if k not in analysis:
                probs.append(f"AI 分析缺少欄位 {k}")
    res.add("1. 資料與分析是今天的", probs, f"原始資料與分析皆為 {date}")


def sig_digits(raw: str) -> int:
    d = raw.replace(",", "").replace(".", "").lstrip("0")
    return len(d.rstrip("0")) if "." not in raw else len(d)


def check_numbers(res, analysis, struct, per_url, pool, trace):
    probs, warns, checked = [], [], 0
    for path, text in ai_text_fields(analysis):
        m = re.match(r"(key_events|industry_items)\[(\d+)\]", path)
        is_event = bool(m)
        if is_event:
            item = analysis[m.group(1)][int(m.group(2))]
            cited = per_url.get(norm_url(item.get("source_url", "")), [])
            truth = cited + struct  # 事件的數字優先對回「它引用的那則新聞」＋結構化資料
        else:
            truth = pool + struct   # 摘要／今日判讀／貿易含義：結構化資料＋AI 看到的新聞
        for tok in ai_tokens(text):
            checked += 1
            hits = match_token(tok, truth)
            note = ""
            if not hits and is_event and sig_digits(tok["raw"]) >= 3:
                # 同一件事常有多家媒體報導，模型會把兩則合併成一個事件、只附一個連結。
                # 有效位數 ≥3 的數字（352、40.32）碰巧撞到別則新聞的機率極低，放行但提醒。
                hits = match_token(tok, pool + struct)
                if hits:
                    note = "來自另一則新聞（非該事件引用的連結）"
                    warns.append(f"`{path}` 的數字 **{tok['raw']}** 不在引用的那則新聞裡，但在今天另一則新聞找到："
                                 f"{hits[0]}；建議把事件連結補成兩則或改寫")
            trace.append({"field": path, "token": tok["raw"], "unit_mult": tok["mult"],
                          "matched": hits, "note": note})
            if not hits:
                probs.append(f"`{path}` 的數字 **{tok['raw']}** 在原始資料找不到來源（上下文：「…{tok['ctx']}…」）")
    res.add("2. AI 寫的數字都能對回原始 JSON", probs, f"共檢查 {checked} 個數字，全部找得到來源",
            warnings=warns)


def check_links(res, analysis, payload):
    news = [n for n in payload.get("news", []) if n.get("url")]
    by_url = {norm_url(n["url"]): n for n in news}
    probs, n_items = [], 0
    for group, name_key in (("key_events", "source_name"), ("industry_items", "source")):
        for i, e in enumerate(analysis.get(group, [])):
            n_items += 1
            url = (e.get("source_url") or "").strip()
            tag = f"{group}[{i}]「{(e.get('headline') or '')[:24]}」"
            if not url:
                probs.append(f"{tag} 沒有來源連結")
                continue
            if not re.match(r"^https?://[^/\s]+\.[^/\s]+", url):
                probs.append(f"{tag} 連結格式不對：{url}")
                continue
            n = by_url.get(norm_url(url))
            if n is None:
                probs.append(f"{tag} 的連結不在今天的原始新聞裡（可能是模型編的）：{url}")
                continue
            if not (e.get(name_key) or "").strip():
                probs.append(f"{tag} 沒有來源名稱")
            if group == "industry_items" and n.get("published") and e.get("date"):
                if str(e["date"])[:10] != str(n["published"])[:10]:
                    probs.append(f"{tag} 日期 {e['date']} 與新聞發布日 {str(n['published'])[:10]} 不一致")
    res.add("3. 每個事件都有來源連結（且連結、日期對得上原始新聞）", probs,
            f"共 {n_items} 則事件，連結皆可對回原始新聞")


def load_opencc():
    try:
        from opencc import OpenCC
        return OpenCC("s2t")
    except ImportError:
        return None


def check_simplified(res, visible_text, whitelist):
    cc = load_opencc()
    if cc is None:
        res.add("4. 簡體字掃描（OpenCC）", ["缺少 opencc 套件：pip install opencc-python-reimplemented"])
        return
    bad = {}
    for i, ch in enumerate(visible_text):
        if "一" <= ch <= "鿿" and ch not in whitelist:
            t = cc.convert(ch)
            if t != ch:
                bad.setdefault(ch, []).append(i)
    probs = []
    for ch, idxs in bad.items():
        i = idxs[0]
        probs.append(f"簡體字「{ch}」（應為「{cc.convert(ch)}」）出現 {len(idxs)} 次，例如：…{visible_text[max(0, i-12): i+12]}…")
    for term in MAINLAND_TERMS:
        if term in visible_text:
            i = visible_text.index(term)
            probs.append(f"大陸用語「{term}」，例如：…{visible_text[max(0, i-12): i+12]}…")
    res.add("4. 沒有簡體字或大陸用語（OpenCC）", probs, "掃描整頁可見文字，沒有簡體字")


def check_integrity(res, html, payload):
    probs = []
    if re.search(r"\{\{[A-Z_0-9]+\}\}", html):
        probs.append("頁面仍有未替換的 {{TOKEN}}：" + ", ".join(sorted(set(re.findall(r"\{\{[A-Z_0-9]+\}\}", html)))))
    n_missing = html.count("資料缺漏")
    if n_missing:
        probs.append(f"頁面有 {n_missing} 處「資料缺漏」（某個資料來源今天沒抓到）")
    rates = (payload.get("fx") or {}).get("rates", {})
    for code in ("USD", "EUR"):
        r = (rates.get(code) or {}).get("per_unit_selling")
        if r is None:
            probs.append(f"原始資料沒有 {code}/TRY 匯率")
        elif f"{r:.4f}" not in html:
            probs.append(f"頁面找不到 {code}/TRY 匯率 {r:.4f}")
    fc = (payload.get("macro") or {}).get("funding_cost") or []
    if fc and f"{fc[-1]['value']:.1f}" not in html:
        probs.append(f"頁面找不到最新融資成本 {fc[-1]['value']:.1f}")
    for marker in ("TODAY_TAKE", "SUMMARY", "KEY_EVENTS", "INDUSTRY", "TRADE_IMPLICATIONS"):
        if f"AI:{marker}:START" not in html:
            probs.append(f"頁面缺少 AI:{marker} 區塊標記")
    res.add("5. 頁面完整（無殘留 token、匯率與融資成本已寫入）", probs, "token 皆已替換，數字已寫入")


# ───────────────────────── 輸出 ─────────────────────────
def render_report(res, date):
    lines = [f"# 發布檢查 {'通過 ✅' if res.ok else '未通過 ❌ —— 今天的網頁沒有更新'}（{date}）", ""]
    for name, passed, details in res.checks:
        lines.append(f"## {'✅' if passed else '❌'} {name}")
        for d in details:
            lines.append(f"- {d}")
        for w in res.warnings.get(name, []):
            lines.append(f"- ⚠️ 提醒（不擋發布）：{w}")
        lines.append("")
    if not res.ok:
        lines += ["---", "網頁維持昨天的版本。處理方式：修正後到 Actions 手動重跑「Daily Turkiye Econ Data Fetch」；",
                  "若確認某個數字是正確的，可登記到 `verify_allowlist.json` 放行。"]
    return "\n".join(lines)


def send_telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (tok and chat):
        return
    try:
        import requests
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": chat, "text": text[:3800]}, timeout=20)
    except Exception as e:  # 警示失敗不能讓檢查本身出錯
        print(f"(Telegram 警示送出失敗：{e})", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", required=True, help="render 到暫存區的 HTML（尚未發布）")
    ap.add_argument("--date")
    ap.add_argument("--max-age-days", type=int, default=1)
    ap.add_argument("--report", default=str(ROOT / "build" / "verify-report.md"))
    ap.add_argument("--alert", action="store_true", help="未通過時送 Telegram（需 TELEGRAM_BOT_TOKEN/CHAT_ID）")
    args = ap.parse_args()

    date = resolve_date(args.date)
    payload_path = DATA_DIR / f"{date}.json"
    analysis_path = DATA_DIR / f"{date}-analysis.json"
    html_path = Path(args.html)
    res = Result()

    payload = load_json(payload_path) if payload_path.exists() else {}
    analysis = load_json(analysis_path) if analysis_path.exists() else None
    html = html_path.read_text(encoding="utf-8") if html_path.exists() else ""
    if not html:
        res.add("0. 待發布的 HTML 存在", [f"找不到或是空的：{html_path}"])

    check_freshness(res, date, payload_path, analysis_path, analysis, args.max_age_days)

    trace = []
    if analysis is not None and payload:
        allow_path = ROOT / "verify_allowlist.json"
        allow = load_json(allow_path).get("numbers", []) if allow_path.exists() else []
        wl = DEFAULT_WHITELIST | (set(load_json(allow_path).get("simplified_whitelist", "")) if allow_path.exists() else set())
        struct, per_url, pool = build_truth(payload, allow)
        check_numbers(res, analysis, struct, per_url, pool, trace)
        check_links(res, analysis, payload)
    else:
        wl = set(DEFAULT_WHITELIST)
        res.add("2. AI 寫的數字都能對回原始 JSON", ["缺少原始資料或 AI 分析，無法比對"])
        res.add("3. 每個事件都有來源連結", ["缺少原始資料或 AI 分析，無法比對"])

    check_simplified(res, html_to_text(html), wl)
    check_integrity(res, html, payload)

    report = render_report(res, date)
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"{date}-verify.json").write_text(json.dumps(
        {"date": date, "passed": res.ok,
         "checks": [{"name": n, "passed": p, "details": d} for n, p, d in res.checks],
         "number_trace": trace}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(report)
    if not res.ok and args.alert:
        send_telegram(f"⚠️ 土耳其日報 {date} 發布檢查未通過，網頁沒有更新。\n" +
                      "\n".join(f"❌ {n}" for n, p, _ in res.checks if not p))
    sys.exit(0 if res.ok else 1)


if __name__ == "__main__":
    main()
