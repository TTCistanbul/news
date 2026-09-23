#!/usr/bin/env python3
"""
Türkiye 經濟簡報 — 每日資料抓取

輸出 data/YYYY-MM-DD.json，供模板套用。
排程：每天 07:30 UTC = 10:30 TRT（Türkiye 全年 UTC+3，不換日光節約時間）
　　　此檔案本身不含排程邏輯，實際觸發時間由外部排程器（如 Windows工作排程器）
　　　設定，這裡的時間只是文件記錄，改排程請直接去排程器改觸發器。

用法:
    python3 fetch_daily.py                # 抓今天
    python3 fetch_daily.py --discover     # 只探測各媒體的 RSS 位址
    python3 fetch_daily.py --no-evds      # 跳過需要金鑰的部分

相依:
    pip install requests feedparser borsapy

2026-08-29 實測：TCMB 在 2025 年底把 EVDS 服務換到新網域 evds3.tcmb.gov.tr，
舊的 evds2 REST 端點（/service/evds/?series=...）直接關站，全部 302 轉址
到一個 SPA 前端，PyPI 上所有舊版 evds/evdsAPI/evdspy 套件因此全部失效。
新的 v3 後端不只是換了路徑，閘道本身要求瀏覽器等級的 headers 加 cookie
sticky session 才會放行，純用 requests.get() 手刻基本刻不出來。改用
borsapy 這個持續在維護、已經處理好這些細節的套件，見 fetch_evds()。
"""

import argparse
import datetime as dt
import html as html_lib
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import requests

try:
    import feedparser
except ImportError:
    feedparser = None

try:
    import borsapy as bp
except ImportError:
    bp = None

UA = {"User-Agent": "TAITRA-Istanbul-EconBrief/1.0 (+internal use)"}
TIMEOUT = 20
OUT_DIR = Path(__file__).parent / "data"

# TRT 固定 UTC+3，全年不變
TRT = dt.timezone(dt.timedelta(hours=3))


# ─────────────────────────────────────────────
# 1. TCMB 每日匯率 — 免金鑰，最穩定的一支
# ─────────────────────────────────────────────
def fetch_tcmb_fx(day: dt.date | None = None) -> dict:
    """
    TCMB 每個營業日約 15:30 TRT 公布當日匯率。
    today.xml 永遠是最新一個營業日；歷史用 YYYYMM/DDMMYYYY.xml。
    週末與國定假日不發布，所以 08:30 的簡報實際拿到的是前一營業日。
    """
    if day is None:
        url = "https://www.tcmb.gov.tr/kurlar/today.xml"
    else:
        url = f"https://www.tcmb.gov.tr/kurlar/{day:%Y%m}/{day:%d%m%Y}.xml"

    r = requests.get(url, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    # 根元素屬性 Tarih 是 dd.mm.yyyy，Date 是美式 mm/dd/yyyy。
    # 一律轉成 ISO，避免下游把 08/09 讀成 8月9日。
    quote_date = None
    raw = root.attrib.get("Tarih")
    if raw:
        try:
            quote_date = dt.datetime.strptime(raw.strip(), "%d.%m.%Y").date().isoformat()
        except ValueError:
            pass

    want = {"USD", "EUR", "GBP", "JPY", "CNY"}
    out = {
        "date": quote_date,
        "date_raw": raw,
        "bulletin": root.attrib.get("Bulten_No"),
        "rates": {},
    }
    for c in root.findall("Currency"):
        code = c.attrib.get("CurrencyCode")
        if code not in want:
            continue

        def num(tag):
            el = c.find(tag)
            if el is None or not (el.text or "").strip():
                return None
            return float(el.text.strip())

        # JPY 等幣別以「每 100 單位」報價。原始值保留，另給每 1 單位的換算值，
        # 下游一律用 per_unit_*，避免差 100 倍。
        unit = float(c.findtext("Unit") or 1)
        fb, fs, bs = num("ForexBuying"), num("ForexSelling"), num("BanknoteSelling")
        out["rates"][code] = {
            "unit": unit,
            "forex_buying": fb,
            "forex_selling": fs,
            "banknote_selling": bs,
            "per_unit_buying": (fb / unit) if fb is not None else None,
            "per_unit_selling": (fs / unit) if fs is not None else None,
        }

    # 資料新鮮度：TCMB 只在營業日約 15:30 TRT 發布，週末與國定假日不更新。
    # 08:30 的簡報拿到的必然是前一營業日，週一則是上週五。
    if quote_date:
        age = (dt.date.today() - dt.date.fromisoformat(quote_date)).days
        out["age_days"] = age
        out["stale"] = age > 3          # 超過三天代表可能抓到快取或連假
    else:
        out["age_days"] = None
        out["stale"] = True

    # TWD 不在 TCMB 清單裡，需另一個 USD/TWD 來源做交叉計算（見
    # fetch_twd_try_cross()）
    return out


# ─────────────────────────────────────────────
# 1b. TWD/TRY 交叉匯率 — TCMB 沒有直接報價，台灣央行也不報 TRY，
#     兩邊都缺對方，只能用第三方免費匯率 API 抓「1 美元換多少台幣」，
#     再拿 TCMB 剛抓到的官方 USD/TRY 相除算出交叉匯率。
#     TRY 那一段仍然用 TCMB 官方數字（跟報告其他匯率同源，一致性較好），
#     只有 USD/TWD 這一段來自第三方，不是任何央行的正式報價。
# ─────────────────────────────────────────────
TWD_CROSS_URL = "https://open.er-api.com/v6/latest/USD"


# ─────────────────────────────────────────────
# 1c. 布蘭特原油現貨價 — 主要來源是 EIA（美國能源資訊署）自己的 Open Data
#     API v2，序列 RBRTE（Europe Brent Spot Price FOB，每桶美元，日頻）。
#     免費金鑰在 https://www.eia.gov/opendata/register.php 申請，設成環境
#     變數 EIA_API_KEY。
#
#     2026-09-09 改用官方 API，原因：原本走 datahub.io 的 CSV 鏡像，那份
#     鏡像自己就落後好幾天（實測 09-03～09-09 六天，最新一筆都停在 09-01，
#     age_days 一路長到 7 天），等於在 EIA 本來就有的發布時差上又疊一層。
#     直接打 EIA 至少把鏡像那一層拿掉。
#
#     ⚠ 就算接了官方 API，這條仍然是「現貨」而且天生落後——EIA 不是即時
#     報價來源。頁面上要跟新聞區的期貨行情（例如「Brent 逼近 100 美元」）
#     對得起來，要的是期貨報價，不是這條。這裡只解決鏡像多出來的落後，
#     沒有解決現貨與期貨口徑不同的問題。
#
#     沒有金鑰或 API 抓失敗時，退回原本的 datahub CSV（下面 BRENT_CSV_URL），
#     資料屬公開領域授權，不用金鑰。兩條路徑產出的欄位完全一樣，只有
#     source / source_kind 不同。
# ─────────────────────────────────────────────
BRENT_EIA_URL = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
BRENT_EIA_SERIES = "RBRTE"
BRENT_CSV_URL = "https://datahub.io/core/oil-prices/_r/-/data/brent-daily.csv"

# 2026-09-23 第一順位從 stooq 換成 Yahoo Finance 的 BZ=F（ICE Brent 前月期貨
#     連續合約）。stooq 從 09-15 接上後在 GitHub Actions 上一次都沒成功過：
#     它對機房 IP 回的是「需要 JavaScript 驗證瀏覽器」的反機器人頁面，
#     requests 過不了，換 User-Agent 也沒用。
#
#     Yahoo 這條免金鑰、口徑同樣是期貨收盤。它也可能擋機房 IP，所以失敗時
#     照樣往下退：Yahoo 期貨 → EIA 現貨 → datahub 現貨鏡像。
#     每一條失敗的原因都會寫進 brent_oil.fallback_reasons，不用翻 Actions log。
#
#     range=5y 夠算年初至今，資料量也很小（一年約 250 筆）。
BRENT_YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/BZ=F"
BRENT_YAHOO_RANGE = "5y"
# Yahoo 對預設的 python-requests UA 常直接回 429，這裡用一般瀏覽器 UA。
BRENT_YAHOO_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"}


# ─────────────────────────────────────────────
# 1d. 貿易俱樂部（中國輸出入銀行）土耳其專頁 —— 半年左右才發一篇新報告，
#     跟其他每天都有新內容的來源性質不同。每天只檢查「綜合評論」列表
#     最上面那篇的 ID，跟上次記錄的 ID 比對；只有真的換了新報告才回傳
#     完整內容，其餘時候回傳 None（代表「沒有新報告」，不是抓取失敗，
#     呼叫端不要把 None 當成錯誤處理）。
# 2026-08-31：沒辦法連網實機驗證這段正規表達式在真實 HTML 上抓不抓得到，
#     如果之後發現一直抓不到任何項目，把印出來的「解析不到任何項目」
#     警告訊息回報，附上當時網站原始碼，才能照實際標籤結構調整，不要用猜的。
# ─────────────────────────────────────────────
EXIMCLUB_LIST_URL = (
    "https://www.eximclub.com.tw/innerListA.aspx?Continen=3&Country=%E5%9C%9F%E8%80%B3%E5%85%B6"
)
EXIMCLUB_STATE_PATH = OUT_DIR / "_eximclub_seen.json"


def fetch_eximclub_turkey_report() -> dict | None:
    try:
        r = requests.get(EXIMCLUB_LIST_URL, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        html_text = r.text
    except Exception as e:
        print(f"✗ 貿易俱樂部列表: {e}", file=sys.stderr)
        return None

    # 只抓「綜合評論」文章連結（Type=Publish），排除頁面上方「基本國情」
    # 那種 Type=Condition 連結，避免抓錯 ID 序列。日期跟 ID 分開抓，
    # 假設兩份清單順序一致、成對出現（列表本身依日期新到舊排序）。
    ids = re.findall(r"Type=Publish[^\"'>]*?ID=(\d+)", html_text)
    dates = re.findall(r"\b(\d{4}/\d{2}/\d{2})\b", html_text)
    if not ids or not dates:
        print(
            "✗ 貿易俱樂部列表: 解析不到任何項目，網站可能改版了，"
            f"HTML 長度={len(html_text)}",
            file=sys.stderr,
        )
        return None

    latest_id, latest_date = ids[0], dates[0]

    try:
        last_seen = json.loads(EXIMCLUB_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        last_seen = {}

    if last_seen.get("id") == latest_id:
        print(f"- 貿易俱樂部：沒有新報告（最新仍是 {latest_id}，{latest_date}）", file=sys.stderr)
        return None

    content_url = (
        f"https://www.eximclub.com.tw/innerContent.aspx?"
        f"Type=Publish&ID={latest_id}&Continen=3&Country=%E5%9C%9F%E8%80%B3%E5%85%B6"
    )
    title = ""
    excerpt = ""
    try:
        r2 = requests.get(content_url, headers=UA, timeout=TIMEOUT)
        r2.raise_for_status()
        m_title = re.search(r"<title>([^<]+)</title>", r2.text)
        if m_title:
            title = m_title.group(1).strip()
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r2.text,
                       flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        excerpt = text[:3000]  # 全文可能很長，先截一段，避免 payload 過大
    except Exception as e:
        print(f"✗ 貿易俱樂部內文: {e}", file=sys.stderr)

    EXIMCLUB_STATE_PATH.parent.mkdir(exist_ok=True)
    EXIMCLUB_STATE_PATH.write_text(
        json.dumps({"id": latest_id, "date": latest_date, "title": title},
                   ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "id": latest_id,
        "date": latest_date,
        "title": title,
        "url": content_url,
        "content_excerpt": excerpt,
    }


def _obs_on_or_before(observations, target: dt.date, tolerance_days: int):
    """
    在（依日期遞增排序的）觀測值裡，找「日期 <= target」之中最接近的一筆。
    離 target 超過 tolerance_days 就回 None——寧可讓那一格顯示「—」，也不要
    拿一個月前的價格冒充「一週前」。假日與資料空窗是常態，所以要容忍幾天，
    但容忍過頭就失去意義。
    """
    best = None
    for d, price in observations:
        if d <= target and (target - d).days <= tolerance_days:
            if best is None or d > best[0]:
                best = (d, price)
    return best


def _brent_change(observations, latest_date: dt.date, latest_price: float,
                   days_back: int, tolerance_days: int) -> dict | None:
    """回傳 {pct, base_date, base_price}，找不到合適基準就回 None。"""
    base = _obs_on_or_before(observations,
                             latest_date - dt.timedelta(days=days_back),
                             tolerance_days)
    if base is None or not base[1]:
        return None
    return {
        "pct": round((latest_price - base[1]) / abs(base[1]) * 100, 2),
        "base_date": base[0].isoformat(),
        "base_price": base[1],
    }


def _fetch_brent_yahoo() -> list[tuple[dt.date, float]]:
    """Yahoo Finance chart API 的 BZ=F 日線，回傳 [(日期, 每桶美元), ...]。
    抓不到就丟例外讓呼叫端往下退到 EIA。

    時間戳是 UTC 秒數；Brent 在倫敦收盤，換成 UTC 日期就是交易日，不會跨日。
    close 裡偶爾會有 None（停牌或當天還沒收盤），直接略過。"""
    params = {"range": BRENT_YAHOO_RANGE, "interval": "1d"}
    r = requests.get(BRENT_YAHOO_URL, params=params,
                     headers=BRENT_YAHOO_HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"Yahoo 回的不是 JSON：{r.text[:200]}")
    chart = body.get("chart") or {}
    if chart.get("error"):
        raise RuntimeError(f"Yahoo 回報錯誤：{str(chart['error'])[:200]}")
    results = chart.get("result") or []
    if not results:
        raise RuntimeError(f"Yahoo 回應沒有 result：{str(body)[:200]}")
    res = results[0]
    stamps = res.get("timestamp") or []
    quotes = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = quotes.get("close") or []

    out = []
    for ts, close in zip(stamps, closes):
        if ts is None or close is None:
            continue
        d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).date()
        out.append((d, round(float(close), 2)))
    if not out:
        raise RuntimeError("Yahoo 回應解析不出任何一筆收盤價")
    # 同一天偶爾會有兩筆（當天盤中＋前一筆），同日期只留最後一筆。
    dedup = {}
    for d, p in out:
        dedup[d] = p
    return sorted(dedup.items())


def _redact(msg: str) -> str:
    """錯誤訊息裡的 URL 會帶 api_key，寫進公開 repo 的 JSON 前先遮掉。"""
    return re.sub(r"(api_key=)[^&\s]*", r"\1***", str(msg))[:300]


def _fetch_brent_eia(api_key: str) -> list[tuple[dt.date, float]]:
    """EIA Open Data API v2，回傳 [(日期, 每桶美元), ...]，抓不到就丟例外
    讓呼叫端退回 CSV。sort 用 period desc + length 取最近 800 筆（約三年
    的交易日），足夠算年初至今，又不用把 1987 年至今整份拉下來。"""
    params = {
        "api_key": api_key,
        "frequency": "daily",
        "data[0]": "value",
        "facets[series][]": BRENT_EIA_SERIES,
        "sort[0][column]": "period",
        "sort[0][direction]": "desc",
        "offset": 0,
        "length": 800,
    }
    r = requests.get(BRENT_EIA_URL, params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    rows = ((body.get("response") or {}).get("data")) or []
    if not rows:
        # API 對錯誤的金鑰／參數會回 200 加一個 error 欄位，不是 HTTP 4xx，
        # 所以這裡要把原始回應印出來，不然只會看到「0 筆」不知道為什麼。
        raise RuntimeError(f"EIA 回應沒有資料：{str(body)[:300]}")
    out = []
    for row in rows:
        period, value = row.get("period"), row.get("value")
        if not period or value in (None, ""):
            continue
        try:
            out.append((dt.date.fromisoformat(str(period)[:10]), float(value)))
        except (ValueError, TypeError):
            continue
    if not out:
        raise RuntimeError(f"EIA 回應解析不出任何一筆：{str(rows[:2])[:300]}")
    return out


def _fetch_brent_datahub() -> list[tuple[dt.date, float]]:
    """備援：datahub.io 的 CSV 鏡像（1987 年至今全部歷史）。"""
    r = requests.get(BRENT_CSV_URL, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    lines = r.text.strip().splitlines()
    if len(lines) < 2:
        raise RuntimeError(f"CSV 內容異常（只有 {len(lines)} 行）")
    header = [h.strip().lower() for h in lines[0].split(",")]
    if header[:2] != ["date", "price"]:
        raise RuntimeError(f"CSV 欄位跟預期不同，實際標頭：{lines[0]}")

    out = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 2:
            continue
        date_str, price_str = parts[0].strip(), parts[1].strip()
        if not date_str or not price_str:
            continue
        try:
            out.append((dt.date.fromisoformat(date_str), float(price_str)))
        except ValueError:
            continue
    if not out:
        raise RuntimeError("CSV 裡解析不出任何一筆有效資料")
    return out


def _obs_on_or_before(observations, target: dt.date, tolerance_days: int):
    """
    在（依日期遞增排序的）觀測值裡，找「日期 <= target」之中最接近的一筆。
    離 target 超過 tolerance_days 就回 None——寧可讓那一格顯示「—」，也不要
    拿一個月前的價格冒充「一週前」。假日與資料空窗是常態，所以要容忍幾天，
    但容忍過頭就失去意義。
    """
    best = None
    for d, price in observations:
        if d <= target and (target - d).days <= tolerance_days:
            if best is None or d > best[0]:
                best = (d, price)
    return best


def _brent_change(observations, latest_date: dt.date, latest_price: float,
                   days_back: int, tolerance_days: int) -> dict | None:
    """回傳 {pct, base_date, base_price}，找不到合適基準就回 None。"""
    base = _obs_on_or_before(observations,
                             latest_date - dt.timedelta(days=days_back),
                             tolerance_days)
    if base is None or not base[1]:
        return None
    return {
        "pct": round((latest_price - base[1]) / abs(base[1]) * 100, 2),
        "base_date": base[0].isoformat(),
        "base_price": base[1],
    }


def _build_brent_payload(observations, source: str, source_kind: str,
                          basis: str = "spot") -> dict:
    """三條取得路徑（Yahoo 期貨／EIA API／datahub CSV）共用的計算，產出的欄位
    一模一樣，render_report 不需要知道資料是從哪條路來的。basis 是唯一
    需要往下傳的差別：futures = 期貨收盤，spot = 現貨，頁面備註會照著寫。"""
    observations = sorted(observations, key=lambda x: x[0])
    best_date, best_price = observations[-1]
    previous_date, previous_price = (observations[-2] if len(observations) >= 2
                                      else (None, None))

    # 日變動：對「前一個真的有報價的交易日」，不是對「昨天」，
    # 所以週末與假日不會被誤算成持平。
    change_pct = None
    if previous_price:
        change_pct = round((best_price - previous_price) / previous_price * 100, 2)

    # 週／月／年初至今。三個區間的「終點」一律是 best_date（最後一筆
    # 報價日），不是今天——資料卡住時，區間會誠實地停在報價日，而不是
    # 拿一筆舊報價去跟今天湊出一段名不副實的期間。
    #
    # 年初至今的基準取「去年最後一個交易日」的收盤價（YTD 的標準定義）；
    # 萬一序列裡沒有去年的資料，退而取今年第一筆。
    prev_year = [o for o in observations if o[0].year == best_date.year - 1]
    this_year = [o for o in observations if o[0].year == best_date.year]
    ytd_base = prev_year[-1] if prev_year else (this_year[0] if this_year else None)
    ytd = None
    if ytd_base and ytd_base[1] and ytd_base[0] != best_date:
        ytd = {
            "pct": round((best_price - ytd_base[1]) / abs(ytd_base[1]) * 100, 2),
            "base_date": ytd_base[0].isoformat(),
            "base_price": ytd_base[1],
        }

    age_days = (dt.date.today() - best_date).days
    return {
        "date": best_date.isoformat(),
        "usd_per_barrel": best_price,
        "previous_date": previous_date.isoformat() if previous_date else None,
        "previous_usd_per_barrel": previous_price,
        "change_pct": change_pct,
        "changes": {
            # 容忍天數：一週抓 5 天（吃得下連假），一個月抓 10 天。
            "week": _brent_change(observations, best_date, best_price, 7, 5),
            "month": _brent_change(observations, best_date, best_price, 30, 10),
            "ytd": ytd,
        },
        "changes_anchor_date": best_date.isoformat(),
        "age_days": age_days,
        # 2026-09-08 從 5 天收緊到 3 天。原本 5 天太寬鬆，資料卡住
        # 三四天都還不會被標記，容易沒注意到。收到 2 天又太緊——EIA
        # 只在工作日更新，週五收盤後到週一之間本來就有 2-3 天的正常
        # 空窗（六、日不更新，週一的資料通常也要等到當天收盤後才有），
        # 訂在 2 天會導致每週一早上都被誤判成「過期」，反而製造假警報。
        # 3 天可以吃下這個正常的週末空窗，只有真的卡超過一個週末才會
        # 觸發。
        "stale": age_days > 3,
        "source": source,
        "source_kind": source_kind,
        "basis": basis,
        "observations_used": len(observations),
    }


def fetch_brent_oil() -> dict | None:
    """依序試 Yahoo 期貨 → EIA 現貨 → datahub 現貨鏡像，先成功的就用。
    三條路徑的輸出格式相同，差別只在 source_kind 與 basis。
    前面失敗的原因記在 fallback_reasons，直接看 data/*.json 就知道為什麼退。"""
    reasons: list[str] = []

    def _done(payload: dict) -> dict:
        payload["fallback_reasons"] = reasons
        return payload

    # 1. Yahoo：ICE Brent 前月期貨連續合約，免金鑰，當天收盤後就有。
    try:
        obs = _fetch_brent_yahoo()
        return _done(_build_brent_payload(
            obs, "Yahoo Finance — ICE Brent front-month futures (BZ=F)",
            "yahoo_futures", basis="futures"))
    except Exception as e:
        reasons.append(f"yahoo: {_redact(e)}")
        print(f"! 布蘭特原油: Yahoo 期貨抓取失敗，往下退到 EIA 現貨：{_redact(e)}",
              file=sys.stderr)

    # 2. EIA 官方現貨（RBRTE）。口徑是現貨，本來就會落後幾天。
    #    strip()：2026-09-23 實際踩到 secret 尾巴多兩個換行，EIA 回 403。
    api_key = (os.environ.get("EIA_API_KEY") or "").strip()
    if api_key:
        try:
            obs = _fetch_brent_eia(api_key)
            return _done(_build_brent_payload(
                obs, "EIA Open Data API v2 (series RBRTE)", "eia_api",
                basis="spot"))
        except Exception as e:
            reasons.append(f"eia: {_redact(e)}")
            print(f"! 布蘭特原油: EIA API 失敗，改用 datahub CSV 備援：{_redact(e)}",
                  file=sys.stderr)
    else:
        reasons.append("eia: 未設定 EIA_API_KEY")
        print("! 未設定 EIA_API_KEY，布蘭特原油改用 datahub CSV 備援"
              "（那份鏡像通常比 EIA 官方慢幾天）", file=sys.stderr)

    # 3. datahub 鏡像，最後一道。實測會比 EIA 再慢好幾天，只是不讓欄位開天窗。
    try:
        obs = _fetch_brent_datahub()
        return _done(_build_brent_payload(
            obs, "EIA (via datahub.io, public domain)", "datahub_csv",
            basis="spot"))
    except Exception as e:
        print(f"✗ 布蘭特原油: {_redact(e)}", file=sys.stderr)
        return None


def fetch_twd_try_cross(usd_try_selling: float | None) -> dict | None:
    """
    open.er-api.com 是免金鑰的開放端點，官方建議一天呼叫一次，剛好符合
    這支腳本的執行頻率，不會被限流。第一次上線後，如果 log 裡看到欄位
    對不上（例如 rates 底下沒有 TWD），把印出來的完整回應內容貼出來，
    照實際欄位調整即可，不用整段重猜——跟 fetch_evds() 處理 borsapy
    欄位不確定性的方式是一樣的邏輯。
    """
    if usd_try_selling is None:
        return None
    try:
        r = requests.get(TWD_CROSS_URL, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        rates = data.get("rates") or {}
        usd_twd = rates.get("TWD")
        if usd_twd is None:
            print(
                f"✗ TWD/TRY 交叉匯率: 回應裡沒有 TWD 欄位，"
                f"實際 rates keys（前 10 個）: {list(rates.keys())[:10]}",
                file=sys.stderr,
            )
            return None
        return {
            "try_per_twd": usd_try_selling / usd_twd,
            "usd_twd": usd_twd,
            "usd_try_selling_used": usd_try_selling,
            "source": "open.er-api.com（交叉匯率，非央行官方報價）",
            "fetched_at_source": data.get("time_last_update_utc"),
        }
    except Exception as e:
        print(f"✗ TWD/TRY 交叉匯率: {e}", file=sys.stderr)
        return None


# ─────────────────────────────────────────────
# 2. TCMB EVDS — 總經時間序列，需免費 API 金鑰
#    註冊登入後開 https://evds3.tcmb.gov.tr → 右上「BENİM SAYFAM」→
#    登入後點使用者名稱 → 「Profilim」→ 頁面下方「API Key Kopyala」
# ─────────────────────────────────────────────
# 序號請到 EVDS「ALL SERIES」分頁展開分類確認（MY SELECTIONS 會顯示代號，
# 「SELECT DATAGROUP」右上的 🔗 圖示可產生含序號的連結，比手抄可靠）。
# frequency 留 None 代表拿序列原始頻率，設定要跟資料組實際頻率一致，
# 設錯會拿到空值（funding_cost 是 Business 頻率，不是月頻）。
EVDS_SERIES = {
    # 加權平均融資成本 —— 不是政策利率。一週附賣回自 2026/03 暫停後，
    # 銀行實際走隔夜管道，這支才反映土耳其廠商真正付的資金成本。
    # 序列本身是 Business 頻率，不要轉月頻。
    "funding_cost": ("TP.APIFON4", None),
    # CPI：舊代碼 TP.FE.OKTG01 是舊基期版本（可能是 2003=100），TCMB 換成
    # 2025=100 新基期後這支被凍結在舊資料停更，2026-08-29 從 EVDS 網頁的
    # 「CONSUMER PRICE INDEX (TURKSTAT) → Indicators For The CPIs Having
    # Specified Coverage (2025=100)」查到新代碼，換成這支。CPI 本身就是
    # 月頻資料，frequency=5 沒問題，跟 funding_cost 不同，不要對調。
    "cpi": ("TP.FE25.OKTG01", 5),
    # 商品貿易差額（淨額，International BPM6 統計口徑）——2026-08-31 從 EVDS
    # 網頁 All Series → BALANCE OF PAYMENTS STATISTICS (CBRT) → Balance of
    # Payments Developments-Detailed Presentation → 1.1.Goods 底下查到這支，
    # 使用者手動在網頁上確認取得，非由本程式自動探索。單位是「百萬美元」
    # （月頻），不是台幣簡報常見的「億」，render_report.py 那邊會換算。
    # 如果之後發現數字對不起來（例如變成只有出口或只有進口，不是淨額），
    # 回頭到同一個路徑重新確認選的是「Net」那個子項目，換掉這裡的代碼。
    # 商品出口／進口（BPM6，International 統計口徑）——2026-08-31 使用者
    # 用 EVDS 網頁自己匯出 Excel 核對過：Q5 是出口、Q6 是進口，兩者相減
    # 算出來的差額量級（每月約 -44 億～-95 億美元）跟 TÜİK 官方數字對得上。
    # 原本試過 Q16「Net Exports of Goods Under Merchanting」，那個其實是
    # 「轉口貿易淨額」這種很小眾的特殊項目，數字量級差了幾十倍，不要用。
    "trade_exports": ("TP.ODEAYRSUNUM6.Q5", 5),
    "trade_imports": ("TP.ODEAYRSUNUM6.Q6", 5),
    # 季度實質 GDP（TÜİK，支出面法，鏈式不變價格，未經季節調整）——
    # 2026-09-08 使用者第一次用網址列上的 slug「bie_gsyhhrczinc」抓，
    # EVDS API 回 400「Series does not exist」——那個 slug 是給人看的
    # 網址代稱，不是真正的 API 序列代碼。後來使用者在 EVDS 網頁上重新
    # 勾選「Gayrisafi Yurt İçi Hasıla (Harcama Yöntemiyle, Zincirlenmiş
    # Hacim)」、匯出表格，欄位標題顯示的才是真正代碼：
    # TP_GSYIH20_CY_B1GQ（畫面上底線對應真正代碼裡的點，所以是
    # TP.GSYIH20.CY.B1GQ）。frequency=6（quarterly）不變。
    #
    # 這個序列沒有做季節調整，只拿來算年增率（跟去年同一季比，季節性
    # 因素會互相抵銷，未調整也沒關係）。2026-09-08 用 TÜİK 官方新聞稿
    # 核對過：2026 Q2 算出來的年增率 +2.3%，跟官方公布的數字完全一致。
    # 千萬不要拿這條序列算季增率——實測踩過雷：算出 +7.2%，但官方公布
    # 的季增率其實是 +1.1%，差了 6 倍，因為土耳其 GDP 季節性很明顯
    # （Q1 傳統低、Q2/Q3 回溫是每年常態），沒調整的序列直接算季增率會
    # 把季節性誤判成真實成長。季增率要用下面 gdp_growth_sa 那條。
    "gdp_growth": ("TP.GSYIH20.CY.B1GQ", 6),
    # 季度實質 GDP，季節與工作日調整後版本（TÜİK 官方新聞稿裡的
    # 「Mevsim ve takvim etkilerinden arındırılmış GSYH」就是指這個）——
    # 只拿來算季增率，不要拿來算年增率（這條是「指數」形式，不是原始
    # 金額量級，年增率理論上應該也算得出來，但 TÜİK 官方公布年增率時
    # 用的是上面那條原始序列，維持跟官方一致的算法，這裡分工明確：
    # 年增率＝gdp_growth，季增率＝gdp_growth_sa，不要混用）。
    # 序列代碼一樣是使用者從 EVDS 網頁匯出表格核對得到：
    # TP_GSYIH30_HY_B1GQ → TP.GSYIH30.HY.B1GQ。2026-09-08 用 2026 Q2
    # (245.18) vs 2026 Q1 (242.47) 算出季增 +1.12%，跟 TÜİK 官方公布的
    # +1.1% 一致，確認選對序列。
    "gdp_growth_sa": ("TP.GSYIH30.HY.B1GQ", 6),
    # 核心通膨 B 指標（TÜİK 特定涵蓋範圍 CPI／Özel Kapsamlı TÜFE
    # Göstergeleri，2025=100）：不含未加工食品、能源、酒精飲料與菸草、黃金。
    # 2026-09-09 使用者從 EVDS 網頁勾選後匯出表格取得代碼（欄位標題
    # TP_FE25_OKTG03，底線換成點），跟上面 gdp 那兩條同一套做法。
    # 驗證：用這條算出的 2026 年 6/7/8 月年增率為 31.18／30.97／30.68，
    # 跟 TÜİK 公布的核心通膨完全吻合，確認勾對的是 B 不是 A 或 C。
    # 同一組裡 TP.FE25.OKTG04 是 C 指標（再扣掉全部食品與非酒精飲料），
    # 土耳其媒體講「çekirdek enflasyon」有時是指 C，要換就換這支。
    # 跟 cpi 一樣是月頻，frequency=5。
    "core_cpi_b": ("TP.FE25.OKTG03", 5),
}

# EVDS v2 → v3 frequency 對照（v2 是舊 evds/evdspy 系列套件慣用的 1-8 編號，
# borsapy 吃 snake_case 字串）。EVDS_SERIES 裡繼續用舊的數字，這裡轉換。
_FREQUENCY_V2_TO_BORSAPY = {
    1: "daily", 2: "business", 3: "weekly", 4: "twice_monthly",
    5: "monthly", 6: "quarterly", 7: "semiannual", 8: "annual",
}


def fetch_evds(series_code: str, start: dt.date, end: dt.date,
               api_key: str, frequency: int | None = None) -> list:
    """
    2026-08-29 實測發現：TCMB 已在 2025 年底把 EVDS 服務換到新網域
    evds3.tcmb.gov.tr，舊的 evds2 REST 端點整個關站（302 轉址到 SPA
    首頁），連換成新網域＋官方文件的 URL 格式一樣拿不到資料——新版閘道
    要求瀏覽器等級的 headers 加 cookie sticky session 才會放行，不是單純
    URL 或參數問題，自己手刻 requests.get() 基本刻不出來。

    改用 borsapy 這個持續在維護、已經處理好這些細節的套件（見
    https://github.com/saidsurucu/borsapy）。這裡沒辦法在沒有網路的環境
    實機測試過，如果欄位名稱跟預期的不同，這個函式會把 DataFrame 的實際
    欄位印出來，照著調 COLUMN 對應即可，不用整個重猜。
    """
    if bp is None:
        raise RuntimeError("未安裝 borsapy，請先 pip install borsapy")

    bp.set_evds_key(api_key)
    freq_str = _FREQUENCY_V2_TO_BORSAPY.get(frequency) if frequency else None
    kwargs = {"start": start.isoformat(), "end": end.isoformat()}
    if freq_str:
        kwargs["frequency"] = freq_str

    df = bp.evds_series(series_code, **kwargs)
    if df is None or len(df) == 0:
        return []

    # 不確定實機上 DataFrame 的欄位長什麼樣子（日期欄可能叫 Tarih/date/index，
    # 數值欄可能是序列代碼本身），這裡盡量兼容常見命名，抓不到就整個
    # DataFrame 轉出來，至少看得到實際欄位名稱去調整。
    df = df.reset_index()
    date_col = next((c for c in df.columns
                      if c.lower() in ("tarih", "date", "index")), df.columns[0])
    value_col = next((c for c in df.columns if c != date_col), None)
    if value_col is None:
        raise RuntimeError(
            f"borsapy 回傳的 DataFrame 只有日期欄，看不出數值欄："
            f"columns={list(df.columns)}"
        )
    return [
        {"date": str(row[date_col]), "value": row[value_col]}
        for _, row in df.iterrows()
    ]



# ─────────────────────────────────────────────
# 3. 媒體 RSS
#    這些位址會改，第一次跑請用 --discover 確認，別直接信這份清單
# ─────────────────────────────────────────────
# 2026-08-29 實測結果：全部回 200 且有內容（含 Webrazzi 與兩個 rss.app 自訂來源，
# 已於 discover 後確認實際媒體身分，見下方註記）。
FEED_CANDIDATES = {
    "Anadolu Ajansı (EN)":  ["https://www.aa.com.tr/en/rss/default?cat=economy"],       # 30
    "Daily Sabah":          ["https://www.dailysabah.com/rss/economy"],                 # 50
    "Hürriyet Daily News":  ["https://www.hurriyetdailynews.com/rss/economy"],          # 100
    #                        https://www.hurriyetdailynews.com/rss → 200 但 0 筆
    "Dünya":                ["https://www.dunya.com/rss"],                              # 25
    #                        https://www.dunya.com/rss/ekonomi → 404
    "Ekonomim":             ["https://www.ekonomim.com/rss"],                           # 25
    # 2026-09-23 Bloomberg HT 官方 RSS（/rss）的 lastBuildDate 停在 09-14，
    #   之後完全沒更新，但網站本身照常出稿。改抓「Tüm Ekonomi Haberleri」
    #   列表頁（HTML，約 45 則），解析方式見 _parse_bloomberght()。
    "Bloomberg HT":         ["https://www.bloomberght.com/tum-ekonomi-haberleri"],      # ~45，HTML
    "Hürriyet Ekonomi":     ["https://www.hurriyet.com.tr/rss/ekonomi"],                # 100
    "Sözcü Ekonomi":        ["https://www.sozcu.com.tr/feeds-rss-category-ekonomi"],    # 50
    "Webrazzi":             ["https://webrazzi.com/feed/"],                             # 20，科技/新創/ICT
    # 2026-09-23 新增：台土五大重點產業的專業來源。一般財經媒體幾乎不報
    #   工具機、自動化、塑膠機械，09-10～09-23 實測 354 則土耳其相關新聞裡
    #   只有十幾則碰到這五個產業，所以直接補產業媒體與公會。
    #   ✓ = 2026-09-23 實際讀過 feed、確認有當天內容；? = 依網站架構推定的
    #   RSS 位址（WordPress 標準 /feed/），第一次跑請看 news_diagnostics。
    "ST Endüstri":          ["https://www.stendustri.com.tr/rss"],                      # ✓ 自動化/工業4.0/工具機/包裝機械
    "MİB（機械製造商協會）":  ["https://mib.org.tr/feed/"],                                # ? 工具機/金屬加工
    "plastonline":          ["https://plastonline.com/feed/"],                          # ? 塑膠/包裝/塑膠機械
    "Yeşil Haber":          ["https://yesilhaber.net/feed/"],                           # ? 再生能源/儲能/EV/回收
    "Sözcü Otomotiv":       ["https://www.sozcu.com.tr/feeds-rss-category-otomotiv"],   # ? 汽車/EV（與 Sözcü Ekonomi 同一套 RSS）
    # 2026-09-23 rss.app 免費方案到期（09-07 起 402 Payment Required），
    #   CNN Business／OSD／TAYSAD 三條同時失效。CNN Business 幾乎都被
    #   scope=global 濾掉，直接移除；兩個公會改抓官網新聞頁（HTML），
    #   解析方式見 _parse_osd()／_parse_taysad()。
    "OSD（汽車製造商協會）":  ["https://www.osd.org.tr/haberler"],                        # HTML，有日期
    "TAYSAD（汽車零組件供應商協會）": ["https://taysad.org.tr/tr/haberler"],              # HTML，有日期
}

# 沒有日期欄位的來源：靠 fetch_news() 內的 SEEN 跨天去重擋掉重複。
# 2026-09-23 起 OSD／TAYSAD 改抓官網、有日期了；換成 Bloomberg HT 列表頁沒日期。
NO_DATE_SOURCES = {"Bloomberg HT"}

# 這兩家是純土耳其產業公會，逐則內文本身常常不會提到「Türkiye／土耳其」
# 這類 TR_MARKERS 關鍵字（例如 TAYSAD 的課程名稱），會被 scope 判斷誤判成
# global。這兩家的存在本身就代表土耳其相關，不透過 TR_MARKERS 判斷，
# 直接強制視為 domestic。
ALWAYS_DOMESTIC_SOURCES = {"OSD（汽車製造商協會）", "TAYSAD（汽車零組件供應商協會）",
                           # 2026-09-23 新增的土耳其產業媒體／公會：內文常常只寫
                           # 產品或技術，不會提到 Türkiye，但本身就是土耳其市場的
                           # 產業新聞（外商在土耳其推新設備對台商也是競爭情報）。
                           "ST Endüstri", "MİB（機械製造商協會）", "plastonline",
                           "Yeşil Haber"}

# 這兩家的 feed 停在數天前（2026-08-29 實測：最新一筆各為 08-25、08-26），
# 推測是快取的靜態檔而非即時產生。用 24 小時窗口它們永遠是 0，
# 所以個別放寬，抓到的項目會標 late=true，不冒充當日新聞。
FEED_MAX_HOURS = {
    "Hürriyet Ekonomi": 120,
    # 公會一個月才發幾則，頁面日期只到「日」（當成 00:00 UTC），用 24 小時
    # 窗口很容易漏。放寬到 14 天，重複收錄交給 SEEN 去重擋。
    "OSD（汽車製造商協會）": 24 * 14,
    "TAYSAD（汽車零組件供應商協會）": 24 * 14,
    # 2026-09-23 實測：兩家 feed 都正常，但一週才發幾篇（最新一筆分別是
    # 09-15、09-20），24 小時窗口永遠是 0。重複收錄交給 SEEN 去重擋。
    "MİB（機械製造商協會）": 24 * 14,
    "plastonline": 24 * 7,
}


# ─────────────────────────────────────────────
# 3b. 沒有可用 RSS 的來源：直接解析官網列表頁
#     每個 parser 吃整頁 HTML、回傳跟 feedparser entry 長得一樣的物件
#     （title／summary／link／id／published_parsed），fetch_news() 後面的
#     主題分類、去重、scope 判斷完全共用，不用另外維護一套。
#
#     ⚠ 2026-09-23 撰寫時只看得到這些頁面轉成文字後的樣子，看不到原始
#     HTML，所以 regex 刻意寫得寬鬆（只依賴網址樣式與日期文字，不依賴
#     class 名稱）。如果哪天 diag 顯示某來源 total=0 但沒有 error，就是
#     網站改版了，把該頁原始 HTML 存下來對照調整即可。
# ─────────────────────────────────────────────
_TR_MONTHS = {
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6,
    "temmuz": 7, "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}
_TR_FOLD = str.maketrans("şŞğĞüÜıİöÖçÇ", "ssgguuiioocc")
_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(Ocak|Şubat|Subat|Mart|Nisan|Mayıs|Mayis|Haziran|Temmuz|"
    r"Ağustos|Agustos|Eylül|Eylul|Ekim|Kasım|Kasim|Aralık|Aralik)\s+(\d{4})\b",
    re.IGNORECASE)
_READ_MORE = {"daha fazla oku", "devamını oku", "devamini oku", "detay", ""}


def _strip_tags(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", fragment,
                  flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html_lib.unescape(text)).strip()


def _find_tr_date(text: str, last: bool = False):
    """在一段文字裡找「14 Eylül 2026」這種日期，回傳 (y, m, d, 0, 0, 0)。"""
    hits = list(_DATE_RE.finditer(text))
    if not hits:
        return None
    m = hits[-1] if last else hits[0]
    month = _TR_MONTHS.get(m.group(2).lower().translate(_TR_FOLD))
    if not month:
        return None
    try:
        d = dt.date(int(m.group(3)), month, int(m.group(1)))
    except ValueError:
        return None
    return (d.year, d.month, d.day, 0, 0, 0)


def _parse_listing(page: str, href_re: str, base: str,
                   date_position: str | None) -> list:
    """共用骨架：找出所有「有文字的」文章連結當標題（同一網址只取第一個
    有文字的，圖片連結與「Daha Fazla Oku」這種按鈕略過），再用前後兩個
    標題之間的區段找日期與摘要。

    date_position："before" = 日期寫在標題前面（OSD），
                   "after"  = 日期寫在標題後面（TAYSAD），None = 頁面沒日期。
    """
    anchor_re = re.compile(
        r"<a\b[^>]*href=[\"'](?P<href>" + href_re + r")[\"'][^>]*>(?P<inner>.*?)</a>",
        re.S | re.I)
    items, seen = [], set()
    for m in anchor_re.finditer(page):
        href = html_lib.unescape(m.group("href"))
        text = _strip_tags(m.group("inner"))
        # OSD 的連結內文有時是「標題 標題」（標題加上重複的副標），收成一份
        half = len(text) // 2
        if len(text) % 2 == 1 and text[:half] == text[half + 1:]:
            text = text[:half]
        if text.lower() in _READ_MORE or len(text) < 8:
            continue
        link = href if href.startswith("http") else base + href
        if link in seen:
            continue
        seen.add(link)
        items.append({"link": link, "title": text,
                      "start": m.start(), "end": m.end()})

    entries = []
    for i, it in enumerate(items):
        prev_end = items[i - 1]["end"] if i > 0 else 0
        next_start = items[i + 1]["start"] if i + 1 < len(items) else len(page)
        before = _strip_tags(page[prev_end:it["start"]])
        after = _strip_tags(page[it["end"]:next_start])
        pub = None
        if date_position == "before":
            pub = _find_tr_date(before, last=True)
        elif date_position == "after":
            pub = _find_tr_date(after)
        summary = _DATE_RE.sub(" ", after)
        summary = re.sub(r"(Pazartesi|Salı|Çarşamba|Perşembe|Cuma|Cumartesi|Pazar)\b",
                         " ", summary)
        summary = re.sub(r"Daha Fazla Oku|Devamını Oku", " ", summary, flags=re.I)
        summary = re.sub(r"[\s,·]+", " ", summary).strip()[:400]
        if summary == it["title"]:
            summary = ""
        entries.append(SimpleNamespace(
            title=it["title"], summary=summary, link=it["link"], id=it["link"],
            published_parsed=pub, updated_parsed=None))
    return entries


def _parse_osd(page: str) -> list:
    # 卡片順序：圖片連結 → 日期 → <h2> 標題連結（/haberler/252）→ 摘要
    return _parse_listing(page, r"(?:https?://(?:www\.)?osd\.org\.tr)?/haberler/\d+",
                          "https://www.osd.org.tr", "before")


def _parse_taysad(page: str) -> list:
    # 卡片順序：圖片連結 → <h4> 標題連結（/tr/haber/slug）→ 日期「01 Temmuz 2026, Çarşamba」→ 摘要
    return _parse_listing(page, r"(?:https?://(?:www\.)?taysad\.org\.tr)?/tr/haber/[a-z0-9-]+",
                          "https://taysad.org.tr", "after")


def _parse_bloomberght(page: str) -> list:
    """列表頁每則是一個 <a href=".../slug-3789232" title="標題">，連結內文是
    「標題＋摘要」。網址尾巴一定是 6～8 位數的文章編號，靠這個跟選單、
    行情頁連結區分。頁面上沒有日期，交給 SEEN 跨天去重。"""
    anchor_re = re.compile(
        r"<a\b[^>]*href=[\"'](?P<href>(?:https?://www\.bloomberght\.com)?/[a-z0-9-]+-\d{6,8})[\"'][^>]*>(?P<inner>.*?)</a>",
        re.S | re.I)
    # 屬性值可能用雙引號包、裡面夾單引號（THY'den），兩種引號分開比對
    title_re = re.compile(r"""\btitle=(?:"(?P<a>[^"]*)"|'(?P<b>[^']*)')""", re.I)
    entries, seen = [], set()
    for m in anchor_re.finditer(page):
        href = m.group("href")
        link = href if href.startswith("http") else "https://www.bloomberght.com" + href
        if link in seen:
            continue
        inner = _strip_tags(m.group("inner"))
        tm = title_re.search(m.group(0))
        title = (html_lib.unescape(tm.group("a") or tm.group("b") or "").strip()
                 if tm else "") or inner
        if len(title) < 8:
            continue
        seen.add(link)
        summary = inner[len(title):].strip() if inner.startswith(title) else inner
        entries.append(SimpleNamespace(
            title=title, summary=summary[:400], link=link, id=link,
            published_parsed=None, updated_parsed=None))
    return entries


HTML_PARSERS = {
    "Bloomberg HT": _parse_bloomberght,
    "OSD（汽車製造商協會）": _parse_osd,
    "TAYSAD（汽車零組件供應商協會）": _parse_taysad,
}

def tr_norm(text: str) -> str:
    """
    土文比對用正規化。

    Python 的 str.lower() 依英文規則處理，"İ" 會變成 "i" + U+0307 組合附加點，
    於是 "İTHALAT".lower() 得到 "i\u0307thalat"，用 "ithalat" 比對會失配。
    土文標題常用全大寫或首字大寫，İ 出現頻率極高（ihracat / ithalat / sanayi /
    İstanbul / TİM…），不處理會漏掉大量新聞。

    一併把土文特有字母折成 ASCII，讓關鍵字可以純 ASCII 書寫、兩邊都比對得到。
    """
    t = text.lower().replace("\u0307", "")      # 去掉組合附加點
    for a, b in (("ı", "i"), ("ş", "s"), ("ğ", "g"),
                 ("ü", "u"), ("ö", "o"), ("ç", "c"), ("â", "a")):
        t = t.replace(a, b)
    return t


# 主題分類（以 tr_norm 後的形式書寫，純 ASCII）。
# 「分類」不是「過濾」——不相關的丟掉，相關的標籤化，由人決定看哪一類。
# 國防工業在 Türkiye 成長最快，直接濾掉會連帶丟掉零組件的機會。
#
# 詞尾 * = 允許接土文詞尾變化（ihracat* 會命中 ihracatı/ihracatın/ihracatta）
# 無 * = 必須整個詞相符
#
# 土文是黏著語，直接用子字串比對會出事（2026-08-29 實測）：
#   iha  → 命中 cihaz（設備）、ihale（招標），害國防類灌水到 35%
#   kur  → 命中 kurul、kurum、kuruluş，害總經類灌水
#   kota → 命中 kotarmak
# 所以短詞一律用整詞比對。
TOPICS = {
    "trade": [
        "ihracat*", "ithalat*", "dis ticaret*", "gumruk*", "tarife*", "kota",
        "lojistik*", "konteyner*", "serbest ticaret*", "damping", "antidamping*",
        "export*", "import*", "trade", "tariff*", "customs", "quota*",
    ],
    "macro": [
        "enflasyon*", "tufe", "ufe", "uretici fiyat*", "faiz*", "merkez bankasi*",
        # 單獨的 dolar / euro 訊號太弱：轉會費、油價、金價都以美元歐元計價，
        # 2026-08-29 實測時把加拉塔薩雷轉會新聞拉進了總經類。改用帶「匯率」的詞組。
        "doviz", "doviz kur*", "dolar kur*", "dolar/tl", "euro bolgesi",
        "buyume", "issizlik*", "butce*", "cari acik*", "politika faizi",
        "inflation", "central bank", "interest rate*", "gdp", "unemployment",
        "budget", "current account",
        # 2026-08-29 實機第一次真跑，發現整批經濟版新聞（股市、銀行、資本市場）
        # 完全沒有主題詞可命中，「採用」數是 0——原本清單只顧到嚴格定義的貿易/
        # 總經/產業/國防四類，漏掉一般財經新聞最常見的字，照實測落網標題補上。
        # 注意：不能用裸的 "hisse*"——「hisseden」（動詞 hissetmek「感覺」的
        # 變化形）跟「hisse」（股票/股份）字面上長一樣，2026-08-29 實測把一則
        # 球員傷退新聞（"ağrı hisseden"＝感到疼痛）誤判成 macro。改用明確指向
        # 股票語境的詞組，不用裸字根。
        "piyasa*", "borsa*", "hisse senedi*", "hissedar*", "hisseleri",
        "hissesi", "sermaye*", "kredi*", "banka*",
        "yatirimci*", "spk", "bddk", "gsyh",
        "market", "stock exchange", "shares", "capital", "credit",
        "bank", "investor",
        # 2026-08-31 實測：「Türkiye's economy grows 2.3 percent」「Rusya ve
        # ABD, finans alanındaki işbirliğini görüştü」這類明顯是經濟/金融
        # 新聞的標題完全沒命中——清單裡從頭到尾沒有「經濟／economy」本身，
        # 也沒有「grow／growth」「finans／financial」，照實測落網標題補上。
        "ekonomi*", "economy", "economic", "grow*", "growth",
        "finans*", "financial",
    ],
    "industry": [
        "sanayi*", "uretim*", "otomotiv*", "tekstil*", "makine*", "kimya*",
        "elektronik*", "fabrika*", "yatirim*", "kapasite*", "teknopark*",
        "industry", "industrial", "manufactur*", "automotive", "textile*",
        "machinery", "factory", "investment*",
        # 2026-08-29 實測：「Türkiye's crude steel output rises 7 percent」
        # 這種標題完全沒命中，原清單漏了「產出/鋼鐵/能源」這幾個常見詞。
        "celik*", "petrol*", "enerji*", "steel", "oil", "energy",
        # 2026-08-31 實測：「European gas prices hit highest since January
        # 2023」這種天然氣價格新聞沒命中——原清單只有 oil/energy，漏了 gas。
        # 裸字 "gas" 誤判風險較低（tr_norm 後跟其他字混淆機率不高），但仍
        # 優先用明確詞組降低風險。
        "dogalgaz*", "natural gas", "gas",
        # 2026-08-29 新增，配合 Webrazzi（科技/新創/ICT）來源，
        # 原本清單完全沒有科技/新創詞，Webrazzi 內容會整批被判「無主題」丟掉。
        "teknoloji*", "girisim*", "yazilim*", "yapay zeka", "surdurulebilir*",
        "e-ticaret", "startup*", "fintech", "siber guvenlik*", "bulut bilisim*",
        "technology", "software", "artificial intelligence", "e-commerce",
        "cybersecurity", "cloud computing",
    ],
    "defense": [
        "savunma sanayi*", "iha", "siha", "insansiz hava",
        "muhimmat*", "askeri", "asker", "aselsan", "baykar", "tusas", "roketsan",
        "defense", "defence", "drone*", "military",
    ],
}


def _compile(word: str):
    """詞尾 * 允許接後綴，否則整詞比對。tr_norm 後只剩 ASCII，\b 可靠。"""
    if word.endswith("*"):
        return re.compile(r"\b" + re.escape(word[:-1]) + r"\w*")
    return re.compile(r"\b" + re.escape(word) + r"\b")


TOPIC_PATTERNS = {
    topic: [(w, _compile(tr_norm(w))) for w in words]
    for topic, words in TOPICS.items()
}

# ── 與 Türkiye 的相關性 ──
# 主題分類只回答「這是不是經濟新聞」，不回答「這關不關土耳其的事」。
# 2026-08-29 實測：尼日槍擊、尼泊爾水災、烏克蘭無人機、Fed 談話全都通過了
# 主題分類，因為它們確實含 military / inflation / drone。少的是這個維度。
TR_MARKERS = [
    "turkiye", "turk*", "turkish", "ankara", "istanbul", "anadolu",
    "tcmb", "tuik", "bist", "borsa istanbul", "lira", "tl",
    "erdogan", "simsek", "karahan", "bolat", "kacir", "yilmaz",
    # 政府機關詞。曾經用過 "bakan*" 前綴比對，2026-08-29 實測踩雷：
    # bakan 同時是動詞 bakmak 的現在分詞（「看的人」），日常用語極常見，
    # 害挪威無人機、藥品定價等新聞全被判成土耳其相關（14 則裡誤收 4 則）。
    # 改用明確指向本國機關的整詞／詞組。
    "bakanlik*", "bakanligi", "sanayi bakan*", "ticaret bakan*",
    "hazine ve maliye", "meclis", "resmi gazete", "cumhurbaskan*",
    "spk", "bddk", "epdk", "rekabet kurumu",
    "aselsan", "baykar", "tusas", "roketsan", "tofas", "ford otosan",
    "tim", "tobb", "iso", "ithib", "itkib", "oib", "osd",
]
TR_PATTERNS = [(w, _compile(tr_norm(w))) for w in TR_MARKERS]

# 主題優先序：貿易最有價值；國防排在工業前面，避免含 sanayi 就被歸成一般工業
TOPIC_PRIORITY = ["trade", "defense", "macro", "industry"]


# ─────────────────────────────────────────────
# 台土五大重點產業（2026-09-23 新增）
#   跟上面的 TOPICS 是兩個不同維度：TOPICS 決定「要不要收」，這裡決定
#   「是不是辦事處最關注的產業」。命中的新聞會帶 tw_sectors 欄位，排序時
#   排在同類新聞最前面，generate_analysis.py 也會要 Gemini 優先寫這些。
#   只命中這裡、沒命中 TOPICS 的新聞（例如「servo 減速機新品」）也會收，
#   主題記為 industry。
#
#   2026-09-23 用 09-10～09-23 的實際資料試跑過，下面這些字會誤判，刻意不用：
#     pres*（→ president）、car*（→ card／care／carbon）、ev（土文「家」）、
#     dokum*（→ dokuma 紡織）、amb（→ AMB 歐洲
#     央行的土文縮寫）、res（太短）、green／yesil*（太泛，綠色什麼都有）、
#     yapay zeka（多半是 OpenAI 等消費科技新聞，跟工廠端無關）。
# ─────────────────────────────────────────────
TW_SECTORS = {
    "工具機與金屬加工": [
        "takim tezgah*", "tezgah*", "cnc", "talasli imalat*", "talasli isleme*",
        "metal isleme*", "sac isleme*", "sac metal", "lazer kesim*", "abkant*",
        "pres makine*", "kaynak makine*", "isleme merkez*", "reduktor*",
        "makine imalat*", "makina imalat*", "makine sanayi*", "makina sanayi*",
        "makine ihracat*", "makina ihracat*", "makine sektor*", "makina sektor*",
        "makinecil*", "mib", "makfed", "maktek", "fanuc",
        "machine tool*", "machining", "metalworking", "sheet metal",
    ],
    "智慧製造與工業4.0": [
        "endustri 4.0", "sanayi 4.0", "industry 4.0",
        "akilli fabrika*", "akilli uretim*", "smart factory", "smart manufacturing",
        "otomasyon*", "automation", "robot*", "cobot*", "dijital ikiz*",
        "digital twin", "iiot", "endustriyel iot", "nesnelerin interneti",
        "plc", "scada", "servo*", "sensor*", "kestirimci bakim*",
        "predictive maintenance", "agv", "amr", "endustriyel yapay zeka",
        "industrial ai",
    ],
    "汽車零組件與EV供應鏈": [
        "otomotiv*", "automotive", "automaker*", "carmaker*", "auto output",
        "yan sanayi*", "tedarik sanayi*", "yedek parca*", "oto parca*",
        "auto part*", "spare part*", "elektrikli arac*", "electric vehicle*",
        "electric car*", "sarj istasyon*", "sarj agi", "sarj islem*",
        "charging station*", "batarya*", "battery", "batteries", "togg",
        "tofas", "ford otosan", "oyak renault", "hyundai assan", "byd",
        "osd", "taysad", "oib", "otomobil uretim*", "otomobil ihracat*",
        "arac uretim*", "hibrit arac*",
    ],
    "塑膠與包裝機械": [
        "plastik*", "plastic*", "ambalaj*", "packaging", "kaucuk*", "rubber",
        "polimer*", "polymer*", "enjeksiyon makine*", "injection molding",
        "ekstruder*", "ekstruzyon*", "extrusion", "petkim", "pagev", "pagder",
        "paketleme makine*", "paletleme*", "kalip sanayi*", "kompaund*",
    ],
    "綠能、儲能與回收": [
        "yenilenebilir*", "renewable*", "gunes enerji*", "gunes panel*",
        "gunes santral*", "ges", "solar", "ruzgar enerji*", "ruzgar santral*",
        "ruzgar turbin*", "wind power", "wind energy", "wind turbine*",
        "wind farm*", "wind supply chain*", "offshore wind", "onshore wind",
        "enerji depolama*", "energy storage", "depolamali", "geri donusum*",
        "recycl*", "sifir atik*", "zero waste", "dongusel ekonomi*",
        "circular economy", "hidrojen*", "hydrogen", "skdm", "cbam",
        "karbon ayak iz*", "emisyon ticaret*", "jeotermal*", "geothermal",
        "biyokutle*",
    ],
}
TW_SECTOR_PATTERNS = {k: [(w, _compile(tr_norm(w))) for w in v]
                      for k, v in TW_SECTORS.items()}


# 來源本身就代表某個重點產業：標題常常只寫「Ağustos 2026 Sonuçları
# Açıklandı!」這種完全沒有關鍵字的句子（2026-09-23 實測 OSD 的 8 月產銷
# 數據就因此被當成「沒命中任何主題」丟掉），所以直接依來源補上產業。
SOURCE_SECTORS = {
    "OSD（汽車製造商協會）": ["汽車零組件與EV供應鏈"],
    "TAYSAD（汽車零組件供應商協會）": ["汽車零組件與EV供應鏈"],
    "MİB（機械製造商協會）": ["工具機與金屬加工"],
    "plastonline": ["塑膠與包裝機械"],
}


def detect_tw_sectors(blob: str, source: str = "") -> list:
    """blob 要先經過 tr_norm()。回傳命中的重點產業名稱（依 TW_SECTORS 順序）。"""
    hit = set(SOURCE_SECTORS.get(source, []))
    hit |= {name for name, pats in TW_SECTOR_PATTERNS.items()
            if any(pat.search(blob) for _, pat in pats)}
    return [name for name in TW_SECTORS if name in hit]


def _news_sort_key(x: dict):
    """土耳其相關 → 重點產業 → 當日（非 late）→ 新的在前。"""
    return (x["scope"] != "domestic", not x.get("tw_sectors"), x["late"],
            "" if x["published"] is None else
            "".join(chr(255 - ord(c)) for c in x["published"]))


def discover_feeds() -> dict:
    """逐一測試候選 RSS 位址，回報哪個能用。第一次部署務必跑一次。"""
    results = {}
    for name, urls in FEED_CANDIDATES.items():
        results[name] = []
        for u in urls:
            try:
                r = requests.get(u, headers=UA, timeout=TIMEOUT)
                ok = r.status_code == 200 and (
                    b"<rss" in r.content[:2000] or b"<feed" in r.content[:2000]
                )
                n = r.content.count(b"<item") + r.content.count(b"<entry")
                results[name].append(
                    {"url": u, "status": r.status_code, "is_feed": ok, "items": n}
                )
            except Exception as e:
                results[name].append({"url": u, "error": str(e)[:120]})
    return results


# ─────────────────────────────────────────────
# 跨天去重快取
#   起因：OSD／TAYSAD 這兩個 rss.app feed 完全沒有 pubDate，「沒日期就不會
#   過期」的邏輯會讓它們的項目每天重複收錄。順便也堵住既有的漏洞——
#   Bloomberg HT／Hürriyet Ekonomi 用 120 小時視窗，同一則舊聞理論上可以
#   連續 5 天出現在簡報裡，只是先前沒被注意到。
#   用 (source, guid或link) 當 key，記錄第一次看到的日期；超過
#   SEEN_PRUNE_DAYS 沒再出現就從快取清掉，檔案大小不會無限成長。
# ─────────────────────────────────────────────
SEEN_PATH = OUT_DIR / "_seen_ids.json"
SEEN_PRUNE_DAYS = 14


def _load_seen() -> dict:
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_seen(seen: dict) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")


def fetch_news(since_hours: int = 24, feeds: dict | None = None) -> tuple[list, dict]:
    """
    回傳 (新聞清單, 各來源診斷)。

    改用 requests 抓取後再交給 feedparser 解析——feedparser 自行連線時
    有數個來源會靜默回 0 筆（2026-08-29 實測：Bloomberg HT、Hürriyet Ekonomi、
    Daily Sabah、Hürriyet Daily News 皆為 0，但同樣位址用 requests 都能取得
    20–100 筆）。靜默的零最難察覺，所以一律回報每個來源的三段數字。
    """
    if feedparser is None:
        return [], {"_error": "未安裝 feedparser"}

    feeds = feeds or {k: v[0] for k, v in FEED_CANDIDATES.items()}
    now = dt.datetime.now(dt.timezone.utc)
    today_iso = now.date().isoformat()
    items, diag = [], {}
    seen = _load_seen()

    headers = {**UA, "Accept": "application/rss+xml, application/xml, text/xml, */*"}

    for source, url in feeds.items():
        hours = FEED_MAX_HOURS.get(source, since_hours)
        cutoff = now - dt.timedelta(hours=hours)
        st = {"total": 0, "recent": 0, "kept": 0, "domestic": 0,
              "window_hours": hours,
              "newest": None, "oldest": None, "no_date": 0,
              "dedup_skipped": 0, "tw_sector": 0, "no_topic_samples": [],
              "error": None}
        try:
            parser = HTML_PARSERS.get(source)
            r = requests.get(url, headers=UA if parser else headers, timeout=TIMEOUT)
            r.raise_for_status()
            if parser:
                entries = parser(r.content.decode("utf-8", errors="replace"))
            else:
                entries = feedparser.parse(r.content).entries
        except Exception as e:
            st["error"] = str(e)[:150]
            diag[source] = st
            continue

        st["total"] = len(entries)
        for e in entries:
            pub = None
            if getattr(e, "published_parsed", None):
                pub = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
            elif getattr(e, "updated_parsed", None):
                pub = dt.datetime(*e.updated_parsed[:6], tzinfo=dt.timezone.utc)
            if pub:
                iso = pub.isoformat()
                if st["newest"] is None or iso > st["newest"]:
                    st["newest"] = iso
                if st["oldest"] is None or iso < st["oldest"]:
                    st["oldest"] = iso
            else:
                st["no_date"] += 1
            if pub and pub < cutoff:
                continue
            st["recent"] += 1

            title = (getattr(e, "title", "") or "").strip()
            summary = re.sub(r"<[^>]+>", "", getattr(e, "summary", "") or "").strip()
            blob = tr_norm(f"{title} {summary}")

            topics, matched = [], []
            for topic, pats in TOPIC_PATTERNS.items():
                hit = [w for w, pat in pats if pat.search(blob)]
                if hit:
                    topics.append(topic)
                    matched += hit
            sectors = detect_tw_sectors(blob, source)
            if sectors and not topics:
                # 只命中重點產業（例如專業媒體的設備新品）也收，歸在 industry
                topics.append("industry")
                matched += [w for name in sectors
                            for w, pat in TW_SECTOR_PATTERNS[name] if pat.search(blob)]
            if not topics:
                # 除錯用：每個來源最多留 5 則沒命中任何主題的標題樣本，
                # 才知道 TOPICS 關鍵字清單漏掉了什麼，不用猜的加關鍵字。
                if len(st["no_topic_samples"]) < 5:
                    st["no_topic_samples"].append(title)
                continue

            # 沒有日期的來源（目前是 OSD／TAYSAD）靠日期過期沒用，改靠這裡
            # 的跨天去重擋掉重複——同一個 guid/link 只在第一次出現時收錄。
            uid = f"{source}::{getattr(e, 'id', None) or getattr(e, 'link', '') or title}"
            if uid in seen:
                st["dedup_skipped"] += 1
                continue
            seen[uid] = today_iso

            tr_hits = [w for w, pat in TR_PATTERNS if pat.search(blob)]
            scope = "domestic" if (tr_hits or source in ALWAYS_DOMESTIC_SOURCES) else "global"

            primary = next((t for t in TOPIC_PRIORITY if t in topics), topics[0])
            st["kept"] += 1
            if scope == "domestic":
                st["domestic"] += 1
            if sectors:
                st["tw_sector"] += 1
            items.append({
                "source": source,
                "title": title,
                "url": getattr(e, "link", ""),
                "published": pub.isoformat() if pub else None,
                "no_date": pub is None,
                "late": bool(pub and pub < now - dt.timedelta(hours=since_hours)),
                "summary": summary[:400],
                "scope": scope,
                "tr_markers": sorted(set(tr_hits))[:5],
                "primary_topic": primary,
                "topics": topics,
                "matched": sorted(set(matched))[:6],
                "tw_sectors": sectors,
            })
        diag[source] = st

    # 清掉太久沒再出現的舊 id，檔案大小才不會一直長
    cutoff_date = (now - dt.timedelta(days=SEEN_PRUNE_DAYS)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff_date}
    _save_seen(seen)

    items.sort(key=_news_sort_key)
    return items, diag


# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true", help="只探測 RSS 位址")
    ap.add_argument("--no-evds", action="store_true", help="跳過 EVDS")
    ap.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()

    if args.discover:
        print(json.dumps(discover_feeds(), ensure_ascii=False, indent=2))
        return

    now_trt = dt.datetime.now(TRT)
    payload = {
        "generated_at": now_trt.isoformat(),
        "generated_at_label": now_trt.strftime("%Y-%m-%d %H:%M TRT"),
        "fx": None,
        "macro": {},
        "news": [],
        "news_diagnostics": {},
        "errors": [],
    }

    # 匯率
    try:
        payload["fx"] = fetch_tcmb_fx()
        print(f"✓ 匯率 {payload['fx']['date']}", file=sys.stderr)
    except Exception as e:
        payload["errors"].append(f"tcmb_fx: {e}")
        print(f"✗ 匯率: {e}", file=sys.stderr)

    # TWD/TRY 交叉匯率（需要 fx 先抓成功，拿裡面的 USD/TRY 官方賣出價當基準）
    if payload["fx"]:
        usd_sel = (payload["fx"].get("rates", {}).get("USD") or {}).get("per_unit_selling")
        cross = fetch_twd_try_cross(usd_sel)
        payload["fx"]["twd_try_cross"] = cross
        if cross:
            print(f"✓ TWD/TRY 交叉匯率 {cross['try_per_twd']:.4f}", file=sys.stderr)
        else:
            payload["errors"].append("twd_try_cross: 抓取失敗或 USD/TRY 尚未取得")

    # 布蘭特原油
    brent = fetch_brent_oil()
    payload["brent_oil"] = brent
    if brent:
        _src = {
            "yahoo_futures": "Yahoo 期貨",
            "stooq_futures": "stooq 期貨",
            "eia_api": "EIA API 現貨",
            "datahub_csv": "datahub CSV 現貨備援",
        }.get(brent.get("source_kind"), brent.get("source_kind") or "未知來源")
        print(f"✓ 布蘭特原油 ${brent['usd_per_barrel']:.2f}（{brent['date']}，"
              f"{_src}，落後 {brent['age_days']} 天）", file=sys.stderr)
        ch = brent.get("changes") or {}
        parts = []
        for label, key in (("週", "week"), ("月", "month"), ("年初至今", "ytd")):
            c = ch.get(key)
            parts.append(f"{label} {c['pct']:+.1f}%（基準 {c['base_date']} ${c['base_price']:.2f}）"
                          if c else f"{label} —")
        print("   ↳ " + "  ".join(parts), file=sys.stderr)
        if brent.get("fallback_reasons"):
            # 有拿到數字但不是第一順位，也記進 errors，免得像 09-09～09-23
            # 那樣退到備援兩週都沒人發現。
            payload["errors"].append(
                "brent_oil: 退到備援 " + brent["source_kind"] + "（"
                + "；".join(brent["fallback_reasons"]) + "）")
    else:
        payload["errors"].append("brent_oil: 抓取失敗")

    # 貿易俱樂部土耳其報告（半年一次，大部分日子這裡是 None，正常現象）
    eximclub = fetch_eximclub_turkey_report()
    payload["eximclub_turkey_report"] = eximclub
    if eximclub:
        print(f"✓ 貿易俱樂部新報告：{eximclub['date']} {eximclub['title']}", file=sys.stderr)

    # EVDS
    key = (os.environ.get("EVDS_API_KEY") or "").strip()
    if not args.no_evds and key:
        end = dt.date.today()
        # 2026-09-09 從 400 天放寬到 500 天。月頻序列要 13 筆才算得出最新
        # 一個月的年增率、14 筆才算得出「前一個月」的年增率；400 天實測只
        # 拿到 13 筆，所以首頁的 CPI「前值」一直是空的（後來被手填的官方
        # 序列蓋過去才看不出來）。核心通膨 B 指標接進來會踩到同一個坑。
        start = end - dt.timedelta(days=500)
        # 季度序列（目前只有 gdp_growth）算年增率要往前推 4 筆，400 天大約
        # 只夠抓到 4-5 季，index 不夠長會讓年增率算不出來（2026-09-08 實測
        # 踩到：季增率有算出來但年增率是空的，因為季增只需要往前推 1 筆，
        # 資料再少都夠用，年增率的門檻比較高）。季度序列改抓 3 年（約
        # 12-13 季），留足夠的緩衝。
        start_quarterly = end - dt.timedelta(days=1100)
        for name, (code, freq) in EVDS_SERIES.items():
            series_start = start_quarterly if freq == 6 else start
            try:
                payload["macro"][name] = fetch_evds(code, series_start, end, key, freq)[-14:]
                print(f"✓ EVDS {name}", file=sys.stderr)
                if name == "trade_imports" and payload["macro"].get("trade_exports") and payload["macro"][name]:
                    exp_v = payload["macro"]["trade_exports"][-1].get("value")
                    imp_v = payload["macro"][name][-1].get("value")
                    if exp_v is not None and imp_v is not None:
                        print(
                            f"   ↳ 出口 {exp_v:.0f} － 進口 {imp_v:.0f} = "
                            f"差額 {exp_v - imp_v:.0f}（單位百萬美元，人工核對："
                            f"應落在 -4000 ~ -12000 附近才合理）",
                            file=sys.stderr,
                        )
            except Exception as e:
                payload["errors"].append(f"evds:{name}: {e}")
                print(f"✗ EVDS {name}: {e}", file=sys.stderr)
    elif not args.no_evds:
        payload["errors"].append("evds: 未設定 EVDS_API_KEY")
        print("! 未設定 EVDS_API_KEY，跳過總經序列", file=sys.stderr)

    # 新聞
    payload["news"], payload["news_diagnostics"] = fetch_news(args.hours)
    print(f"✓ 新聞 {len(payload['news'])} 則", file=sys.stderr)
    for src, st in payload["news_diagnostics"].items():
        if st.get("error"):
            print(f"    ✗ {src}: {st['error']}", file=sys.stderr)
        else:
            flag = ""
            if st["total"] == 0:
                flag = "  ← 抓到 0 筆，請查位址"
            elif st["recent"] == 0:
                flag = f"  ← 全部超過時間範圍，最新一筆 {(st['newest'] or '無日期')[:16]}"
            print(f"    {src}: 共{st['total']} / 近期{st['recent']} / "
                  f"採用{st['kept']} / 其中土耳其相關{st['domestic']} / "
                  f"去重擋掉{st.get('dedup_skipped', 0)} / "
                  f"重點產業{st.get('tw_sector', 0)}{flag}",
                  file=sys.stderr)
            if st["kept"] == 0 and st.get("no_topic_samples"):
                print(f"        ↳ 沒命中任何主題的標題樣本："
                      f"{st['no_topic_samples']}", file=sys.stderr)
    from collections import Counter
    dom = [n for n in payload["news"] if n["scope"] == "domestic"]
    c = Counter(n["primary_topic"] for n in dom)
    if c:
        print(f"    土耳其相關 {len(dom)} 則，主題分布: "
              + "  ".join(f"{k}={v}" for k, v in c.most_common()), file=sys.stderr)
    sc = Counter(s_ for n in dom for s_ in (n.get("tw_sectors") or []))
    print(f"    台土重點產業（土耳其相關）: "
          + ("  ".join(f"{k}={v}" for k, v in sc.most_common()) if sc else "0 則"),
          file=sys.stderr)
    g = len(payload["news"]) - len(dom)
    if g:
        print(f"    國際新聞 {g} 則（已標 scope=global，預設不進簡報）", file=sys.stderr)

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{now_trt:%Y-%m-%d}.json"

    # 2026-09-23 同一天重跑會把新聞洗成 0 則：早上那次已經把當天新聞記進
    # _seen_ids.json，晚上手動再跑一次時，同樣的新聞全被跨天去重擋掉，
    # 接著覆蓋掉早上的檔案，首頁跟 AI 分析就變成「今天沒有新聞」。
    # 所以當天檔案已經存在時，把舊檔的新聞併進來（同來源＋同網址只留一則），
    # 匯率、油價等數字仍然用這次抓到的最新值。
    if out.exists():
        try:
            prev = json.loads(out.read_text(encoding="utf-8"))
            prev_news = prev.get("news") or []
            have = {(n.get("source"), n.get("url") or n.get("title"))
                    for n in payload["news"]}
            carried = [n for n in prev_news
                       if (n.get("source"), n.get("url") or n.get("title")) not in have]
            for n in carried:
                if "tw_sectors" not in n:   # 舊版程式抓的，補上重點產業標記
                    n["tw_sectors"] = detect_tw_sectors(
                        tr_norm(f"{n.get('title', '')} {n.get('summary', '')}"),
                        n.get("source", ""))
            if carried:
                payload["news"] = carried + payload["news"]
                payload["news"].sort(key=_news_sort_key)
                print(f"↺ 今天已跑過一次，沿用先前抓到的 {len(carried)} 則新聞"
                      f"（合計 {len(payload['news'])} 則）", file=sys.stderr)
        except Exception as e:
            print(f"! 合併當天既有新聞失敗，改為覆蓋：{e}", file=sys.stderr)

    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out}", file=sys.stderr)

    if payload["errors"]:
        print(f"完成，但有 {len(payload['errors'])} 個錯誤", file=sys.stderr)


if __name__ == "__main__":
    main()
