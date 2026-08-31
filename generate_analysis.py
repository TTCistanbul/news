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
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"

# Ordered list of models to try. First entry is the "primary" -- the one
# we expect to actually be used day to day. The rest are fallbacks tried
# in order ONLY when Gemini returns 404 (model retired/not found), so a
# future Google-side retirement doesn't take the whole daily pipeline
# down the way gemini-2.5-flash's early retirement did on 2026-08-30.
# Update this list occasionally (see https://ai.google.dev/gemini-api/docs/models)
# -- it's a safety net, not a substitute for keeping the primary current.
CANDIDATE_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
]
TIMEOUT = 60


def _gemini_url(model: str) -> str:
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )


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
5. 全部輸出繁體中文（地名、機構名可保留原文，如 TCMB、TÜİK）。
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
      "source": "來源名稱",
      "source_url": "來源網址：這則新聞在輸入資料裡如果有標示「連結：」，就一定要把該網址原封不動填進來，不可以省略或留空；輸入裡真的沒有連結欄位才留空字串，絕對不要自己編造網址",
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


def _call_gemini_once(model: str, prompt: str, api_key: str) -> dict:
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
        },
    }
    r = requests.post(
        f"{_gemini_url(model)}?key={api_key}",
        json=body,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(
            f"Gemini 回應格式跟預期不同，看不到 candidates[0].content.parts[0].text：\n"
            f"{json.dumps(data, ensure_ascii=False)[:1000]}"
        ) from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"Gemini 回傳的內容不是合法 JSON，即使已經要求 responseMimeType="
            f"application/json：\n{text[:1000]}"
        ) from e


def call_gemini(prompt: str, api_key: str) -> tuple[dict, str]:
    """依序嘗試 CANDIDATE_MODELS。

    - 404（模型已下架/不存在）：直接換下一個候選模型。
    - 5xx（500-599，Google 那邊暫時性伺服器問題，例如 503 Service
      Unavailable）：2026-08-31 實測踩到——3.5-flash-lite 回傳 503 讓
      整支程式直接掛掉，沒有繼續試第三個候選模型。5xx 通常是暫時性的，
      這裡先原地重試一次（等 3 秒），還是失敗就換下一個候選模型，
      不要讓一次暫時性過載就搞掛整個每日流程。
    - 其他錯誤（金鑰無效、額度用完、請求格式錯誤、JSON 格式錯誤等）：
      這些換模型或重試都沒用，直接往上拋出，不要吞掉真正的問題。
    """
    last_error: Exception | None = None
    for i, model in enumerate(CANDIDATE_MODELS):
        is_last_candidate = i == len(CANDIDATE_MODELS) - 1
        for attempt in range(2):  # 同一個模型最多試 2 次（原始 + 1 次重試）
            try:
                result = _call_gemini_once(model, prompt, api_key)
                if i > 0 or attempt > 0:
                    print(f"   注意：改用/重試模型 {model} 成功產生內容")
                return result, model
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if e.response is not None else None
                last_error = e
                if status == 404:
                    print(f"   模型 {model} 回傳 404（可能已下架），改試下一個候選模型...")
                    break  # 換模型，不重試同一個
                if status is not None and 500 <= status < 600:
                    if attempt == 0:
                        print(f"   模型 {model} 回傳 {status}（可能是暫時性伺服器問題），"
                              f"3 秒後重試同一個模型...")
                        time.sleep(3)
                        continue  # 原地重試同一個模型一次
                    print(f"   模型 {model} 重試後仍是 {status}，改試下一個候選模型...")
                    break
                raise  # 其他狀態碼（401/403/429 等）不是換模型能解決的，直接拋出
        if is_last_candidate:
            raise last_error
    raise last_error  # pragma: no cover -- unreachable unless list is empty


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD，預設用 data/ 裡最新的檔案")
    args = ap.parse_args()

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("環境變數 GEMINI_API_KEY 沒有設定")

    payload, resolved_date = load_payload(args.date)
    user_content = build_user_content(payload)

    print(f"-> 呼叫 Gemini（依序嘗試：{', '.join(CANDIDATE_MODELS)}），"
          f"輸入新聞則數見上方 build_user_content 輸出")
    analysis, used_model = call_gemini(user_content, api_key)

    # 基本結構檢查，缺欄位就直接報錯，不要讓 render_report.py 拿到殘缺資料
    # 才在套版時炸掉，錯誤要在這一步就浮現。
    required = ["today_take", "summary", "key_events", "industry_items", "trade_implications"]
    missing = [k for k in required if k not in analysis]
    if missing:
        raise SystemExit(f"Gemini 回傳的 JSON 缺少欄位：{missing}\n完整內容：{analysis}")

    out_path = DATA_DIR / f"{resolved_date}-analysis.json"
    analysis["_generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    analysis["_model"] = used_model
    analysis["_source_date"] = resolved_date
    out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> 已寫入 {out_path}（使用模型：{used_model}）")
    print(f"   key_events={len(analysis.get('key_events', []))}  "
          f"industry_items={len(analysis.get('industry_items', []))}  "
          f"trade_implications={len(analysis.get('trade_implications', []))}")


if __name__ == "__main__":
    main()
