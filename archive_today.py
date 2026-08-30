#!/usr/bin/env python3
"""
archive_today.py -- snapshot the CURRENTLY LIVE docs/index.html into
docs/archive/ before you start editing it for the next day's report.

WHY THIS EXISTS:
render_report.py only fills in 4 numeric tokens (FX rates, funding
cost, timestamp) -- everything else in docs/index.html (摘要, 關鍵事件,
產業動態, 貿易意涵, and the ARCHIVE_ITEMS list) is written by hand.
That means every day, whoever writes the report opens docs/index.html
and overwrites yesterday's content with today's. Nothing in the repo
ever saved a copy of yesterday's page before that happened -- so every
entry in ARCHIVE_ITEMS pointed at a docs/archive/*.html file that was
never created, and every "歷史簡報" link 404'd.

WHEN TO RUN THIS:
Run it FIRST, every day, BEFORE you start editing docs/index.html for
the new report. It reads the date already baked into the currently
live page (the DATE chip in the hero section) and copies the whole
file to docs/archive/turkey-econ-YYYYMMDD.html, so the link you're
about to add to ARCHIVE_ITEMS for that date will actually resolve.

    python3 archive_today.py
    # ... now edit docs/index.html for the new day ...
    # ... add a new ARCHIVE_ITEMS entry pointing at
    #     /archive/turkey-econ-<yesterday>.html ...
    # ... commit, push, let the workflow fill in the FX/funding tokens ...

It refuses to run if:
  - docs/index.html still has unfilled {{TOKEN}} placeholders
    (means it was never actually published -- nothing to archive)
  - the target archive file already exists (won't silently overwrite)
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).parent
LIVE_PATH = ROOT / "docs" / "index.html"
ARCHIVE_DIR = ROOT / "docs" / "archive"

DATE_RE = re.compile(r'<span class="lbl">DATE</span>(\d{4})年(\d{1,2})月(\d{1,2})日')


def main():
    if not LIVE_PATH.exists():
        raise SystemExit(f"找不到 {LIVE_PATH}")

    html = LIVE_PATH.read_text(encoding="utf-8")

    if "{{" in html:
        raise SystemExit(
            "docs/index.html 裡還有未填值的 {{TOKEN}}，代表這份還沒被\n"
            "render_report.py 處理過、還不是正式發布版，沒有東西可以存檔。\n"
            "先讓每日 workflow 跑完（或手動跑一次 render_report.py）再存檔。"
        )

    m = DATE_RE.search(html)
    if not m:
        raise SystemExit(
            "在 docs/index.html 裡找不到 DATE chip（<span class=\"lbl\">DATE</span>…），\n"
            "沒辦法判斷這是哪一天的簡報，存檔中止。"
        )

    y, mo, d = m.groups()
    date_str = f"{y}{int(mo):02d}{int(d):02d}"
    out_path = ARCHIVE_DIR / f"turkey-econ-{date_str}.html"

    if out_path.exists():
        raise SystemExit(
            f"{out_path} 已經存在，不覆蓋，避免蓋掉舊檔。\n"
            f"若真的要重新存檔，請先手動刪除該檔案再重跑。"
        )

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    print(f"-> 已將 {y}年{mo}月{d}日 的簡報存檔至 {out_path}")
    print("   接下來可以安心編輯 docs/index.html，開始寫新一天的內容了。")
    print("   別忘了在 ARCHIVE_ITEMS 裡也加一筆指向這個檔案的紀錄：")
    print(f'   {{"date":"{y}-{mo.zfill(2)}-{d.zfill(2)}", "href":"/archive/turkey-econ-{date_str}.html", ...}}')


if __name__ == "__main__":
    main()
