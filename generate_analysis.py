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
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-flash-latest",
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
6. 輸出必須是合法 JSON：字串值裡不可以出現未跳脫的雙引號。HTML 標籤的屬性一律
   用單引號（例如 <span class='data'>），中文引述請用「」，不要用半形雙引號。
7. 只處理輸入新聞裡有的內容，新聞不夠寫滿的欄位就回傳較短的陣列，不要
   為了湊數量而編造。

只輸出符合以下 JSON schema 的內容，不要有任何其他文字：

{
  "today_take": "今日判讀 HTML 片段（純文字＋<strong>標籤，2-4 句話）",
  "summary": "摘要 HTML 片段（純文字＋<span class='data'>數字</span>標記重要數字，一段完整段落）",
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


# ── 金額單位換算的自動檢查 ──
# 2026-09-05 發現的實際錯誤：土耳其媒體寫「23,47 milyar dolar」（＝234.7
# 億美元），模型輸出「23.47 億美元」，少了一個數量級。同一份輸出裡另一則
# 「63 億美元」卻換算正確，所以不是每次都錯，人工校對很難穩定抓到。
#
# 檢查方式：如果輸出裡某個「N 億」的 N，恰好等於原文裡某個「N milyar／
# N billion」的 N，那幾乎可以確定是照抄沒換算（正確結果應該是 N×10 億）。
# milyon／million 對「萬」也是同樣道理（正確結果是 N×100 萬）。
UNIT_PATTERNS = [
    # (原文單位, 中文單位, 正確倍數說明)
    (r"milyar|billion", "億", "milyar/billion 要乘以 10 才是「億」"),
    (r"milyon|million", "萬", "milyon/million 要乘以 100 才是「萬」"),
]


def _numbers_before_unit(text: str, unit_regex: str) -> set[float]:
    """抓出「數字 + 單位」裡的數字，土耳其式逗號小數點一併正規化。"""
    out = set()
    for m in re.finditer(rf"([\d][\d.,]*)\s*(?:{unit_regex})", text, re.IGNORECASE):
        raw = m.group(1).rstrip(".,")
        # 土耳其寫法：逗號＝小數點、點＝千分位
        if "," in raw:
            norm = raw.replace(".", "").replace(",", ".")
        else:
            # 只有點時無法百分之百分辨，1.850 視為千分位、23.47 視為小數
            norm = raw.replace(".", "") if re.fullmatch(r"\d{1,3}(\.\d{3})+", raw) else raw
        try:
            out.add(round(float(norm), 4))
        except ValueError:
            continue
    return out


def check_unit_conversion(source_text: str, analysis: dict) -> list[str]:
    """回傳可疑的單位換算清單，沒問題就回空 list。"""
    blob = json.dumps(analysis, ensure_ascii=False)
    problems = []
    for src_regex, zh_unit, hint in UNIT_PATTERNS:
        src_nums = _numbers_before_unit(source_text, src_regex)
        out_nums = _numbers_before_unit(blob, zh_unit)
        for n in sorted(src_nums & out_nums):
            problems.append(
                f"原文有「{n} {src_regex.split('|')[0]}」，輸出也寫成「{n} {zh_unit}」——{hint}"
            )
    return problems


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


def parse_model_json(text: str) -> dict:
    """解析 Gemini 回傳的 JSON。失敗時先嘗試修復常見問題再解析：
    模型會把 <span class="data"> 這種帶雙引號的 HTML 原封不動寫進 JSON 字串，
    雙引號沒跳脫就整份壞掉（2026-10-04 實際發生）。這裡把「HTML 標籤內部」的
    雙引號改成單引號——JSON 結構本身的引號不在標籤裡，不會被動到。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fixed = re.sub(r"<[^<>]*>", lambda m: m.group(0).replace('"', "'"), text)
    # 有時模型會包一層 ```json 圍欄
    fixed = re.sub(r"^```(?:json)?\s*|\s*```$", "", fixed.strip())
    return json.loads(fixed)


RETRY_STATUS = (500, 502, 503, 504)
BACKOFF = [5, 15, 30, 60]  # 503（伺服器忙碌）時的等待秒數，每個模型最多試 5 次


def call_gemini(prompt: str, api_key: str) -> tuple[dict, str]:
    import time
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
        for attempt in range(1, len(BACKOFF) + 2):
            try:
                r = requests.post(
                    f"{GEMINI_BASE}/{model}:generateContent",
                    params={"key": api_key},
                    json=body,
                    timeout=TIMEOUT,
                )
            except requests.RequestException as e:
                last_err = f"{model}: 連線錯誤 {e}"
                r = None
            if r is not None:
                if r.status_code in (404, 429):
                    # 404 = 模型下架/無權限，429 = 額度用完，直接換下一個模型
                    last_err = f"{model}: HTTP {r.status_code} {r.text[:200]}"
                    print(f"   {last_err}，改試下一個模型", file=sys.stderr)
                    break
                if r.status_code in RETRY_STATUS:
                    last_err = f"{model}: HTTP {r.status_code}（伺服器忙碌）"
                elif r.status_code != 200:
                    last_err = f"{model}: HTTP {r.status_code} {r.text[:200]}"
                    print(f"   {last_err}，改試下一個模型", file=sys.stderr)
                    break
                else:
                    data = r.json()
                    try:
                        text = data["candidates"][0]["content"]["parts"][0]["text"]
                        return parse_model_json(text), model
                    except (KeyError, IndexError):
                        last_err = f"{model}: 回應格式跟預期不同：{json.dumps(data, ensure_ascii=False)[:300]}"
                    except json.JSONDecodeError as e:
                        last_err = f"{model}: JSON 無法解析（{e}）"
            if attempt <= len(BACKOFF):
                wait = BACKOFF[attempt - 1]
                print(f"   {last_err}（第 {attempt} 次），{wait} 秒後重試", file=sys.stderr)
                time.sleep(wait)
            else:
                print(f"   {last_err}，這個模型放棄，換下一個", file=sys.stderr)
    raise SystemExit(f"所有候選模型都失敗，最後一個錯誤：{last_err}")


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

    # 金額單位換算檢查。發現疑似照抄沒換算時，帶著具體錯誤請模型重寫一次。
    # 重寫後仍有問題就照樣寫檔（總比整天開天窗好），但把警告記在 JSON 裡。
    unit_problems = check_unit_conversion(user_content, analysis)
    if unit_problems:
        print("!  偵測到疑似金額單位換算錯誤：")
        for p in unit_problems:
            print(f"   - {p}")
        print("   請模型重寫一次...")
        fix_prompt = (
            user_content
            + "\n\n以上是原始資料。你剛才產生的內容有金額單位換算錯誤：\n"
            + "\n".join(f"- {p}" for p in unit_problems)
            + "\n\n請重新產生完整 JSON，其他內容維持一樣的判斷，只把金額換算改正確。"
        )
        try:
            fixed, fixed_model = call_gemini(fix_prompt, api_key)
            missing = [k for k in required if k not in fixed]
            if missing:
                raise RuntimeError(f"重寫後的 JSON 缺少欄位：{missing}")
            analysis, used_model = fixed, fixed_model
            unit_problems = check_unit_conversion(user_content, analysis)
            print("   重寫後已無單位問題" if not unit_problems else "   重寫後仍有單位問題，保留內容但記錄警告")
        except Exception as e:
            print(f"!  重寫失敗，沿用原本內容：{e}")

    analysis = to_traditional(analysis)

    out_path = DATA_DIR / f"{resolved_date}-analysis.json"
    analysis["_generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    analysis["_model"] = used_model
    analysis["_source_date"] = resolved_date
    if unit_problems:
        analysis["_unit_warnings"] = unit_problems
    out_path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> 已寫入 {out_path}")
    print(f"   key_events={len(analysis.get('key_events', []))}  "
          f"industry_items={len(analysis.get('industry_items', []))}  "
          f"trade_implications={len(analysis.get('trade_implications', []))}")


if __name__ == "__main__":
    main()
