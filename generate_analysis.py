#!/usr/bin/env python3
"""
generate_analysis.py -- calls Gemini to write the sections that need
judgment: today's take, executive summary, key-events table, industry
watch cards, and trade implications.

This is the FULLY AUTOMATIC branch: output goes straight to
data/YYYY-MM-DD-analysis.json and render_report.py injects it into the
HTML with no human review step. That was an explicit choice -- know
that it means occasional stale or overreaching analysis can go live
unreviewed (this is exactly the kind of error a human reviewer caught
in a manual pass on 2026-08-29: the site said one-week repo auctions
were still suspended three days after TCMB had already resumed them).
The ai-disclaimer banner in the HTML template exists to warn readers
of this trade-off; removing it without re-adding a review step would
misrepresent the page to readers.

Usage:
    python3 generate_analysis.py                 # today's data/*.json
    python3 generate_analysis.py --date 2026-08-29
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"

# 2026-10-04 實測：gemini-2.5-flash 對新金鑰回 404（Google 已把它列為
# deprecated，僅限舊專案使用）。官方建議替代為 gemini-3.5-flash。
# Google 換模型很快，所以這裡放候選清單，前一個 404/429 就自動換下一個；
# 也可以用環境變數 GEMINI_MODEL 指定第一優先的模型。
GEMINI_MODELS = [
    m for m in [
        os.environ.get("GEMINI_MODEL"),
        "gemini-3.5-flash",
        "gemini-3.1-flash-lite",
        "gemini-2.5-flash-lite",
    ] if m
]
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = 60

SYSTEM_PROMPT = """\
你是台灣外貿協會（TAITRA）駐伊斯坦堡辦事處的產業分析師，負責把當天篩選出的
土耳其（Türkiye）經濟與產業新聞，寫成給台灣廠商看的每日經濟簡報。

嚴格規則：
1. 只根據下面提供的新聞內容（標題、摘要、來源、發布日期）撰寫，不要使用你
   自己既有的知識去補充或推測新聞裡沒寫的具體數字、日期、政策狀態。
2. 每一則新聞、每一個數字，都要能追溯到輸入資料裡的某一條，不要合併/延伸
   出輸入裡沒有的新事實。
3. 區分「新聞陳述的事實」跟「你對台商的解讀／推論」，解讀要用「可能」
   「值得觀察」等保留語氣，不要把單一數據點講成已證實的因果結論
   （例如：進口結構變化不能直接斷言為「企業正在補庫存」）。
4. 如果同一主題的新聞語氣互相衝突，或某個政策/數字的時效性不確定，寧可
   保守、明確寫出不確定性，也不要選一個聽起來比較篤定的說法。
5. 全部輸出「台灣用語的繁體中文」，絕對不可以出現任何簡體字或中國大陸用語
   （例如寫「軟體」不寫「软件」、「資料」不寫「数据」、「品質」不寫「质量」）。
   地名、機構名可保留原文，如 TCMB、TÜİK。
6. 只處理輸入新聞裡有的內容，新聞不夠寫滿的欄位就回傳較短的陣列，不要
   為了湊數量而編造。

只輸出符合以下 JSON schema 的內容，不要有任何其他文字：

{
  "today_take": "今日判讀 HTML 片段（純文字＋<strong>標籤，2-4 句話）",
  "summary": "摘要 HTML 片段（純文字＋<span class=\\"data\\">數字</span>標記重要數字，一段完整段落）",
  "key_events": [
    {
      "direction": "red|green|neutral",
      "importance": "重大|中等",
      "source_name": "來源名稱",
      "source_url": "來源網址（用輸入資料裡的 url，沒有就留空字串）",
      "headline": "事件標題（一行）",
      "summary": "重點摘要（1-2句）",
      "business_impact": "對台商與貿易影響（1-2句，用保留語氣）"
    }
  ],
  "industry_items": [
    {
      "sector": "產業別（例如：紡織成衣、汽車、機械）",
      "sentiment": "pos|neg|neu",
      "headline": "標題",
      "source": "來源",
      "date": "YYYY-MM-DD（新聞的發布/報導日期，不是新聞裡提到的生效日或事件日）",
      "body": "內容 1-2 句",
      "business_interpretation": "台商解讀（1-2句，用保留語氣）"
    }
  ],
  "trade_implications": [
    {
      "title": "一句話標題",
      "body": "分析內容（2-3句，用保留語氣）"
    }
  ]
}

key_events 最多 5 則，依重要性排序。industry_items 最多 5 則。
trade_implications 最多 3 則，只從 key_events／industry_items 已經寫過的
內容做整合式結論，不要引入新事實。
"""


def load_payload(date_str: str | None) -> tuple[dict, str]:
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
    resolved_date = path.stem
    return json.loads(path.read_text(encoding="utf-8")), resolved_date


def build_user_content(payload: dict) -> str:
    news = payload.get("news", [])
    domestic = [n for n in news if n.get("scope") == "domestic"]
    # 沒有 domestic 新聞時退而求其次用全部新聞，讓當天至少有東西可寫，
    # 而不是整段 AI 區塊開天窗。
    items = domestic or news

    fx = payload.get("fx", {})
    macro = payload.get("macro", {})

    lines = [f"報告日期：{payload.get('date', '')}", ""]
    if fx:
        lines.append(f"匯率快照：{json.dumps(fx, ensure_ascii=False)}")
    if macro:
        lines.append(f"總經數據（EVDS，只取最近幾筆）：")
        for name, series in macro.items():
            tail = series[-3:] if isinstance(series, list) else series
            lines.append(f"  {name}: {json.dumps(tail, ensure_ascii=False)}")
    lines.append("")
    lines.append(f"今日篩選出的新聞（共 {len(items)} 則，scope={'domestic' if domestic else 'all'}）：")
    for i, n in enumerate(items, 1):
        lines.append(
            f"{i}. [{n.get('primary_topic', '')}] {n.get('source', '')} "
            f"({n.get('published') or '無日期'})"
        )
        lines.append(f"   標題：{n.get('title', '')}")
        if n.get("summary"):
            lines.append(f"   摘要：{n['summary'][:300]}")
        if n.get("url"):
            lines.append(f"   連結：{n['url']}")
    return "\n".join(lines)


def to_traditional(obj):
    """Gemini 偶爾會混出簡體字，prompt 擋不住 100%。這裡用 OpenCC s2twp
    （簡體 -> 台灣繁體，含台灣慣用詞）對所有文字欄位做一次轉換，
    網址欄位（key 以 url 結尾）跳過。已經是繁體的文字不會被改壞。"""
    try:
        from opencc import OpenCC
    except ImportError:
        raise SystemExit("缺少 opencc 套件：pip install opencc-python-reimplemented")
    cc = OpenCC("s2twp")

    def walk(x, key=""):
        if isinstance(x, str):
            return x if key.endswith("url") else cc.convert(x)
        if isinstance(x, list):
            return [walk(i, key) for i in x]
        if isinstance(x, dict):
            return {k: walk(v, k) for k, v in x.items()}
        return x

    return walk(obj)


def call_gemini(prompt: str, api_key: str) -> tuple[dict, str]:
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }
    last_err = None
    for model in GEMINI_MODELS:
        r = requests.post(
            f"{GEMINI_BASE}/{model}:generateContent",
            params={"key": api_key},
            json=body,
            timeout=TIMEOUT,
        )
        if r.status_code in (404, 429):
            # 404 = 模型不存在/已下架，429 = 該模型免費額度用完，換下一個
            last_err = f"{model}: HTTP {r.status_code} {r.text[:200]}"
            print(f"   {last_err}，改試下一個模型", file=sys.stderr)
            continue
        r.raise_for_status()
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise RuntimeError(
                f"Gemini 回應格式跟預期不同（model={model}）：\n"
                f"{json.dumps(data, ensure_ascii=False)[:1000]}"
            ) from e
        try:
            return json.loads(text), model
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Gemini 回傳的內容不是合法 JSON（model={model}）：\n{text[:1000]}"
            ) from e
    raise SystemExit(f"所有候選模型都不可用，最後一個錯誤：{last_err}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，預設用 data/ 裡最新的檔案")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("環境變數 GEMINI_API_KEY 沒有設定")

    payload, resolved_date = load_payload(args.date)
    user_content = build_user_content(payload)

    print(f"-> 呼叫 Gemini，候選模型：{GEMINI_MODELS}")
    analysis, used_model = call_gemini(user_content, api_key)
    print(f"   實際使用模型：{used_model}")

    # 基本結構檢查，缺欄位就直接報錯，不要讓 render_report.py 拿到殘缺資料
    # 才在套版時炸掉，錯誤要在這一步就浮現。
    required = ["today_take", "summary", "key_events", "industry_items", "trade_implications"]
    missing = [k for k in required if k not in analysis]
    if missing:
        raise SystemExit(f"Gemini 回傳的 JSON 缺少欄位：{missing}\n完整內容：{analysis}")

    analysis = to_traditional(analysis)

    out_path = DATA_DIR / f"{resolved_date}-analysis.json"
    analysis["_generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    analysis["_model"] = used_model
    analysis["_source_date"] = resolved_date
    out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> 已寫入 {out_path}")
    print(f"   key_events={len(analysis.get('key_events', []))}  "
          f"industry_items={len(analysis.get('industry_items', []))}  "
          f"trade_implications={len(analysis.get('trade_implications', []))}")


if __name__ == "__main__":
    main()
