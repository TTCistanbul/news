#!/usr/bin/env python3
"""
generate_period_report.py -- 一鍵產出週／月／季／年報告

用法：
    python3 generate_period_report.py --period week
    python3 generate_period_report.py --period month
    python3 generate_period_report.py --period quarter
    python3 generate_period_report.py --period year
    python3 generate_period_report.py --period week --end 2026-08-24

設計原則：
- 這是獨立的一支腳本，不會改動 fetch_daily.py / generate_analysis.py /
  render_report.py 既有的每日流程，避免互相拖累除錯。
- 週期用「往前捲動 N 天」計算（week=7、month=30、quarter=91、year=365），
  不是自然月/季/年的日曆邊界——原因跟 render_report.py 的「產業動態近 7
  天」篩選一樣：日曆邊界要處理跨月/跨年等一堆邊界情況，滾動視窗更穩定
  好懂，對台商實際判讀來說差異也不大。
- 結束日預設用 data/ 裡「實際已經抓到的最新一天」，不是系統當下的
  wall-clock 日期——因為 workflow 觸發當下，今天的每日資料可能還沒跑，
  用「今天」當結束日反而會漏掉最新一天。
- 匯率/總經的高低點、起訖值一律用 Python 自己算好、當作既定事實餵給
  Gemini，不假手 AI 從一堆每日文字裡自己去抓數字——跟
  generate_analysis.py 既有的「不讓 AI 自己編數字」原則一致。
- 每日的 key_events / industry_items 跨很多天會有大量重複/雷同的舊聞，
  交給 Gemini 做「去重＋濃縮成趨勢敘述」，不是逐日條列。
"""

import argparse
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
TEMPLATE_PATH = ROOT / "templates" / "period-report-template.html"
OUT_DIR = ROOT / "docs" / "reports"

TRT = dt.timezone(dt.timedelta(hours=3))

PERIOD_DAYS = {"week": 7, "month": 30, "quarter": 91, "year": 365}
PERIOD_LABEL_ZH = {"week": "週報", "month": "月報", "quarter": "季報", "year": "年報"}

CANDIDATE_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-3.5-flash-lite",
    "gemini-3.5-flash",
]
TIMEOUT = 60


def _gemini_url(model: str) -> str:
    return f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


SYSTEM_PROMPT = """\
你是台灣外貿協會（TAITRA）駐伊斯坦堡辦事處的產業分析師，負責把過去一段
期間（週/月/季/年）累積的 Türkiye 經濟與產業新聞，濃縮寫成一份給台灣廠商
看的期間趨勢報告。這不是每日簡報，重點是「這段期間的變化與趨勢」，不是
逐日條列流水帳。

嚴格規則：
1. 訊息裡會直接給你這段期間的匯率、總經指標的「起始值／期末值／期間高低點」，
   這些數字你必須原樣引用，不要自己從新聞文字裡重新推算或估計。
2. 同一件事在不同日期反覆出現時（例如某政策連續多天被報導），合併成一句
   趨勢敘述，不要逐日重複列出。
3. 只根據提供的輸入內容撰寫，不要用你自己既有的知識補充輸入裡沒有的具體
   數字、日期、政策狀態。
4. 區分「新聞陳述的事實」跟「你對台商的解讀／推論」，解讀要用「可能」
   「值得觀察」等保留語氣。
5. 全部輸出繁體中文（地名、機構名可保留原文，如 TCMB、TÜİK）。
6. 內容不夠寫滿的欄位就回傳較短的陣列，不要為了湊數量而編造。
7. 金額單位換算務必正確，這是最常出錯的地方：
   - 土耳其文 milyar ＝ 英文 billion ＝ 中文「十億」＝「10 億」。
     所以 23,47 milyar dolar 要寫成「234.7 億美元」，不是「23.47 億美元」。
   - 土耳其文 milyon ＝ 英文 million ＝ 中文「百萬」＝「100 萬」。
     所以 6,3 milyon dolar 要寫成「630 萬美元」，不是「6.3 萬美元」。
   - 土耳其文用逗號當小數點（23,47 就是 23.47），用點當千分位
     （1.850 就是 1850）。看到逗號不要當成千分位。
   換算前先把原文數字唸出來確認量級：一個國家的單月出口通常是幾百億美元，
   央行外匯儲備通常是一兩千億美元。算出來的數字如果比常識小十倍或大十倍，
   就是換算錯了，重算一次再寫。

只輸出符合以下 JSON schema 的內容，不要有任何其他文字：

{
  "period_take": "本期總結 HTML 片段（純文字＋<strong>標籤，3-5 句話，
    點出這段期間最重要的變化方向）",
  "period_summary": "摘要 HTML 片段（純文字＋<span class=\\"data\\">數字</span>
    標記重要數字，2-3 段完整段落，涵蓋匯率、總經、貿易面的期間變化）",
  "key_developments": [
    {
      "direction": "red|green|neutral",
      "importance": "重大|中等",
      "headline": "這段期間的重大發展（一行，是趨勢/事件不是單日新聞標題）",
      "summary": "重點說明（2-3句，可以整合多則相關報導）",
      "business_impact": "對台商與貿易影響（1-2句，用保留語氣）"
    }
  ],
  "industry_trends": [
    {
      "sector": "產業別",
      "sentiment": "pos|neg|neu",
      "headline": "這段期間該產業的趨勢標題",
      "body": "內容 2-3 句，整合期間內相關報導",
      "business_interpretation": "台商解讀（1-2句，用保留語氣）"
    }
  ],
  "trade_implications": [
    {
      "title": "一句話標題",
      "body": "分析內容（2-4句，用保留語氣，可引用給定的期間數字）"
    }
  ]
}

key_developments 最多 8 則，依重要性排序。industry_trends 最多 6 則。
trade_implications 最多 4 則。
"""


# 金額單位換算檢查沿用 generate_analysis.py 的實作，規則只維護一份。
# 那支檔案不在或改名時不讓週報整支掛掉，只是少了這道檢查。
try:
    from generate_analysis import check_unit_conversion
except Exception as _e:  # pragma: no cover
    print(f"!  無法載入 generate_analysis.check_unit_conversion，略過單位檢查：{_e}",
          file=sys.stderr)
    check_unit_conversion = None


def load_daily_files(start: dt.date, end: dt.date) -> list[dict]:
    """讀取 [start, end]（含頭尾）區間內所有存在的 data/YYYY-MM-DD.json，
    連同同一天的 -analysis.json 一起打包回傳，依日期由舊到新排序。"""
    out = []
    d = start
    while d <= end:
        raw_path = DATA_DIR / f"{d.isoformat()}.json"
        if raw_path.exists():
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"! 略過壞掉的 {raw_path.name}: {e}", file=sys.stderr)
                d += dt.timedelta(days=1)
                continue
            analysis_path = DATA_DIR / f"{d.isoformat()}-analysis.json"
            analysis = None
            if analysis_path.exists():
                try:
                    analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
                except Exception as e:
                    print(f"! 略過壞掉的 {analysis_path.name}: {e}", file=sys.stderr)
            out.append({"date": d.isoformat(), "raw": raw, "analysis": analysis})
        d += dt.timedelta(days=1)
    return out


def resolve_end_date(explicit: str | None) -> dt.date:
    if explicit:
        p = DATA_DIR / f"{explicit}.json"
        if not p.exists():
            raise SystemExit(f"指定的結束日 {explicit} 沒有對應的 {p} 檔案")
        return dt.date.fromisoformat(explicit)
    files = sorted(DATA_DIR.glob("*.json"))
    files = [f for f in files if re.match(r"\d{4}-\d{2}-\d{2}\.json$", f.name)]
    if not files:
        raise SystemExit("data/ 裡完全沒有找到任何每日資料檔案，無法產生期間報告")
    return dt.date.fromisoformat(files[-1].stem)


def fx_trend(days: list[dict], code: str) -> dict | None:
    """從一串每日原始資料裡，抓出某貨幣的 per_unit_selling 走勢，
    算出起始值、期末值、期間內最高最低（含日期）。缺值的日子跳過。"""
    points = []
    for d in days:
        rates = ((d["raw"].get("fx") or {}).get("rates") or {})
        v = (rates.get(code) or {}).get("per_unit_selling")
        if v is not None:
            points.append((d["date"], v))
    if not points:
        return None
    start_date, start_val = points[0]
    end_date, end_val = points[-1]
    hi_date, hi_val = max(points, key=lambda p: p[1])
    lo_date, lo_val = min(points, key=lambda p: p[1])
    change_pct = ((end_val - start_val) / start_val * 100) if start_val else None
    return {
        "start": {"date": start_date, "value": start_val},
        "end": {"date": end_date, "value": end_val},
        "high": {"date": hi_date, "value": hi_val},
        "low": {"date": lo_date, "value": lo_val},
        "change_pct": change_pct,
        "n_points": len(points),
    }


def latest_macro(days: list[dict], series_name: str) -> list | None:
    """macro 序列（cpi / funding_cost）本身就是時間序列，不用逐日相加，
    直接拿期間內「最後一次成功抓到該序列」那天的資料即可，它已經涵蓋
    往前一段時間的歷史點。"""
    for d in reversed(days):
        series = (d["raw"].get("macro") or {}).get(series_name)
        if series:
            return series
    return None


def build_gemini_input(period: str, start: dt.date, end: dt.date,
                        days: list[dict]) -> str:
    usd = fx_trend(days, "USD")
    eur = fx_trend(days, "EUR")
    cpi = latest_macro(days, "cpi")
    funding = latest_macro(days, "funding_cost")

    lines = [
        f"報告週期：{PERIOD_LABEL_ZH[period]}（{start.isoformat()} ～ {end.isoformat()}，"
        f"共 {(end - start).days + 1} 天，實際有資料的天數：{len(days)}）",
        "",
    ]

    if usd:
        lines.append(
            f"USD/TRY：期初 {usd['start']['value']:.4f}（{usd['start']['date']}）→ "
            f"期末 {usd['end']['value']:.4f}（{usd['end']['date']}），"
            f"期間最高 {usd['high']['value']:.4f}（{usd['high']['date']}）、"
            f"最低 {usd['low']['value']:.4f}（{usd['low']['date']}）"
            + (f"，變動 {usd['change_pct']:+.2f}%" if usd["change_pct"] is not None else "")
        )
    if eur:
        lines.append(
            f"EUR/TRY：期初 {eur['start']['value']:.4f}（{eur['start']['date']}）→ "
            f"期末 {eur['end']['value']:.4f}（{eur['end']['date']}），"
            f"期間最高 {eur['high']['value']:.4f}（{eur['high']['date']}）、"
            f"最低 {eur['low']['value']:.4f}（{eur['low']['date']}）"
            + (f"，變動 {eur['change_pct']:+.2f}%" if eur["change_pct"] is not None else "")
        )
    if cpi:
        lines.append(f"CPI 序列（最近抓到的資料，可能涵蓋比報告週期更長的時間）："
                      + json.dumps(cpi[-8:], ensure_ascii=False))
    if funding:
        lines.append(f"隔夜融資成本序列（同上）：" + json.dumps(funding[-8:], ensure_ascii=False))

    lines.append("")
    lines.append("── 期間內各日分析摘要（AI 每日產出的內容，供你整合，不要逐日重複列出）──")
    for d in days:
        a = d.get("analysis")
        if not a:
            continue
        lines.append(f"\n[{d['date']}]")
        if a.get("today_take"):
            lines.append(f"今日判讀：{re.sub('<[^>]+>', '', a['today_take'])[:300]}")
        for ev in (a.get("key_events") or [])[:3]:
            lines.append(f"  事件：{ev.get('headline','')}｜{ev.get('summary','')[:150]}")
        for it in (a.get("industry_items") or [])[:3]:
            lines.append(f"  產業：[{it.get('sector','')}] {it.get('headline','')}｜{it.get('body','')[:150]}")

    return "\n".join(lines)


def _call_gemini_once(model: str, prompt: str, api_key: str) -> dict:
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
    }
    r = requests.post(f"{_gemini_url(model)}?key={api_key}", json=body, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(
            f"Gemini 回應格式跟預期不同：{json.dumps(data, ensure_ascii=False)[:1000]}"
        ) from e
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gemini 回傳的內容不是合法 JSON：{text[:1000]}") from e


def call_gemini(prompt: str, api_key: str) -> tuple[dict, str]:
    last_error = None
    for i, model in enumerate(CANDIDATE_MODELS):
        try:
            result = _call_gemini_once(model, prompt, api_key)
            if i > 0:
                print(f"   注意：改用備援模型 {model} 成功", file=sys.stderr)
            return result, model
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            if status == 404 and i < len(CANDIDATE_MODELS) - 1:
                print(f"   模型 {model} 404，改試下一個候選模型...", file=sys.stderr)
                last_error = e
                continue
            raise
    raise last_error


def render_key_developments(items: list[dict]) -> str:
    icon = {"red": "🔴", "green": "🟢", "neutral": "⚪"}
    cls = {"red": "dir-red", "green": "dir-green", "neutral": "dir-neutral"}
    rows = []
    for i, e in enumerate(items[:8], 1):
        d = e.get("direction", "neutral")
        importance = html.escape(e.get("importance", "中等"))
        badge_cls = "badge-major" if importance == "重大" else "badge-medium"
        headline = html.escape(e.get("headline", ""))
        summary = html.escape(e.get("summary", ""))
        impact = html.escape(e.get("business_impact", ""))
        rows.append(f'''          <tr>
            <td>{i}</td>
            <td class="{cls.get(d,'dir-neutral')}">{icon.get(d,'⚪')}</td>
            <td><span class="badge {badge_cls}">{importance}</span></td>
            <td>{headline}</td>
            <td>{summary}</td>
            <td>{impact}</td>
          </tr>''')
    return "\n".join(rows) if rows else '          <tr><td colspan="6">本期無資料</td></tr>'


def render_industry_trends(items: list[dict]) -> str:
    cls = {"pos": "pos", "neg": "neg", "neu": "neu"}
    lis = []
    for it in items[:6]:
        sentiment = cls.get(it.get("sentiment", "neu"), "neu")
        sector = html.escape(it.get("sector", ""))
        headline = html.escape(it.get("headline", ""))
        body = html.escape(it.get("body", ""))
        interp = html.escape(it.get("business_interpretation", ""))
        lis.append(f'''      <li class="sector-item {sentiment}">
        <div class="row-top">
          <span class="sector-chip">{sector}</span>
          <span class="headline">{headline}</span>
        </div>
        <div class="body">{body}</div>
        <div class="impact"><b>台商解讀：</b>{interp}</div>
      </li>''')
    return "\n".join(lis) if lis else "      <li>本期無資料</li>"


def render_trade_implications(items: list[dict]) -> str:
    lis = []
    for it in items[:4]:
        title = html.escape(it.get("title", ""))
        body = html.escape(it.get("body", ""))
        lis.append(f"      <li><strong>{title}</strong>{body}</li>")
    return "\n".join(lis) if lis else "      <li>本期無資料</li>"


def replace_block(html_text: str, marker: str, new_inner: str) -> str:
    pattern = re.compile(
        rf"(<!-- AI:{marker}:START.*?-->)(.*?)(<!-- AI:{marker}:END.*?-->)", re.DOTALL
    )
    if not pattern.search(html_text):
        return html_text
    return pattern.sub(lambda m: m.group(1) + "\n" + new_inner + "\n" + m.group(3), html_text)


def fmt_fx_cell(trend: dict | None) -> str:
    if not trend:
        return "資料不足"
    arrow = "↑" if (trend["change_pct"] or 0) > 0 else ("↓" if (trend["change_pct"] or 0) < 0 else "→")
    pct = f"{trend['change_pct']:+.2f}%" if trend["change_pct"] is not None else "—"
    return (f"{trend['start']['value']:.4f} {arrow} {trend['end']['value']:.4f}"
            f"（{pct}，區間 {trend['low']['value']:.4f}–{trend['high']['value']:.4f}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", required=True, choices=list(PERIOD_DAYS.keys()))
    ap.add_argument("--end", help="YYYY-MM-DD，預設用 data/ 裡最新一天")
    args = ap.parse_args()

    end = resolve_end_date(args.end)
    start = end - dt.timedelta(days=PERIOD_DAYS[args.period] - 1)
    days = load_daily_files(start, end)
    if not days:
        raise SystemExit(f"{start} ~ {end} 這段期間內 data/ 完全沒有資料，無法產生報告")
    print(f"-> 期間 {start} ~ {end}，實際找到 {len(days)} 天的資料", file=sys.stderr)

    api_key = __import__("os").environ.get("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("環境變數 GEMINI_API_KEY 沒有設定")

    prompt = build_gemini_input(args.period, start, end, days)
    print("-> 呼叫 Gemini 產生期間分析...", file=sys.stderr)
    analysis, used_model = call_gemini(prompt, api_key)
    print(f"-> Gemini 完成（{used_model}）", file=sys.stderr)

    # 金額單位換算檢查（milyar/billion 對「億」、milyon/million 對「萬」）。
    # 抓到疑似照抄沒換算時，帶著具體錯誤請模型重寫一次；重寫後仍有問題就
    # 照樣輸出報告，但把警告印在 log 裡。
    unit_problems = []
    if check_unit_conversion is not None:
        unit_problems = check_unit_conversion(prompt, analysis)
        if unit_problems:
            print("!  偵測到疑似金額單位換算錯誤：", file=sys.stderr)
            for p in unit_problems:
                print(f"   - {p}", file=sys.stderr)
            print("   請模型重寫一次...", file=sys.stderr)
            fix_prompt = (
                prompt
                + "\n\n以上是原始資料。你剛才產生的內容有金額單位換算錯誤：\n"
                + "\n".join(f"- {p}" for p in unit_problems)
                + "\n\n請重新產生完整 JSON，其他內容維持一樣的判斷，只把金額換算改正確。"
            )
            try:
                analysis, used_model = call_gemini(fix_prompt, api_key)
                unit_problems = check_unit_conversion(prompt, analysis)
                print("   重寫後已無單位問題" if not unit_problems
                      else "   重寫後仍有單位問題，保留內容但請人工覆核", file=sys.stderr)
            except Exception as e:
                print(f"!  重寫失敗，沿用原本內容：{e}", file=sys.stderr)

    if not TEMPLATE_PATH.exists():
        raise SystemExit(
            f"找不到範本檔案 {TEMPLATE_PATH}，請先建立 "
            f"templates/period-report-template.html"
        )
    html_text = TEMPLATE_PATH.read_text(encoding="utf-8")

    usd = fx_trend(days, "USD")
    eur = fx_trend(days, "EUR")

    period_label = PERIOD_LABEL_ZH[args.period]
    replacements = {
        "{{PERIOD_LABEL}}": period_label,
        "{{PERIOD_START_ZH}}": f"{start.year}年{start.month}月{start.day}日",
        "{{PERIOD_END_ZH}}": f"{end.year}年{end.month}月{end.day}日",
        "{{PERIOD_DAYS_COUNT}}": str(len(days)),
        "{{USD_TREND}}": fmt_fx_cell(usd),
        "{{EUR_TREND}}": fmt_fx_cell(eur),
        "{{GENERATED_AT_LABEL}}": dt.datetime.now(TRT).strftime("%Y-%m-%d %H:%M TRT"),
    }
    for token, value in replacements.items():
        html_text = html_text.replace(token, str(value))

    if analysis.get("period_take"):
        html_text = replace_block(html_text, "PERIOD_TAKE",
                                   f'  <p class="assessment-text">\n    {analysis["period_take"]}\n  </p>')
    if analysis.get("period_summary"):
        html_text = replace_block(html_text, "PERIOD_SUMMARY",
                                   f'    <p class="summary-text">\n {analysis["period_summary"]}\n    </p>')
    html_text = replace_block(html_text, "KEY_DEVELOPMENTS",
                               render_key_developments(analysis.get("key_developments", [])))
    html_text = replace_block(html_text, "INDUSTRY_TRENDS",
                               render_industry_trends(analysis.get("industry_trends", [])))
    html_text = replace_block(html_text, "TRADE_IMPLICATIONS",
                               render_trade_implications(analysis.get("trade_implications", [])))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_name = f"{args.period}-{end.isoformat()}.html"
    out_path = OUT_DIR / out_name
    out_path.write_text(html_text, encoding="utf-8")

    # 存一份小索引，讓 docs/reports/ 有紀錄可以之後串成列表頁（目前先只
    # 是純資料，還沒有對應的網頁 UI 去讀它）
    index_path = OUT_DIR / "_index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        index = []
    index = [e for e in index if e.get("file") != out_name]
    index.append({
        "file": out_name, "period": args.period,
        "start": start.isoformat(), "end": end.isoformat(),
        "generated_at": dt.datetime.now(TRT).isoformat(),
    })
    index.sort(key=lambda e: e["end"])
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"-> 已輸出 {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
