#!/usr/bin/env python3
"""
fix_terminology.py — 一次性批次修正用語

用途：掃過 docs/archive/ 底下所有 .html 存檔（過去每天的簡報快照，
render_report.py 不會重新產生這些檔案，內容是靜態的），把過時／不一致
的用詞統一替換掉。docs/index.html 不用管，它每天都會被 render_report.py
整份重新產生，換過新版 generate_analysis.py 之後會自動用新譯名。

這支腳本只做文字替換，不碰任何 HTML 結構、不碰 <script> 裡的 JS 邏輯、
不碰 {{TOKEN}} 或 <!-- MARKER --> 這類本來就該存在的標記語法——
REPLACEMENTS 這份清單本身就是普通中文詞彙，不會誤傷到程式碼。

用法：
    python3 fix_terminology.py            # 實際執行替換
    python3 fix_terminology.py --dry-run  # 只列出會被改到哪些檔案、改幾次，不寫入

之後如果還有其他要統一的用詞，直接在 REPLACEMENTS 裡加一行就好，
不用改其他邏輯。
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent
ARCHIVE_DIR = ROOT / "docs" / "archive"

# (舊詞, 新詞) —— 由上而下依序套用
REPLACEMENTS = [
    ("伊斯坦布爾", "伊斯坦堡"),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只顯示會改到哪裡，不實際寫入檔案")
    args = ap.parse_args()

    if not ARCHIVE_DIR.exists():
        print(f"! {ARCHIVE_DIR} 不存在，沒有任何存檔可以修正", file=sys.stderr)
        return

    html_files = sorted(ARCHIVE_DIR.glob("*.html"))
    if not html_files:
        print("docs/archive/ 底下沒有找到任何 .html 檔案", file=sys.stderr)
        return

    total_files_changed = 0
    total_replacements = 0

    for path in html_files:
        text = path.read_text(encoding="utf-8")
        original = text
        file_replacements = 0

        for old, new in REPLACEMENTS:
            count = text.count(old)
            if count:
                text = text.replace(old, new)
                file_replacements += count

        if file_replacements:
            total_files_changed += 1
            total_replacements += file_replacements
            print(f"{'[會修改]' if args.dry_run else '[已修改]'} {path.name}："
                  f"{file_replacements} 處")
            if not args.dry_run:
                path.write_text(text, encoding="utf-8")

    print()
    if total_files_changed == 0:
        print("沒有任何檔案需要修正，用詞已經一致。")
    else:
        verb = "會被修改" if args.dry_run else "已修改"
        print(f"共 {total_files_changed} 個檔案、{total_replacements} 處{verb}。")
        if args.dry_run:
            print("這是預覽模式，沒有真的寫入檔案。拿掉 --dry-run 再跑一次才會實際套用。")


if __name__ == "__main__":
    main()
