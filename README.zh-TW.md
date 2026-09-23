# zju-learning-cli

[简体中文](README.md) | 繁體中文

學在浙大（courses.zju.edu.cn）與智雲課堂（classroom.zju.edu.cn）的單檔命令列工具：同步課件、把智雲課堂的 PPT 截圖合併成 PDF（可去除重複截圖）、匯出課堂語音轉錄、查待辦。

它是 [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（ZLA）的 CLI 移植版。ZLA 是很好用的桌面 GUI，但沒辦法寫進腳本、排程或讓 AI agent 呼叫；這個專案把它的 API 邏輯改寫成一個 Python 檔，補上增量同步、並行下載、PPT 去重，也修了幾個上游的邊界狀況。

## 功能

| 指令 | 作用 |
| --- | --- |
| `zju login` | 設定學號，密碼存進系統憑證庫（macOS Keychain；其他系統走 [keyring](https://pypi.org/project/keyring/)） |
| `zju courses [--all]` | 課程列表（預設只列最新學年） |
| `zju sync [課程...]` | 增量同步課件到 `<輸出目錄>/<課程>/` |
| `zju todo` | 待辦事項，依截止時間排序（本地時區） |
| `zju classroom search 關鍵字` | 在智雲課堂找課，取得 `course_id` |
| `zju classroom subs <course_id>` | 列出該課每一堂的 `sub_id` |
| `zju classroom day [日期] [--days N]` | 某天（或最近 N 天）自己的課 |
| `zju ppt --course <id> \| --days N [--dedup]` | 智雲 PPT 截圖 → `<課程>/智雲PPT/<堂>.pdf` |
| `zju transcript --course <id> \| --days N` | 語音轉錄 → `<課程>/轉錄/<堂>.txt\|srt\|md` |

## 安裝

需要 [uv](https://docs.astral.sh/uv/)。相依套件寫在檔頭（PEP 723），第一次執行時 uv 會自動安裝。

```bash
git clone https://github.com/8eoyw/zju-learning-cli.git
ln -s "$PWD/zju-learning-cli/zju.py" ~/.local/bin/zju   # 或直接 ./zju.py
zju login
```

沒有 uv 的話：`pip install requests img2pdf pillow keyring numpy`，再用 `python zju.py ...` 執行。

## 使用

```bash
zju sync --dry-run                 # 先看會下載什麼、總共多大
zju sync                           # 最新學年全部課程
zju sync 微积分 123456              # 指定課程（名稱片段或 id，id 用 zju courses 查）
zju sync --videos --max-size 0     # 連影音和大檔一起抓
zju sync -j 8                      # 並行數（預設 4）

zju classroom day --days 7
zju ppt --days 1 --dedup           # 今天所有課的 PPT，重複截圖只留最完整的一張
zju transcript --days 1 --format md
```

輸出目錄的優先順序：`--out` > 環境變數 `ZJU_OUT` > `~/.config/zju-learning/config.json` 的 `"out"` > `~/ZJU-Courses`。

排程範例（cron，每天 22:00）：

```cron
0 22 * * * ~/.local/bin/zju sync && ~/.local/bin/zju ppt --days 1 && ~/.local/bin/zju transcript --days 1
```

退出碼：`0` 成功；`1` 設定、登入或 API 錯誤；`2` 部分檔案或堂次失敗（其餘照常完成）。

## 跟 ZLA 的差異

- **增量同步**：以 `.zju_manifest.json` 記錄 upload id，而不是比對檔名和大小；老師換了新版（新 id）才會重抓。
- **下載完整性**：先寫 `.part-*`，核對 `Content-Length` 後才 rename；空檔、截斷、伺服器回的 HTML 錯誤頁都不會被記成已下載。
- **並行下載**：課件預設 4 檔同時，PPT 截圖 8 張同時；每條執行緒有自己的 session，共用 cookie jar。遇到 429/503 會照 `Retry-After` 退讓。
- **PPT 去重**（`--dedup`）：智雲是對投影畫面定時截圖，同一頁會因動畫逐步出現、老師邊講邊寫、翻回前面而被截很多次。一頁的筆畫若全都還在後面那頁（或之前留下的某頁）裡就刪掉，所以動畫只留跑完的那張、手寫只留寫完的那張，註記不會丟。用局部對比找筆畫，白底、黑底、底圖紋理、教學影片都適用；實測三堂課 73→68、81→48、176→99 頁，逐頁核對無誤刪。`--keep-images` 仍保留全部原圖。
- **預設直連**，連不上才改走系統 proxy；連線 timeout 6 秒，排程時不會卡死。只有冪等請求會自動重試，登入 POST 不會被重送。
- **學年判斷**：學校常常不把舊課程標成已結束（`is_closed`），因此改用 `academic_year_id` 找最新學年。
- **同名課程**（不同教學班）分開存放；同一課程裡的同名檔一律加上 id，命名不受 API 回傳順序影響。
- 預設跳過影音檔和 200MB 以上的檔案（通常是軟體安裝包、專題壓縮檔），`--dry-run` 會列出總大小。

修掉的上游邊界狀況：

- 智雲 `search-ppt` 不遵守 `per_page`：常常第 1 頁就回傳全部，下一頁再重複一次。ZLA 假設每頁最多 100 張，超過 100 頁的課會一直重試然後失敗；這裡改成依序去重。
- 轉錄 API 對「還沒有語音資料」回傳 `code=10002`，現在當成「無轉錄」處理，不再中止整批。
- **明文傳送登入憑證**：智雲 PPT 截圖網址是 `http://`，而 `.zju.edu.cn` 的 SSO cookie（包括 `iPlanetDirectoryPro`）沒有設 `Secure`，照常下載就會把登入憑證用明文送出。這裡把學校主機的網址升級成 HTTPS，而且所有 `http://` 請求都不帶 Cookie 和 Authorization。
- `courses.zju.edu.cn` 與 `identity.zju.edu.cn` 只支援 1024-bit DHE／靜態 RSA，OpenSSL 3 預設拒絕連線（`DH_KEY_TOO_SMALL`）。`SECLEVEL=1` 只套用在這兩台，其他主機維持預設的 TLS 設定。
- 智雲的 `_token` cookie 設在 `.zju.edu.cn` 父網域，而不是 `classroom.zju.edu.cn`。

## 關閉下載的課件

老師關閉下載時，依序嘗試三條路：

1. `/api/uploads/reference/{rid}/blob`：正常下載
2. `/api/uploads/{id}/blob`：多數情況仍然拿得到原格式（ZLA、[eWloYW8/ZJU-course-material-download](https://github.com/eWloYW8/ZJU-course-material-download)、[xzzd-pro](https://github.com/xzzd-pro/xzzd-pro) 用的方式）
3. `/api/uploads/reference/document/{rid}/url?preview=true`：預覽器轉出來的 PDF（[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader) 用的方式）

**老師排定之後才開放的活動**，伺服器對以上所有端點都會回 403。這是權限控管，本工具不會嘗試繞過：這類檔案會標成 `[未開放]（開放時間）`，不算失敗，開放後下次 `sync` 會自動抓。

## 安全性

- 密碼只存在系統憑證庫。macOS 由系統 `security` 在終端機提示輸入，不會出現在命令列參數或 shell 歷史紀錄。也可以改用環境變數 `ZJU_USER` / `ZJU_PASS`。
- 登入時密碼先用 CAS 提供的公鑰加密再送出，跟網頁登入的做法相同。
- Session cookie 以 JSON（不是 pickle）快取在 `~/.config/zju-learning/cookies.json`；在 macOS／Linux 上檔案權限是 `0600`，目錄是 `0700`。快取位置可用環境變數 `ZJU_STATE_DIR` 覆寫。
- Cookie 只會透過 HTTPS 送往 `*.zju.edu.cn`，明文 `http://` 請求一律不帶。沒有任何遙測。
- TLS 驗證失敗會直接報錯，不會自動重試或改走 proxy，避免把中間人攻擊誤當成網路不穩。

## 免責聲明

只在 macOS 上實測過；Linux、Windows 理論上可以使用，但沒有測試過（Windows 請用 `python zju.py`）。

僅供個人學習使用。課件的著作權屬於授課教師與學校，請勿散布下載的內容；使用時請遵守學校的相關規定，不要高頻或大量抓取。學校的 API 沒有公開文件，隨時可能改版。

## 致謝

- [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（MIT）：登入流程、學在浙大與智雲課堂的 API 呼叫都移植自它的 `src-tauri/src/zju_assist.rs`。本專案沿用 MIT 授權並保留其版權聲明，見 [LICENSE](LICENSE)。
- [eWloYW8/ZJU-course-material-download](https://github.com/eWloYW8/ZJU-course-material-download)（MIT）、[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader)、[xzzd-pro/xzzd-pro](https://github.com/xzzd-pro/xzzd-pro)：參考了關閉下載時的端點做法（沒有複製程式碼）。

## 授權

[MIT](LICENSE)
