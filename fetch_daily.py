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
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

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

    # TWD 不在 TCMB 清單裡，需另一個 USD/TWD 來源做交叉計算
    return out


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
    "Bloomberg HT":         ["https://www.bloomberght.com/rss"],                        # 20
    "Hürriyet Ekonomi":     ["https://www.hurriyet.com.tr/rss/ekonomi"],                # 100
    "Sözcü Ekonomi":        ["https://www.sozcu.com.tr/feeds-rss-category-ekonomi"],    # 50
    "Webrazzi":             ["https://webrazzi.com/feed/"],                             # 20，科技/新創/ICT
    # rss.app 自訂 feed，discover 抓內容後確認實際來源如下：
    # LLwADw0l2yOISF9h.xml → OSD 官網 + Instagram 混合（真正的新聞條目很少，
    #   多數是社群賀節／獲獎貼文，訊噪比不高，但仍是汽車產業一手來源）
    "OSD（汽車製造商協會）":  ["https://rss.app/feeds/LLwADw0l2yOISF9h.xml"],             # 6
    # XpvzQGRjx9yJNdPh.xml → TAYSAD，內容幾乎全是內部教育訓練活動列表，
    #   不是新聞快訊。多數項目本來就不含 TOPICS 關鍵字（如「團隊管理」
    #   「現金流管理」等課程名稱），會被主題分類自然濾掉，留下來的通常
    #   才是真正跟產業/汽車相關的活動或公告。
    "TAYSAD（汽車零組件供應商協會）": ["https://rss.app/feeds/XpvzQGRjx9yJNdPh.xml"],    # 25
}

# 這兩個公會 feed（OSD、TAYSAD）完全沒有 pubDate/updated 欄位，見下方
# fetch_news() 內的 SEEN 去重快取——沒有這層，這兩個來源的項目會被
# 「沒日期＝不會過期」的判斷邏輯每天重複收錄，永遠不會消失。
NO_DATE_SOURCES = {"OSD（汽車製造商協會）", "TAYSAD（汽車零組件供應商協會）"}

# 這兩家是純土耳其產業公會，逐則內文本身常常不會提到「Türkiye／土耳其」
# 這類 TR_MARKERS 關鍵字（例如 TAYSAD 的課程名稱），會被 scope 判斷誤判成
# global。這兩家的存在本身就代表土耳其相關，不透過 TR_MARKERS 判斷，
# 直接強制視為 domestic。
ALWAYS_DOMESTIC_SOURCES = {"OSD（汽車製造商協會）", "TAYSAD（汽車零組件供應商協會）"}

# 這兩家的 feed 停在數天前（2026-08-29 實測：最新一筆各為 08-25、08-26），
# 推測是快取的靜態檔而非即時產生。用 24 小時窗口它們永遠是 0，
# 所以個別放寬，抓到的項目會標 late=true，不冒充當日新聞。
FEED_MAX_HOURS = {
    "Bloomberg HT": 120,
    "Hürriyet Ekonomi": 120,
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
    ],
    "industry": [
        "sanayi*", "uretim*", "otomotiv*", "tekstil*", "makine*", "kimya*",
        "elektronik*", "fabrika*", "yatirim*", "kapasite*", "teknopark*",
        "industry", "industrial", "manufactur*", "automotive", "textile*",
        "machinery", "factory", "investment*",
        # 2026-08-29 實測：「Türkiye's crude steel output rises 7 percent」
        # 這種標題完全沒命中，原清單漏了「產出/鋼鐵/能源」這幾個常見詞。
        "celik*", "petrol*", "enerji*", "steel", "oil", "energy",
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
              "dedup_skipped": 0, "no_topic_samples": [], "error": None}
        try:
            r = requests.get(url, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            d = feedparser.parse(r.content)
        except Exception as e:
            st["error"] = str(e)[:150]
            diag[source] = st
            continue

        st["total"] = len(d.entries)
        for e in d.entries:
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
            })
        diag[source] = st

    # 清掉太久沒再出現的舊 id，檔案大小才不會一直長
    cutoff_date = (now - dt.timedelta(days=SEEN_PRUNE_DAYS)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff_date}
    _save_seen(seen)

    items.sort(key=lambda x: (x["scope"] != "domestic", x["late"],
                              "" if x["published"] is None else
                              "".join(chr(255 - ord(c)) for c in x["published"])))
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

    # EVDS
    key = os.environ.get("EVDS_API_KEY")
    if not args.no_evds and key:
        end = dt.date.today()
        start = end - dt.timedelta(days=400)
        for name, (code, freq) in EVDS_SERIES.items():
            try:
                payload["macro"][name] = fetch_evds(code, start, end, key, freq)[-14:]
                print(f"✓ EVDS {name}", file=sys.stderr)
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
                  f"去重擋掉{st.get('dedup_skipped', 0)}{flag}",
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
    g = len(payload["news"]) - len(dom)
    if g:
        print(f"    國際新聞 {g} 則（已標 scope=global，預設不進簡報）", file=sys.stderr)

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / f"{now_trt:%Y-%m-%d}.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {out}", file=sys.stderr)

    if payload["errors"]:
        print(f"完成，但有 {len(payload['errors'])} 個錯誤", file=sys.stderr)


if __name__ == "__main__":
    main()
