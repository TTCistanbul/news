#!/usr/bin/env python3
"""布蘭特原油三個來源的連線探測。

放在 repo 根目錄跟 fetch_daily.py 同一層，直接跑：

    python brent_probe.py

會分別打 Yahoo 期貨、EIA 現貨、datahub 現貨鏡像，印出每一條「抓不抓得到」
以及「最新一筆是哪一天、多少錢」。不寫檔、不改 data/，純粹看網路通不通。

要順便驗 EIA 那條，先設環境變數：
    EIA_API_KEY=xxxx python brent_probe.py

如果要確認 GitHub Actions 的機器抓不抓得到（跟你本機不同 IP，結果可能不一樣），
把這支也 commit 上去，在 daily-fetch.yml 裡臨時加一步 `python brent_probe.py`
手動觸發一次，看 log 就知道。
"""
import datetime as dt
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("fd", "fetch_daily.py")
fd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fd)

today = dt.date.today()


def report(label: str, fn):
    print(f"\n── {label} " + "─" * (46 - len(label)))
    try:
        obs = fn()
    except Exception as e:
        print(f"  ✗ 失敗：{type(e).__name__}: {e}")
        return None
    obs = sorted(obs, key=lambda x: x[0])
    d, p = obs[-1]
    print(f"  ✓ 成功　共 {len(obs)} 筆")
    print(f"    最新一筆：{d}　${p:.2f}　落後 {(today - d).days} 天")
    print(f"    再往前三筆：" +
          "，".join(f"{x[0]} ${x[1]:.2f}" for x in obs[-4:-1]))
    return obs


print(f"今天：{today}")
report("Yahoo 期貨 BZ=F（第一順位）", fd._fetch_brent_yahoo)

key = (os.environ.get("EIA_API_KEY") or "").strip()
if key:
    report("EIA 現貨 RBRTE（第二順位）", lambda: fd._fetch_brent_eia(key))
else:
    print("\n── EIA 現貨 RBRTE（第二順位）───────────────────")
    print("  — 跳過：沒有設定 EIA_API_KEY")
    print("    （這就是 09-09 之後一直掉到第三順位的原因，"
          "如果 GitHub 上的 secret 也沒設，Actions log 會印同一句話）")

report("datahub 現貨鏡像（第三順位）", fd._fetch_brent_datahub)

print("\n" + "=" * 52)
print("整支跑一次的實際結果（fetch_brent_oil 會用的那一條）：")
brent = fd.fetch_brent_oil()
if not brent:
    print("  ✗ 三條都失敗，brent_oil 會是 None，頁面會顯示「待接即時報價」")
    sys.exit(1)
print(f"  來源 {brent['source_kind']}（{brent['basis']}）")
print(f"  ${brent['usd_per_barrel']:.2f}　{brent['date']}　"
      f"落後 {brent['age_days']} 天　stale={brent['stale']}")
ch = brent.get("changes") or {}
print("  週／月／年初至今：" + "　".join(
    f"{lab} {ch[k]['pct']:+.1f}%" if ch.get(k) else f"{lab} —"
    for lab, k in (("週", "week"), ("月", "month"), ("年初至今", "ytd"))))
