#!/usr/bin/env python3
"""
fetch_trade_release.py -- 自動抓「貿易部（T.C. Ticaret Bakanligi）月度對外貿易初步數字」，
寫進 data/turkey-trade-manual.json（原本要人工貼的檔案）。

流程：
1. 目標月份 = 台北／伊斯坦堡今天的「上個月」（例如 11/2 -> 2026-10）。
   檔案裡已經有該月就直接結束（不重複抓、不覆蓋人工校正過的數字）。
2. 從 ticaret.gov.tr/haberler 找出標題網址含目標月份、且含 dis-ticaret 或 ihracat 的新聞稿；
   找不到再試幾個常見網址。
3. 嚴格驗證「發布日期」落在目標月份的下個月 1~12 日、年份正確
   （2024 年的 Ekim 公告不會被誤收）。
4. 把新聞稿文字交給 Gemini 只做「抽取」（不准推論），再用程式檢查：
   涵蓋率 ≈ 出口/進口、年增率在合理範圍。任何一關沒過就放棄，不寫入。
5. 失敗一律安靜結束（exit 0），讓日報其他步驟照跑；下一天會再試。

新聞稿的金額是四捨五入的（例如「26 milyar dolar」），所以抓進來的 status 一律是
"preliminary"，source 欄位會註明。TÜİK 月底正式數字仍需之後人工校正。
"""
import datetime as dt
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import requests

ROOT = Path(__file__).parent
PATH = ROOT / "data" / "turkey-trade-manual.json"
BASE = "https://ticaret.gov.tr"
HDRS = {"User-Agent": "Mozilla/5.0 (compatible; ttc-brief-bot/1.0)"}
MONTHS_TR = ["Ocak", "Subat", "Mart", "Nisan", "Mayis", "Haziran",
             "Temmuz", "Agustos", "Eylul", "Ekim", "Kasim", "Aralik"]
GEMINI_MODELS = [m for m in [os.environ.get("GEMINI_MODEL"), "gemini-3.5-flash",
                             "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"] if m]


def ascii_fold(s: str) -> str:
    s = s.replace("ı", "i").replace("İ", "I")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()


def target_month(today: dt.date) -> tuple[int, int]:
    first = today.replace(day=1)
    prev = first - dt.timedelta(days=1)
    return prev.year, prev.month


def get(url: str) -> str | None:
    try:
        r = requests.get(url, headers=HDRS, timeout=30)
        return r.text if r.status_code == 200 else None
    except requests.RequestException as e:
        print(f"   連線失敗 {url}: {e}")
        return None


def html_to_text(h: str) -> str:
    h = re.sub(r"(?is)<(script|style|nav|footer|header).*?</\1>", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = re.sub(r"&nbsp;", " ", h)
    return re.sub(r"\s+", " ", h).strip()


def candidate_urls(month_name: str) -> list[str]:
    urls = []
    listing = get(f"{BASE}/haberler")
    if listing:
        for href in re.findall(r'href="(/haberler/[^"#?]+)"', listing):
            slug = ascii_fold(href)
            if month_name in slug and ("dis-ticaret" in slug or "ihracat" in slug):
                urls.append(BASE + href)
    for slug in (f"ticaret-bakani-omer-bolat-{month_name}-ayi-dis-ticaret-verilerini-acikladi",
                 f"ticaret-bakani-bolat-{month_name}-ayi-ihracat-verilerini-acikladi",
                 f"ticaret-bakani-omer-bolat-{month_name}-ayi-ihracat-verilerini-acikladi"):
        urls.append(f"{BASE}/haberler/{slug}")
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def pub_date_ok(text: str, year: int, month: int) -> bool:
    """發布日期必須是目標月份的下個月 1~12 日（例如 10 月數字 -> 11/1~11/12）。"""
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    names = ["Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz",
             "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık"]
    pat = rf"\b(0?[1-9]|1[0-2])\s+{names[nm-1]}\s+{ny}\b"
    return re.search(pat, text, re.I) is not None


EXTRACT_PROMPT = """下面是土耳其貿易部發布的月度對外貿易新聞稿文字。只做抽取，不要推論、不要自己計算。
只輸出 JSON：
{"exports_musd": 出口金額（百萬美元，整數；文中若寫「26 milyar dolar」就填 26000，寫「25,976」之類更精確的就照填）,
 "imports_musd": 進口金額（百萬美元，整數）,
 "coverage_pct": 出口對進口涵蓋率（karşılama oranı，數字，例 83.2）,
 "exports_yoy_pct": 出口年增率（%，下降為負數）,
 "imports_yoy_pct": 進口年增率（%，下降為負數）,
 "period_is_month": 新聞稿談的是單月（不是累計）數字 true/false}
文中找不到的欄位填 null。不要把 1-N 月累計數字當成單月。
"""


def gemini_extract(text: str) -> dict | None:
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        print("   沒有 GEMINI_API_KEY，略過")
        return None
    body = {"contents": [{"parts": [{"text": EXTRACT_PROMPT + "\n\n" + text[:12000]}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0}}
    import time
    for model in GEMINI_MODELS:
        for attempt in range(3):
            try:
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": key}, json=body, timeout=60)
            except requests.RequestException:
                time.sleep(5); continue
            if r.status_code in (404, 429):
                break
            if r.status_code != 200:
                time.sleep(10 * (attempt + 1)); continue
            try:
                return json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
            except Exception:
                time.sleep(3)
    return None


def validate(d: dict) -> str | None:
    """回傳 None 表示通過，否則回傳失敗原因。"""
    try:
        e, i, c = d["exports_musd"], d["imports_musd"], d["coverage_pct"]
        ey, iy = d["exports_yoy_pct"], d["imports_yoy_pct"]
    except KeyError as k:
        return f"缺欄位 {k}"
    if d.get("period_is_month") is not True:
        return "抽到的不是單月數字"
    if None in (e, i, c, ey, iy):
        return "有欄位是 null"
    if not (10000 <= e <= 50000 and 10000 <= i <= 60000):
        return f"金額不合理 exports={e} imports={i}"
    if abs(e / i * 100 - c) > 1.0:
        return f"涵蓋率 {c} 跟出口/進口 {e/i*100:.1f} 對不上"
    if not (-40 <= ey <= 60 and -40 <= iy <= 60):
        return f"年增率不合理 {ey} {iy}"
    return None


def main():
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=3))).date()
    year, month = target_month(today)
    key = f"{year}-{month:02d}"
    data = json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else {
        "currency": "USD", "unit": "million",
        "source_note": "T.C. Ticaret Bakanlığı ／ TÜİK 官方月度對外貿易統計新聞稿", "months": []}
    if any(m.get("month") == key for m in data["months"]):
        print(f"-> {key} 已存在，略過")
        return
    if today.day > 12:
        print(f"-> {key} 還沒有數字但已超過公告期（{today}），改人工處理")
        return

    month_name = MONTHS_TR[month - 1].lower()
    print(f"-> 找 {key} 貿易部新聞稿（{month_name}）")
    for url in candidate_urls(month_name):
        page = get(url)
        if not page:
            continue
        text = html_to_text(page)
        if not pub_date_ok(text, year, month):
            print(f"   {url} 發布日期不符（可能是舊年份文章），略過")
            continue
        d = gemini_extract(text)
        if not d:
            print(f"   {url} 抽取失敗"); continue
        why = validate(d)
        if why:
            print(f"   {url} 驗證未過：{why}；{d}"); continue
        data["months"].append({
            "month": key,
            "exports": d["exports_musd"], "imports": d["imports_musd"],
            "coverage_pct": d["coverage_pct"],
            "exports_yoy_pct": d["exports_yoy_pct"], "imports_yoy_pct": d["imports_yoy_pct"],
            "balance_yoy_pct": None,
            "status": "preliminary",
            "source": f"貿易部新聞稿（{today.isoformat()} 自動抓取；金額為新聞稿四捨五入值，年增率與涵蓋率為官方公布）；{url}",
        })
        data["months"].sort(key=lambda m: m["month"])
        PATH.parent.mkdir(exist_ok=True)
        PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"-> 已寫入 {key}：出口 {d['exports_musd']} 進口 {d['imports_musd']} 百萬美元")
        return
    print("-> 還沒找到可用的新聞稿，明天再試")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # 任何意外都不能讓整條日報流程掛掉
        print(f"! fetch_trade_release 例外（已忽略）：{e}", file=sys.stderr)
