# 經濟組資訊 — Firebase 設定

前台密碼編輯需要一個 Firebase 專案。整套只用兩個免費服務：Authentication（登入）
和 Firestore（存資料），用量遠低於免費額度。

已經有 Firebase 專案的話可以沿用，不必另開。

---

## 1. 建立專案

Firebase 主控台 → 新增專案。Google Analytics 可以關掉，用不到。

## 2. 開啟 Authentication

Build → Authentication → 開始使用 → 選「電子郵件/密碼」→ 啟用 → 儲存。

到「Users」分頁 → 新增使用者：

- 電子郵件：`evatzeng@taitra.org.tw`
- 密碼：自己設一組，這就是之後在首頁輸入的密碼

Firebase 不會寄信給這個地址，也不需要驗證，它只是帳號代號。密碼是在
Firebase 裡另外設的，跟你公司信箱的密碼無關，請不要設成同一組。
如果之後要換帳號，記得同步改範本裡的 `EDITOR_EMAIL`。

## 3. 建立 Firestore

Build → Firestore Database → 建立資料庫 → 選「正式環境模式」→ 位置選
`eur3` 或 `europe-west` 系列（離伊斯坦堡近）。

建好後到「規則」分頁，整份換成：

```
rules_version = '2';
service cloud.firestore {
  match /databases/{database}/documents {

    // 經濟組資訊：所有人可讀（首頁訪客要看得到），
    // 只有登入的帳號可以寫。
    match /site/econ-notes {
      allow read: if true;
      allow write: if request.auth != null;
    }

    // 其他路徑一律封死
    match /{document=**} {
      allow read, write: if false;
    }
  }
}
```

按「發布」。

**這一步是真正的安全防線。** 沒有它，任何人都能改你的資料。

## 4. 取得設定值

專案設定（齒輪）→ 一般 → 你的應用程式 → 如果還沒有網頁應用程式就按
`</>` 新增一個（名稱隨意，不用勾 Firebase Hosting）。

畫面會給你一段 `firebaseConfig`。把裡面六個值填進
`templates/report-template.html` 最下方那段腳本開頭的 `firebaseConfig`，
取代 `PASTE_YOUR_...` 那些佔位字串。

這些值公開在網頁原始碼裡是正常的，Firebase 本來就設計成如此——擋人的是
上面那份規則，不是把 apiKey 藏起來。

## 5. 限制網域（建議）

Authentication → 設定 → 已授權的網域，只留 `ttcistanbul.github.io`
和 `localhost`。這樣別人把你的設定複製到自己的網站也登入不了。

## 6. 部署與測試

1. commit `templates/report-template.html`
2. Actions → 跑一次 Refresh Site
3. 開首頁，捲到最下面「經濟組資訊」，按「編輯」
4. 輸入第 2 步設的密碼 → 打字 → 儲存並發布
5. 用無痕視窗開同一頁，確認訪客看得到內容、但按編輯進不去

---

## 日常使用

- 進首頁按「編輯」，輸入密碼
- 每一則有：日期、標籤（公告／提醒／更正，可自己打）、內容、是否標為重點
- 「＋ 新增一則」加、「刪除這則」移除
- 「儲存並發布」之後首頁立即更新，不用跑任何 workflow

登入狀態會留在瀏覽器裡，同一台電腦下次不用再輸入密碼。共用電腦記得按「登出」。

## 內容存在哪裡

Firestore 的 `site/econ-notes` 這一份文件，欄位 `items` 是陣列。
不在 git 裡，所以 `render_report.py` 每天重新產生首頁也不會影響它。

要備份就到 Firestore 主控台把那份文件的內容複製下來。
