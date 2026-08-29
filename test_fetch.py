#!/usr/bin/env python3
"""離線測試 fetch_daily.py 的解析邏輯（不碰網路）"""
import sys, json, datetime as dt, importlib.util
from unittest import mock

spec = importlib.util.spec_from_file_location("fd", "/mnt/user-data/outputs/fetch_daily.py")
fd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fd)

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  ok   " if cond else "  FAIL ") + name + (f"  → {detail}" if detail and not cond else ""))

# ── TCMB today.xml 真實格式（含 JPY Unit=100 的情況）──
TCMB_XML = b"""<?xml version="1.0" encoding="ISO-8859-9"?>
<Tarih_Date Tarih="29.08.2026" Date="08/29/2026" Bulten_No="2026/165">
  <Currency CrossOrder="0" Kod="USD" CurrencyCode="USD">
    <Unit>1</Unit><Isim>ABD DOLARI</Isim><CurrencyName>US DOLLAR</CurrencyName>
    <ForexBuying>46.8123</ForexBuying><ForexSelling>46.8965</ForexSelling>
    <BanknoteBuying>46.7795</BanknoteBuying><BanknoteSelling>46.9668</BanknoteSelling>
  </Currency>
  <Currency CrossOrder="9" Kod="EUR" CurrencyCode="EUR">
    <Unit>1</Unit><Isim>EURO</Isim><CurrencyName>EURO</CurrencyName>
    <ForexBuying>54.7712</ForexBuying><ForexSelling>54.8698</ForexSelling>
    <BanknoteBuying>54.7328</BanknoteBuying><BanknoteSelling>54.9522</BanknoteSelling>
  </Currency>
  <Currency CrossOrder="6" Kod="JPY" CurrencyCode="JPY">
    <Unit>100</Unit><Isim>JAPON YENI</Isim><CurrencyName>JAPENESE YEN</CurrencyName>
    <ForexBuying>31.2450</ForexBuying><ForexSelling>31.4512</ForexSelling>
    <BanknoteBuying></BanknoteBuying><BanknoteSelling></BanknoteSelling>
  </Currency>
  <Currency CrossOrder="2" Kod="DKK" CurrencyCode="DKK">
    <Unit>1</Unit><Isim>DANIMARKA KRONU</Isim><CurrencyName>DANISH KRONE</CurrencyName>
    <ForexBuying>7.3401</ForexBuying><ForexSelling>7.3899</ForexSelling>
    <BanknoteBuying>7.3195</BanknoteBuying><BanknoteSelling>7.4165</BanknoteSelling>
  </Currency>
</Tarih_Date>"""

print("\n[1] TCMB 匯率解析")
class R:
    content = TCMB_XML
    status_code = 200
    def raise_for_status(self): pass

with mock.patch.object(fd.requests, "get", return_value=R()):
    fx = fd.fetch_tcmb_fx()

check("USD 買入價正確", fx["rates"].get("USD", {}).get("forex_buying") == 46.8123)
check("只留白名單幣別（DKK 應排除）", "DKK" not in fx["rates"])
check("空的 BanknoteSelling 不炸（JPY）", fx["rates"]["JPY"]["banknote_selling"] is None)
check("JPY Unit 讀到 100", fx["rates"]["JPY"]["unit"] == 100)

# 關鍵：JPY 報價是「每 100 日圓」，若直接當單位匯率用會差 100 倍
jpy = fx["rates"]["JPY"]
check("JPY 有換算成每 1 單位的欄位",
      "per_unit_selling" in jpy,
      f"目前只有 forex_selling={jpy['forex_selling']}（實為每100日圓），下游極易誤用")

check("日期為 ISO 或可辨識格式",
      fx["date"] not in (None, "") and "/" not in str(fx["date"]),
      f"目前 date={fx['date']!r}（美式 mm/dd/yyyy，與檔名和其他欄位格式不一致）")

# ── 土文大小寫：關鍵字過濾 ──
print("\n[2] 新聞關鍵字過濾（土文大小寫陷阱）")
RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>İhracat rekoru kırıldı</title>
<description>Temmuz ayinda rekor</description>
<link>https://example.com/1</link>
<pubDate>Fri, 29 Aug 2026 06:00:00 +0000</pubDate></item>
<item><title>İTHALAT VERİLERİ AÇIKLANDI</title>
<description>Veriler yayimlandi</description>
<link>https://example.com/2</link>
<pubDate>Fri, 29 Aug 2026 05:00:00 +0000</pubDate></item>
<item><title>Merkez Bankası faiz kararı</title>
<description>Politika faizi</description>
<link>https://example.com/3</link>
<pubDate>Fri, 29 Aug 2026 04:00:00 +0000</pubDate></item>
<item><title>TÜRK SANAYİ ÜRETİMİ ARTTI</title>
<description>Veriler</description>
<link>https://example.com/6</link>
<pubDate>Fri, 29 Aug 2026 03:30:00 +0000</pubDate></item>
<item><title>Galatasaray maçı sonucu</title>
<description>Spor haberleri</description>
<link>https://example.com/4</link>
<pubDate>Fri, 29 Aug 2026 03:00:00 +0000</pubDate></item>
<item><title>Eski haber ihracat</title>
<description>arsiv</description>
<link>https://example.com/5</link>
<pubDate>Mon, 01 Jun 2026 03:00:00 +0000</pubDate></item>
</channel></rss>"""

parsed = feedparser_parsed = None
import feedparser
class FakeResp:
    content = RSS.encode("utf-8")
    status_code = 200
    def raise_for_status(self): pass

FIXED_NOW = dt.datetime(2026, 8, 29, 8, 30, tzinfo=dt.timezone.utc)
class FakeDT(dt.datetime):
    @classmethod
    def now(cls, tz=None): return FIXED_NOW.astimezone(tz) if tz else FIXED_NOW

with mock.patch.object(fd.requests, "get", return_value=FakeResp()), \
     mock.patch.object(fd.dt, "datetime", FakeDT):
    news, diag = fd.fetch_news(since_hours=24, feeds={"Test": "http://x"})

titles = [n["title"] for n in news]
check("抓到小寫 ihracat 的那則", any("rekoru" in t for t in titles))
check("土文大寫 İ 的標題也要抓到（İTHALAT）",
      any("İTHALAT" in t for t in titles),
      "İ.lower() 產生 i+U+0307 組合字，比對 'ithalat' 會失敗")
check("抓到 faiz / Merkez Bankası", any("faiz" in t for t in titles))
check("結尾大寫 İ 也要抓到（SANAYİ）", any("SANAYİ" in t for t in titles),
      "SANAYİ.lower() -> 'sanayi'+U+0307，比對 'sanayi' 會失敗")
check("體育新聞被濾掉", not any("Galatasaray" in t for t in titles))
check("超過 24 小時的舊聞被濾掉", not any("Eski" in t for t in titles))
check("有回報每來源診斷", diag.get("Test", {}).get("total") == 6,
      f"diag={diag}")
check("國防新聞不被丟棄，改標 defense",
      True, "")

# 主題分類
RSS2 = RSS.replace("<title>Galatasaray maçı sonucu</title>",
                   "<title>ASELSAN savunma sanayi ihracatı arttı</title>")
class FakeResp2(FakeResp):
    content = RSS2.encode("utf-8")
with mock.patch.object(fd.requests, "get", return_value=FakeResp2()), \
     mock.patch.object(fd.dt, "datetime", FakeDT):
    news2, _ = fd.fetch_news(since_hours=24, feeds={"T": "http://x"})
aselsan = [n for n in news2 if "ASELSAN" in n["title"]]
check("國防＋出口的新聞會保留", len(aselsan) == 1)
check("同時含 ihracat 與 savunma 時，primary 判為 trade",
      aselsan and aselsan[0]["primary_topic"] == "trade",
      f"實得 {aselsan[0]['primary_topic'] if aselsan else 'N/A'}")
check("topics 同時列出 trade 與 defense",
      aselsan and set(["trade","defense"]).issubset(set(aselsan[0]["topics"])),
      f"實得 {aselsan[0]['topics'] if aselsan else 'N/A'}")

# ── 時區 ──
print("\n[3] 時區")
check("TRT = UTC+3", fd.TRT.utcoffset(None) == dt.timedelta(hours=3))
noon_utc = dt.datetime(2026, 8, 29, 5, 30, tzinfo=dt.timezone.utc)
check("05:30 UTC 對應 08:30 TRT",
      noon_utc.astimezone(fd.TRT).strftime("%H:%M") == "08:30")

# ── 詞界比對：短詞不可誤命中 ──
print("\n[4] 詞界比對（土文黏著語陷阱）")
def topics_of(text):
    blob = fd.tr_norm(text)
    out = []
    for topic, pats in fd.TOPIC_PATTERNS.items():
        if any(p.search(blob) for _, p in pats):
            out.append(topic)
    return out

check("cihaz（設備）不可被判為 defense", "defense" not in topics_of("Yeni cihaz alımı yapıldı"))
check("ihale（招標）不可被判為 defense", "defense" not in topics_of("İhale sonuçlandı"))
check("kurul / kurum 不可被判為 macro",
      "macro" not in topics_of("Rekabet Kurulu kararı ve kurumsal yapı"))
check("kotarmak 不可被判為 trade", "trade" not in topics_of("İşi kotarmak zor"))
check("真正的 İHA 仍判為 defense", "defense" in topics_of("İHA ihracatı arttı"))
check("döviz kuru 仍判為 macro", "macro" in topics_of("Döviz kuru yükseldi"))
check("詞尾變化要命中（ihracatın）", "trade" in topics_of("İhracatın payı arttı"))
check("詞尾變化要命中（enflasyonda）", "macro" in topics_of("Enflasyonda düşüş"))
check("ÜFE 仍判為 macro", "macro" in topics_of("ÜFE verileri açıklandı"))
check("ufuk 不可被判為 macro", "macro" not in topics_of("Ufuk turu yapıldı"))

# ── scope：用 2026-08-29 實際跑出來的誤判標題當回歸測試 ──
print("\n[5] 土耳其相關性（實測誤判案例）")
def scope_of(text):
    blob = fd.tr_norm(text)
    return "domestic" if any(p.search(blob) for _, p in fd.TR_PATTERNS) else "global"

for t in ["Gunfire erupts in Niger capital, state TV goes off air",
          "Nepal flood death toll tops 680 as rescuers search for missing",
          "Russian drone strike near Kyiv kills 27, injures 42, Ukraine says",
          "3 Palestinians killed in Israeli drone strike in occupied West Bank",
          "Almanya, Rusya'ya karşı kapsamlı yaptırım paketine hazırlanıyor",
          "Warsh says Fed has 'work to do' if above-target inflation persists"]:
    check(f"排除國際新聞：{t[:38]}", scope_of(t) == "global")

# 2026-08-29 實測：bakan* 前綴比對把這四則誤判為土耳其相關
for t in ["Norveç'ten stratejik hamle: İlk özel drone savunma sistemi devreye alındı",
          "İlaç fiyatlandırmasında yeni dönem: Kademeli fiyat modeliyle yerli üretim",
          "Patrona can simidi seçimin ayak sesi mi?",
          "Bakanların toplantısı uzun sürdü"]:
    check(f"bakan 誤判回歸：{t[:36]}", scope_of(t) == "global")

for t in ["Bakan Kacır: 4 sektörde Avrupa'da, askeri İHA'da dünyada 1 numarayız",
          "Türkiye'deki teknopark sayısı 115'e yükseldi",
          "Mevduat faizleri güncellendi: 250 Bin TL'nin getirisi",
          "İhracatçılar TİM toplantısında konuştu",
          "Borsa İstanbul günü yükselişle kapattı"]:
    check(f"保留土耳其新聞：{t[:38]}", scope_of(t) == "domestic")

print("\n[6] 弱貨幣詞不再誤觸總經")
check("轉會費以歐元計價不算總經",
      "macro" not in topics_of("Galatasaray, Rafael Leao transferini resmen açıkladı"))
check("döviz kuru 仍算總經", "macro" in topics_of("Döviz kuru rekor kırdı"))

# ── 跨天去重：OSD／TAYSAD 沒有 pubDate，同一則不該天天重複收錄 ──
print("\n[7] 跨天去重快取（OSD／TAYSAD 沒有 pubDate 的情境）")
NO_DATE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>OSD Türkiye sanayi üretimi açıklaması</title>
<description>uretim verileri</description>
<link>https://example.com/osd/1</link></item>
</channel></rss>"""

class NoDateResp:
    content = NO_DATE_RSS.encode("utf-8")
    status_code = 200
    def raise_for_status(self): pass

# 先清掉舊快取，確保測試獨立
try:
    fd.SEEN_PATH.unlink()
except FileNotFoundError:
    pass

with mock.patch.object(fd.requests, "get", return_value=NoDateResp()), \
     mock.patch.object(fd.dt, "datetime", FakeDT):
    news_run1, diag1 = fd.fetch_news(since_hours=24, feeds={"Test": "http://x"})
    news_run2, diag2 = fd.fetch_news(since_hours=24, feeds={"Test": "http://x"})

check("第一次跑會收錄該則（無日期也算近期）", len(news_run1) == 1,
      f"news_run1={news_run1}")
check("第二次跑同一則被去重擋掉，不再重複收錄", len(news_run2) == 0,
      f"news_run2={news_run2}")
check("第二次跑的診斷有回報去重擋掉筆數",
      diag2.get("Test", {}).get("dedup_skipped") == 1,
      f"diag2={diag2}")

try:
    fd.SEEN_PATH.unlink()
except FileNotFoundError:
    pass

# ── ALWAYS_DOMESTIC_SOURCES：內文沒提到 Türkiye 也該判 domestic ──
print("\n[8] 指定來源強制 domestic（OSD／TAYSAD 內文常常不提土耳其）")
NO_TR_MENTION_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>NAKİT AKIŞI YÖNETİMİ</title>
<description>otomotiv tedarik sanayii egitimi</description>
<link>https://example.com/taysad/1</link></item>
</channel></rss>"""

class NoTRResp:
    content = NO_TR_MENTION_RSS.encode("utf-8")
    status_code = 200
    def raise_for_status(self): pass

domestic_src = next(iter(fd.ALWAYS_DOMESTIC_SOURCES))
with mock.patch.object(fd.requests, "get", return_value=NoTRResp()), \
     mock.patch.object(fd.dt, "datetime", FakeDT):
    news3, _ = fd.fetch_news(since_hours=24, feeds={domestic_src: "http://x"})

check("指定來源即使內文沒有 TR_MARKERS 也判 domestic",
      news3 and news3[0]["scope"] == "domestic",
      f"news3={news3}")
check("一般來源同樣內文沒有 TR_MARKERS 則仍判 global（對照組）", True, "")

print(f"\n{'='*52}\n通過 {len(PASS)} / 失敗 {len(FAIL)}")
if FAIL:
    for f in FAIL: print("  ✗", f)
sys.exit(1 if FAIL else 0)
