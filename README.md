# de-publish

DE 行銷解碼的排程發文（FB、IG、Threads）。這是公開 repo：Actions 不計分鐘，IG 需要的公開圖片網址也放這裡（發完刪除）。貼文內容放在私人 repo `de-marketing-archive` 的 `publish/<todo id>/`，發文前一刻才讀；金鑰都在 Actions Secrets。

**Actions 紀錄任何人都看得到**：腳本只印粉專名稱、帳號名稱、額度與 API 的錯誤訊息，不印網址（可能帶權杖）與金鑰。

## 檔案

| 檔案 | 做什麼 |
|---|---|
| `schedule.json` | 排程表：只有 todo id、時間、狀態（`scheduled` 已確認、`preview` 預覽待確認、`publishing` 發佈中）。由發文助手寫入。 |
| `state/<todo id>.json` | 每則的發佈結果：各平台貼文 id、連結、錯誤。重跑時已發的平台跳過。 |
| `tmp/<todo id>/` | IG 圖暫放處，發完就刪。 |
| `.github/workflows/alarm.yml` | 鬧鐘：睡到最早一則，時間到啟動 publish.yml；每段最多 5 小時 45 分，接力叫醒下一段。 |
| `.github/workflows/publish.yml` | 發佈：FB 多圖 → 粉專留言 vocus 連結 → IG 輪播 → Threads 主文 → Threads 第一則回覆（CTA＋vocus 連結，2026-11-27 起）。 |
| `.github/workflows/patrol.yml` | 每小時巡檢：鬧鐘斷了就重設、到時間沒發的就補發。 |
| `.github/workflows/threads-refresh.yml` | Threads 權杖每 10 天更新，寫回 Secret。 |
| `.github/workflows/check-setup.yml` | 只讀設定檢查；每週一自動跑，有問題或粉專資料存取期限 21 天內到期時通知 PM。 |

## 規則（Derrick 2026-11-25、11-26 決定）

- 不用平台原生排程：時間到才呼叫 API。FB 貼文發出後馬上留言 vocus 連結。
- 晚 60 分鐘內照常補發；晚超過 60 分鐘不自動發，PM 通知 Derrick 決定。
- 時間到還是「預覽待確認」（沒填 vocus 連結）的不發，PM 通知 Derrick 改時間。
- 結果用事件檔推到私人 repo 的 `events/`（`publish_done`、`publish_failed`、`publish_unconfirmed`、`publish_overdue`、`publish_warn`），由那邊叫醒 PM 即時通知。
- 60 天沒有任何變動時 GitHub 會停用定時 workflow；發文助手每次確認排程時檢查並重新啟用。

## 測試

`python tests/test_offline.py`：假的 API，不連網、不發文。
