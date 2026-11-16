# 發布檢查 未通過 ❌ —— 今天的網頁沒有更新（2026-11-16）

## ✅ 1. 資料與分析是今天的
- 原始資料與分析皆為 2026-11-16

## ❌ 2. AI 寫的數字都能對回原始 JSON
- `key_events[0].headline` 的數字 **6** 在原始資料找不到來源（上下文：「…耳其前10月汽車產量下滑6%，但出口金額成長3…」）
- `key_events[0].summary` 的數字 **6** 在原始資料找不到來源（上下文：「…年前10個月汽車產量年減6%，以數量計之出口量…」）
- `key_events[0].summary` 的數字 **9** 在原始資料找不到來源（上下文：「…%，以數量計之出口量下滑9%，其中乘用車產量降…」）
- `key_events[0].summary` 的數字 **17** 在原始資料找不到來源（上下文：「…，其中乘用車產量降幅更達17%。然而，汽車產業出…」）
- `key_events[4].headline` 的數字 **500** 在原始資料找不到來源（上下文：「…，目標向工業綠色轉型提供500億里拉融資…」）
- `key_events[4].summary` 的數字 **500** 在原始資料找不到來源（上下文：「…，目標在2030年前提供500億里拉資金，專門協助…」）
- `industry_items[0].headline` 的數字 **6** 在原始資料找不到來源（上下文：「…耳其前10月汽車產量年減6%，乘用車產量重挫1…」）
- `industry_items[0].headline` 的數字 **17** 在原始資料找不到來源（上下文：「…年減6%，乘用車產量重挫17%…」）
- `industry_items[0].body` 的數字 **6** 在原始資料找不到來源（上下文：「…個月土耳其汽車總產量下降6%，其中乘用車產量降…」）
- `industry_items[0].body` 的數字 **17** 在原始資料找不到來源（上下文：「…%，其中乘用車產量降幅達17%。不過，汽車產業出…」）
- ⚠️ 提醒（不擋發布）：`key_events[0].headline` 的數字 **352** 不在引用的那則新聞裡，但在今天另一則新聞找到：新聞「Otomotivde 10 aylık bilanço: Ü」（Dünya）；建議把事件連結補成兩則或改寫
- ⚠️ 提醒（不擋發布）：`key_events[0].summary` 的數字 **352** 不在引用的那則新聞裡，但在今天另一則新聞找到：新聞「Otomotivde 10 aylık bilanço: Ü」（Dünya）；建議把事件連結補成兩則或改寫
- ⚠️ 提醒（不擋發布）：`industry_items[0].body` 的數字 **352** 不在引用的那則新聞裡，但在今天另一則新聞找到：新聞「Otomotivde 10 aylık bilanço: Ü」（Dünya）；建議把事件連結補成兩則或改寫

## ❌ 3. 每個事件都有來源連結（且連結、日期對得上原始新聞）
- key_events[0]「土耳其前10月汽車產量下滑6%，但出口金額成長3」 的連結不在今天的原始新聞裡（可能是模型編的）：https://www.dunya.com/sektorler/otomotiv/otomotiv-10-aylik-bilanco-uretim-dustu-ihracat-geliri-artti-haberi-847728
- key_events[4]「Akbank 推出 DENGE 計畫，目標向工業」 的連結不在今天的原始新聞裡（可能是模型編的）：https://yesilhaber.net/akbank-denge-sanayi-50-milyon-tl/
- industry_items[0]「土耳其前10月汽車產量年減6%，乘用車產量重挫1」 的連結不在今天的原始新聞裡（可能是模型編的）：https://www.dunya.com/sektorler/otomotiv/otomotiv-10-aylik-bilanco-uretim-dustu-ihracat-geliri-artti-haberi-847728

## ❌ 4. 沒有簡體字或大陸用語（OpenCC）
- 簡體字「峰」（應為「峯」）出現 1 次，例如：…k 於 COP31 氣候峰會發表 DENGE 計…

## ✅ 5. 頁面完整（無殘留 token、匯率與融資成本已寫入）
- token 皆已替換，數字已寫入

---
網頁維持昨天的版本。處理方式：修正後到 Actions 手動重跑「Daily Turkiye Econ Data Fetch」；
若確認某個數字是正確的，可登記到 `verify_allowlist.json` 放行。