# SnapMan — Snapshot Manager

自建的精簡版 SnapCenter / SnapManager，不綁特定儲存品牌，給 vSphere 環境的 DBA / 基礎架構團隊使用。解決兩個問題：

| 任務類型 | 做什麼 | 解決什麼 |
|---|---|---|
| **disk**（資料碟副本） | 對**開機中**的來源 VM（MSSQL）拍**應用一致**靜默快照 → 從一致點 clone 出獨立資料 VMDK → **熱掛**給開機中的目標 VM 並 SQL ATTACH + DBCC 驗證 | 目標每天拿到一份可用副本做 offload 運算（報表、測試、演練），不碰 production 資料本體 |
| **vmsync**（整機同步） | 定時把指定 VM（開機中亦可）完整複製到另一座 vCenter，目標端只留最新一份、保持關機 | 免共享儲存、免 SRM 的 DR standby；複本被開機時有覆蓋保護 |

**術語**

| 術語 | 說明 |
|---|---|
| 一致點 | VMware Tools `quiesce=True` 快照觸發 SQL Server VSS Writer 後的應用一致狀態 |
| 副本（CloneCopy） | disk 任務每輪 clone 出的獨立 VMDK；可多時間點保留、任選掛回、歸檔 |
| 換手 | vmsync 新複本到位後，刪上一份、改為正式名稱的動作 |
| XVM | Cross-vCenter vMotion（舊稱 XVC；設定值仍為 `xvc`），跨 vCenter 搬遷 VM / 磁碟 |
| OVF 串流 | vmsync 另一種跨 VC 傳輸：來源 ESXi 匯出 → SnapMan 記憶體中轉 → 目的 ESXi 匯入，不落地 |
| Guest Operations | VMware Tools 提供的 guest 內執行命令機制，不需打通到 guest 的網路 |
| 所有權標記 | vmsync 複本 extraConfig `snapman.owner=profile:<id>`，換手只覆蓋帶本任務標記的複本 |

開發指引與專案歷程見 [CLAUDE.md](CLAUDE.md)。

---

## 領域內容

| 對象 | 內容 | 依據 / 介面 |
|---|---|---|
| 來源 VM | 開機中的 Windows + MSSQL（可具名執行個體）；vmsync 亦可為關機 VM 或 VM 範本 | pyVmomi `CreateSnapshot(quiesce)`、Guest Operations |
| 目標 VM | 開機中的 Windows + SQL Server（版本 ≥ 來源）+ sqlcmd | Guest Operations、`ReconfigVM`（hot-add） |
| vCenter | 一座或多座（來源 / 目標可分綁） | vSphere API 7.x / 8.x；跨 VC 走 XVM 或 OVF 串流 |
| Datastore | clone 落點、暫存、歸檔；共享 NFS 以 `summary.url` 驗真共享 | `CopyVirtualDisk`、`DeleteVirtualDisk`、datastore 瀏覽 |
| 資料庫 | 來源 DB 清單（留空 = 檔案磁碟分享模式） | `sys.master_files` 權威路徑、`DBCC CHECKDB (PHYSICAL_ONLY)` |
| 網路 | vmsync 複本網卡重接的目標 portgroup（標準 / DVS） | RelocateSpec deviceChange 或 OVF networkMapping |
| 帳號 | 本機 admin + AD 群組登入；分權表決定角色 | ldap3（NTLM / LDAPS） |

---

## 功能特性 / 運作模式

### 模式對照

| 模式 | 任務類型 | 條件 | 適用情境 |
|---|---|---|---|
| SQL 副本 | disk | 填 DB 清單；目標有 SQL Server | 每日給報表 / 測試環境一份可 ATTACH 的 DB 副本 |
| 檔案磁碟分享 | disk | DB 清單留空 | 非 SQL 的資料碟，只需檔案系統一致的每日副本掛給目標 |
| 共享儲存 `shared` | disk（跨 VC） | 同一 NFS export 同名掛給兩座 VC 的主機（前置檢查比對 volume URL） | 兩座 VC 有真共享儲存 |
| XVM `xvc` | disk / vmsync（跨 VC） | 發起端主機版本 + build 不得比目的端 vCenter 新，反向亦然（系統自動挑主機） | 兩側版本相容、要走 vSphere 原生搬遷 |
| OVF 串流 `ovf` | vmsync（跨 VC） | SnapMan 可直連兩側 ESXi 443；VM 無 vTPM / 加密 | 版本不相容、無共享儲存；WAN 只過一次 |

### disk：每日工作流 ①~⑩

| # | 步驟 | 說明 |
|---|------|------|
| ① | 前置檢查 | 來源/目標開機、VMware Tools 正常、SQL Server VSS Writer 就緒 |
| ② | SQL CHECKPOINT | 對來源 DB 刷髒頁 |
| ③ | 靜默快照 | `CreateSnapshot(quiesce=True)` → VMware Tools 觸發 SQL VSS Writer，取**應用一致點** |
| ④ | Clone 資料 VMDK | 從一致點 clone 出一顆**獨立**碟（快照不能直接掛別台，必須 clone） |
| ⑤ | 立即移除快照 | 縮短 production stun 風險 |
| ⑤+ | 跨 VC 搬遷 | 僅 xvc 傳輸模式：暫存 clone 以殼 VM + XVM 搬到目標側 |
| ⑥ | 卸載上一輪 clone | SINGLE_USER 斷開殘留連線 → DETACH 舊 DB → 磁碟 offline → 移除舊碟（檔案保留，交給保留策略） |
| ⑦ | 熱掛新 clone | hot-add 到開機中的目標 VM |
| ⑧ | 上線 + ATTACH | 磁碟 online、自動指派碟號、依來源 `sys.master_files` 權威路徑 `ATTACH` |
| ⑨ | 驗證 | `DBCC CHECKDB`（PHYSICAL_ONLY） |
| ⑩ | 收尾 | 更新狀態與 clone 紀錄 |

- **DB 清單留空 = 檔案磁碟分享模式**：跳過所有 SQL 步驟，快照仍走 VSS 靜默（檔案系統一致），目標端只掛碟 + 指派碟號。
- **跨 vCenter**：任務可分綁來源/目標 vCenter。傳輸模式 `shared`（clone 直接寫入共享 NFS；兩地各一台「同名」NAS 不算共享，①會擋）或 `xvc`（免共享儲存，殼 VM + XVM 搬遷）。

### vmsync：整機同步 ①~⑦

> 前置檢查 → 靜默快照 → 從一致點 clone 完整暫存 VM → 移除快照 → 跨 VC 傳輸（網卡自動重接指定目標 portgroup）→ 目標端換手 → 收尾

- 目標端**只保留最新一份**複本（新複本到位成功才刪上一份），保持**關機** standby。
- **覆蓋保護**：複本若被開機（可能正被 DR 使用），絕不覆蓋、run 失敗並告警。
- 關機中的來源 VM 也可同步（不需 Tools）；同一座 vCenter 亦支援（略過搬遷、改 ReconfigVM 重接網卡）。
- **範本對範本**：來源若是 VM 範本（vSphere 不允許對範本拍快照），②自動略過、直接以範本現行狀態 clone，複本到目的端定名後**自動標記為範本**；標記失敗時複本仍完整可用（只差型別），run 報錯提示手動 Mark as Template。
- 跨 VC 傳輸方式二選一：
  - **XVM 搬遷**：由來源側主機發起，其 ESXi 版本**與 build** 不得比目的端 vCenter 新，目的端落點主機亦不得比來源 vCenter 新（系統兩側自動挑相容主機）。儲存任務時會做 **XVM 相容性預檢**（不通過仍儲存但顯示警告），「檢視狀態」列出預檢結果與兩側挑到的主機。
  - **OVF 串流**：來源主機匯出 → 經 SnapMan 記憶體中轉（不落地、免準備空間）→ 目的主機匯入，**免主機版本相容、免共享儲存**；目的端 thin 佈建。以「涓流保留」機制對付 ESXi 匯出流在接近結尾處的數分鐘停滯（上傳端保留尾段 64KB，停滯時涓流送出維持連線）。匯入完成後**自動補複製 .nvram**（OVF 本身不帶 NVRAM，EFI 複本沒有它會開不了機）。限制：VM 硬體版本仍須目的端主機支援、vTPM/加密 VM 不可匯出。傳輸中斷即整趟重來（無斷點續傳，失敗回滾後由下次排程重跑）。
- 每次為**全量複製**（非增量），傳輸時間與 VM 大小成正比；來源側需暫存整台 VM 的空間。

### 排程

- 任務清單最左的「啟用 / 停用」即**排程啟用狀態**：啟用 = 依排程自動執行，停用 = 僅手動執行。
- **每日**：每天於指定時間（顯示時區、24 小時制）執行；因併發上限或服務重啟錯過時，當日內自動補跑。
- **一次性**：指定日期＋時間執行一次，成功起跑後**自動停用排程**；過期未跑會補跑。
- **任務串接**（本任務成功後自動觸發下一個，含循環偵測）、**全域同時執行上限**、**同 VM 互斥**（共用同一台來源/目標 VM 的任務自動排隊）。

### 副本管理（disk 任務）

- **多時間點保留**：「保留副本數」（含掛載中一份），超額舊副本每日自動刪除；刪檔失敗保留紀錄下輪重試，並對帳 datastore 實際檔案清孤兒。
- **任選掛載**：「副本」頁可把任一歷史副本掛回目標（卸下目前 → 掛上選定 → ATTACH + DBCC 的五步工作流）。
- **副本驗證 / DR 演練**：排程或手動「演練」——掛最舊保留副本跑 DBCC（＋選填自訂驗證 SQL），驗畢自動換回原掛載，發送演練報告。
- **副本歸檔**：每日成功後把當日副本再複製一份到歸檔 datastore，依保留份數修剪。
- 搭配 datastore 端去重（如 NAS 端的 Btrfs 去重）可壓低多份副本的空間成本。

### 監控與營運

- **儀表板**：今日成功/失敗、30 天趨勢圖、近期執行。
- **即時進度**：Run 詳情頁 WebSocket 串流每步狀態與 clone 進度百分比；可安全停止（步驟邊界生效；clone 中會取消 vSphere 工作並回滾）。
- **行事曆**：6 週月曆網格，每筆執行一個依狀態上色的 chip；未來日期虛線 chip 預告排程。
- **告警**：失敗必發、成功可選——Webhook（POST `{"text": ...}`）與/或 SMTP 郵件。
- **稽核**：登入/執行/停止/設定/副本操作全記錄，分「操作稽核」與「系統事件」兩頁籤。
- **執行紀錄保留**：設定天數，每日自動清過期 Run/StepLog。

### 登入與帳號分權（RBAC）

- 本機管理員 **admin**（緊急備援；首次啟動自動產生初始密碼寫入 `data/initial_admin_password.txt`，登入後**強制變更**（≥12 字元、不得為 admin）；忘記可用 `python -m app.set_admin_password` 重設 / `--disable` 停用）＋可選 **AD（NTLM）** 網域登入（Service Account 搜尋 → 使用者 bind 驗密 → 群組授權）。
- AD 群組只控制「誰能登入」；登入後權限由設定頁「帳號分權」表決定：**Full Admin**（全功能）/ **Admin ReadOnly**（全頁面唯讀）/ **Audit**（僅儀表板、工作階段、行事曆、報表、記錄）。授權於伺服器端 middleware 強制、每請求重查分權表（10 秒 TTL 快取）；WebSocket 同受保護。未指派角色的帳號預設 **Admin ReadOnly**。
- 登入失敗速率限制：同 IP＋帳號 5 分鐘 5 次、同 IP 不分帳號 5 分鐘 20 次；失敗訊息一律「帳號或密碼錯誤」，真實原因只寫稽核。
- AD 連線可選 **LDAPS（636）** 與憑證驗證（預設 389 以相容既有環境）。

### 介面

Veeam 式側欄主控台（Jinja2 + HTMX，無前端建置流程）；RWD（窄螢幕側欄收合為抽屜）；暗黑模式（日/夜切換、記憶偏好、跟隨系統）。VM / VMDK / datastore / portgroup 皆為下拉選單自動載入（啟動抓一次、背景每 30 分鐘刷新）。

---

## 服務埠 / 對外介面

| 方向 | 埠 / 協定 | 用途 | 如何改 |
|---|---|---|---|
| 對內（監聽） | **8070/TCP HTTP**（含 WebSocket `/ws/...`） | 網頁主控台 | `config.json` 的 `web_host` / `web_port`，或環境變數 `SNAPMAN_WEB_HOST` / `SNAPMAN_WEB_PORT`；需重啟 |
| 對外 | vCenter 443/TCP | vSphere API（pyVmomi）、`/folder` 檔案 API（NVRAM 補複製） | 設定頁 vCenter 主機 |
| 對外 | ESXi 443/TCP | OVF 串流（ExportVm / ImportVApp lease，兩側主機） | 僅 vmsync `ovf` 模式需要 |
| 對外 | AD 389 / 636（LDAPS）/TCP | 網域登入 | 設定頁 AD 區、`ad_use_ssl` |
| 對外 | SMTP（預設 25，可 STARTTLS） | 郵件告警 | 設定頁 SMTP 區 |
| 對外 | Webhook HTTP(S) | 告警 POST | 設定頁 Webhook URL |

網頁本體為 HTTP（接受風險，內網工具）；需要 TLS 請前置反向代理。

---

## 技術架構

| 層 | 選擇 |
|---|---|
| 後端 | Python 3 + FastAPI（單一進入點 `app/main.py`：路由、middleware、排程器、WebSocket、清單快取） |
| vSphere 自動化 | pyVmomi；同步阻塞呼叫以 `asyncio.to_thread` 包裝；session 每次呼叫檢查、失效自動重連 |
| VM 內操作 | VMware Tools Guest Operations；PowerShell 走 `-EncodedCommand`；以 exit code 判成敗並取回輸出 |
| 工作流引擎 | 自寫 `RunManager` 單例：逐步持久化 StepLog、失敗回滾、副本記帳、同 Profile 互斥 + 同 VM 互斥 + 全域併發上限 |
| 資料庫 | SQLAlchemy + SQLite（`data/snapman.db`）；啟動時輕量 ALTER 遷移 |
| 前端 | Jinja2 + HTMX（自帶 `app/web/static/htmx.min.js`，零外部 CDN）+ WebSocket 即時進度；無建置流程 |
| 驗證 | 本機 admin（初始密碼 + 強制變更）＋ ldap3 AD NTLM（可 LDAPS） |
| Session / 授權 | Starlette SessionMiddleware（signed cookie、SameSite=Lax）；RBAC middleware 每請求重算角色；非 GET 同站檢查（Sec-Fetch-Site / Origin） |
| 設定 | pydantic-settings：`config.json` > 環境變數 `SNAPMAN_*` > `.env` > 預設；設定頁維護、原子寫入、即時套用 |
| 排程 | 內建 asyncio 排程器（每日 / 一次性 / 驗證排程，皆 catch-up 補跑）+ 每日保留策略 / 歸檔 / 紀錄清理 |
| 告警 | Webhook（URL 驗證）/ SMTP（STARTTLS 可驗憑證） |
| 相依鎖定 | `requirements.txt` 下限、`requirements.lock.txt` 精確鎖定（部署裝 lock） |
| 部署 | `python -m app.main`（uvicorn）；開機自啟需自行以工作排程器 / NSSM 設定 |

---

## 目錄結構

```
app/
  main.py               # FastAPI 進入點：路由、登入/RBAC/CSRF middleware、安全標頭、排程器、WebSocket、vCenter 清單快取
  config.py             # 設定（config.json / env / .env）＋ session secret 產生
  database.py           # SQLAlchemy 引擎 + 輕量 schema 遷移（_migrate）
  models.py             # VCenter / Profile / Run / StepLog / CloneCopy / AuditLog / AccountRole
  auth.py               # AD（NTLM/LDAPS）驗證、LDAP 逃逸；本機 admin 密碼生命週期
  set_admin_password.py # CLI：互動重設 / 停用本機 admin 密碼
  notify.py             # Webhook / SMTP 告警
  timeutil.py           # 顯示時區轉換
  vsphere/
    base.py             # VSphereClient 介面（Protocol）
    real.py             # pyVmomi 實作：快照 / clone / 掛卸碟 / XVM 搬遷 / OVF 串流 / NVRAM 補複製 / 整機同步
  workflow/
    steps.py            # STEPS（disk ①~⑩）/ MOUNT_STEPS（掛載五步）/ VMSYNC_STEPS（整機同步七步）
    manager.py          # RunManager：執行、持久化、進度廣播、回滾、保留策略、告警
  web/
    static/htmx.min.js  # 自帶前端資源（零 CDN）
    templates/          # Jinja2 + HTMX 頁面
      base.html         #   側欄框架、暗黑模式
      dashboard.html    #   儀表板（/）
      jobs.html         #   任務清單（/jobs）
      profile_edit.html #   任務表單（新增 / 編輯）
      copies.html       #   副本頁（掛載 / 刪除歷史副本）
      sessions.html     #   工作階段（近百筆 run）
      run.html          #   Run 詳情 + WebSocket 即時進度
      calendar.html     #   行事曆
      reports.html      #   報表
      capacity.html     #   容量
      logs.html         #   稽核 / 系統事件
      settings.html     #   設定（vCenter、Guest、AD、分權、告警、一般）
      login.html / denied.html
      _icons.html / _options.html / _profile_status.html  # 共用片段（HTMX partial）
scripts/preflight.py    # 上線前唯讀檢查（不動任何 VM）
config.example.json     # 設定範例
requirements.txt        # 相依下限
requirements.lock.txt   # 部署用精確鎖定（含間接相依）
config.json             # [gitignore] 執行期設定，設定頁維護，含明碼帳密
data/                   # [gitignore] 執行期產物：snapman.db、initial_admin_password.txt
.env                    # [gitignore] 選用的環境變數檔
```

---

## 主要路由 / API

**所有路由皆需登入**（例外只有 `/login` 與 `/static/*`）；沒有免登入的機器介面。角色限制：Admin ReadOnly 一律拒絕非 GET；Audit 僅可用標 **A** 的路由。

| 路徑 | 方法 | 說明 |
|---|---|---|
| `/` | GET | 儀表板 **A** |
| `/login` · `/logout` | GET/POST · POST | 登入（速率限制）· 登出 **A** |
| `/jobs` | GET | 任務清單 |
| `/profiles` | POST | 新增任務（表單白名單驗證、串接循環偵測、XVM 預檢） |
| `/profiles/{id}/edit` | GET/POST | 編輯任務 |
| `/profiles/{id}/run` | POST | 手動執行 |
| `/profiles/{id}/drill` | POST | 手動副本驗證 / DR 演練 |
| `/profiles/{id}/copies` | GET | 副本頁 |
| `/profiles/{id}/copies/{copy_id}/mount` · `/delete` | POST | 掛載 / 刪除歷史副本 |
| `/runs/{run_id}` | GET | Run 詳情 **A** |
| `/runs/{run_id}/stop` | POST | 停止執行（步驟邊界生效） |
| `/ws/runs/{run_id}` | WebSocket | 即時進度串流（含已結束 run 的回放） |
| `/sessions` · `/sessions.csv` | GET | 工作階段 · CSV 匯出（公式注入防護） **A** |
| `/calendar` | GET | 行事曆 **A** |
| `/reports` | GET | 報表 **A** |
| `/capacity` | GET | datastore 用量與 SnapMan 副本檔案清單 |
| `/logs` | GET | 稽核紀錄 / 系統事件 **A** |
| `/settings` | GET/POST | 設定頁（寫入 `config.json`，即時套用） |
| `/settings/test-alert` | POST | 測試告警 |
| `/vcenters` · `/vcenters/{id}/edit` · `/delete` | POST | vCenter 新增 / 編輯 / 刪除 |
| `/roles` · `/roles/{id}/edit` · `/delete` | POST | 帳號分權新增 / 編輯 / 刪除 |
| `/api/vms` · `/api/vms/{vm}/disks` · `/api/datastores` | GET | JSON 清單（取自快取） |
| `/partials/options/{vms,datastores,networks,disks}` | GET | HTMX 下拉選項片段 |
| `/partials/profile-status/{id}` | GET | HTMX 任務狀態片段（含 XVM 預檢結果） |

---

## 安裝與啟動

```powershell
# 1. 建立虛擬環境並安裝相依（部署一律裝 lock 檔；升級時改 requirements.txt 下限 → 測試 → 重產 lock）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt

# 2. 啟動（綁定 0.0.0.0:8070）
.\.venv\Scripts\python.exe -m app.main
# 開發熱重載（有 run 在跑時勿用：改檔即重啟、會殺掉執行中的 run）：
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8070 --reload

# 3. 上線前檢查（唯讀，不動任何 VM）
.\.venv\Scripts\python.exe scripts\preflight.py

# 忘記 admin 密碼：互動重設（AD 已啟用時可加 --disable 停用本機 admin）
.\.venv\Scripts\python.exe -m app.set_admin_password
```

首次登入：瀏覽器開 `http://<主機>:8070`，帳號 `admin`，初始密碼在 `data/initial_admin_password.txt`（首次啟動自動產生）。登入後會被導到設定頁**強制變更密碼**（≥12 字元、不得為 admin），變更後該檔自動刪除。

> 改程式或模板後須重啟服務（新模板配舊程式會 500）。

---

## 功能頁面 / 使用說明

### 頁面

| 頁面 | 摘要 |
|---|---|
| 儀表板 | 今日成功/失敗、30 天趨勢（成功/失敗堆疊、耗時折線）、近期執行 |
| 任務 | 任務清單（啟用/停用排程、執行、演練、副本、編輯）；表單依任務類型自動切換欄位，VM / 磁碟 / datastore / 網路皆下拉 |
| 副本 | 某任務的歷史副本清單；任選一份掛回目標、刪除 |
| 工作階段 | 近百筆執行紀錄、CSV 匯出；點入 Run 詳情看每步日誌與即時進度、可停止 |
| 行事曆 | 6 週月曆，執行紀錄依狀態上色，未來排程虛線預告 |
| 報表 | 各任務成功率、平均耗時、各步驟平均耗時 |
| 容量 | 各 datastore 用量、SnapMan 副本 / 歸檔檔案清單 |
| 記錄 | 操作稽核（人為）與系統事件（排程觸發、保留策略、串接）兩頁籤 |
| 設定 | vCenter（多座）、Guest 帳密、本機 admin 密碼、AD 登入、帳號分權、告警（含測試）、顯示時區、紀錄保留、併發上限 |

### 使用步驟

1. **設定**：新增 vCenter（主機 / 帳號 / 密碼 / 是否略過 TLS 驗證）、Guest 帳密（目標 SQL 需該帳號 sysadmin）、告警；寫入 `config.json`，即時套用不需重啟。
2. **建任務**：「任務」頁選任務類型（disk / vmsync），填來源/目標 VM、磁碟、datastore、傳輸模式、（vmsync）目標 portgroup。
3. **排程**：勾「啟用排程」，選每日或一次性；或隨時按「執行」手動跑。
4. **監控**：儀表板看總況、Run 詳情看即時進度、行事曆 / 報表看歷史。

### 設定檔說明

- `config.json` 含明碼帳密，**已列入 `.gitignore`**，切勿提交；以檔案系統 ACL 限制存取。範例見 [config.example.json](config.example.json)。
- 優先序：`config.json` > 環境變數（`SNAPMAN_*`）> `.env` > 內建預設。
- 密碼欄位留空 = 不變更既有值。
- `SNAPMAN_ALERT_SUBJECT_TAG`（僅環境變數，不入 UI）：告警主旨前綴，測試環境設 `[測試]`。

### disk 任務的目標 VM 前置需求

- 安裝 SQL Server（**版本 ≥ 來源**，附掛低版本檔案無法降級）與 sqlcmd 工具。
- Guest 帳號在目標 SQL 具 sysadmin（Windows 驗證）。
- 具名執行個體：來源/目標可各自設定（例：趨勢 Apex One 的 `OFFICESCAN`），sqlcmd 自動帶 `-S .\{執行個體}`；只接受英數 / 底線 / `$`（不接受 `HOST\INST`）。

---

## 資料與安全

### 資料存放

| 資料 | 位置 | 說明 |
|---|---|---|
| 設定（含 Guest / AD / SMTP 明碼帳密） | `config.json` | 設定頁維護、原子寫入；**備份時視為機密** |
| 任務、執行紀錄、副本記帳、稽核、分權表、vCenter 帳密 | `data/snapman.db`（SQLite） | vCenter 密碼同為明碼 |
| 初始 admin 密碼 | `data/initial_admin_password.txt`（0600） | 首次改密後自動刪除 |
| Session 簽章金鑰 | `config.json` 的 `session_secret` | 首次啟動自動產生；換掉會讓所有登入失效 |
| disk 副本 / 歸檔 | 目標 datastore `snapman-clones/`、歸檔 datastore `snapman-archive/` | 檔名 `snapman-{來源VM}-data-{時間戳}.vmdk` |
| vmsync 複本 | 目的 vCenter | 帶 `snapman.owner=profile:<id>` extraConfig 標記 |

**備份 / 還原**：停服務後複製 `config.json` 與 `data/` 即為完整狀態；還原到新主機後，datastore 上的副本檔案由記帳表對帳認領。

### 保留與清理

- **執行紀錄**：`run_retention_days`（預設 90）天，每日自動清 Run / StepLog。
- **disk 副本**：超過「保留副本數」的舊副本每日刪除；刪檔失敗**保留紀錄**下輪重試，「檔案已不存在」才移除紀錄；另對帳 datastore 實際檔案清孤兒。歸檔修剪同語意。
- **vmsync 複本**：只留最新一份；殘留的暫存 / 殼 VM 由下一輪依名稱前綴認領清除。
- 清理機制皆為保守判斷，仍應定期巡檢 datastore 容量（容量頁）。

### 安全設計要點

- **不變量**：任一步失敗**絕不會動到來源 VM 的資料本體**；DETACH 失敗即中止、不拔碟。
- 靜默快照對繁忙 DB 有 **VSS freeze/thaw 停頓**與快照 consolidation 的 **VM stun** 風險；⑤立即移除快照以縮短窗口。
- 進 guest 命令的欄位（DB 名、碟號、SQL 執行個體名）於表單入口白名單驗證、組命令時第二道檢查；同一座 vCenter 時目標 VM 不得等於來源 VM。
- vmsync 換手只覆蓋帶本任務所有權標記的複本，同名無標記的 VM / 範本一律不動；複本開機中絕不覆蓋。
- Web：RBAC 伺服器端強制、每請求重算角色；非 GET 同站檢查（CSRF）；安全回應標頭（CSP self + inline、X-Frame-Options、nosniff、Referrer-Policy same-origin）；登入速率限制；登入失敗訊息一般化、細節與來源 IP 進稽核；CSV 匯出公式注入防護。
- 所有前端資源落地專案內，零外部 CDN；相依以 lock 檔鎖定。
- **接受風險**：網頁 HTTP（前置反向代理可補 TLS）、憑證明碼存於 `config.json` / SQLite（靠檔案 ACL）。2026-09-17 OWASP Top 10 / ASVS L1 評估 26 項已修 24 項，餘兩項即上述。

---

## 運維雜項

### 告警

- 失敗必發、成功可選（`alert_on_success`）；Webhook POST `{"text": ...}` 與/或 SMTP（STARTTLS、可驗憑證）。
- 演練 / 驗證排程完成另發演練報告；vmsync 複本被開機而拒絕覆蓋亦告警。
- **測試環境務必清空 smtp/webhook 設定**，或設 `SNAPMAN_ALERT_SUBJECT_TAG=[測試]`，避免測試 run 發真告警。

### 目標環境需求

| 項目 | 需求 |
|---|---|
| SnapMan 主機 | 可跑 Python 3 的 Windows / Linux；可連 vCenter 443；OVF 模式需直連兩側 ESXi 443 |
| 來源 VM（disk） | Windows + MSSQL、VMware Tools 正常、SQL Server VSS Writer 就緒、開機中 |
| 目標 VM（disk） | Windows + SQL Server（≥ 來源版本）+ sqlcmd、VMware Tools 正常、開機中、有空 SCSI unit |
| vmsync 來源 | VM 或範本；開機中需 Tools（VSS 靜默），關機不需 |
| vmsync XVM | 兩側主機 / vCenter 版本 + build 相容（儲存任務時預檢） |
| vmsync OVF | VM 無 vTPM / 加密；硬體版本 ≤ 目的端主機支援 |
| vCenter 權限 | 快照、clone、磁碟 hot-add / 移除、datastore 瀏覽與刪檔、Guest Operations、（vmsync）Relocate / OVF 匯出匯入 / rename / mark as template |

### 上線檢查清單

1. **變更 admin 初始密碼**（首次登入強制；preflight 對初始 / 不合規密碼會警告）。
2. 填妥 vCenter / Guest 帳密，建任務（用下拉選 VM/VMDK 避免打錯）。
3. 跑 `scripts/preflight.py` 至全部 ✅。
4. **先在測試環境完整跑一輪**：disk 任務重點確認⑧的磁碟 online / 簽章重寫與 ATTACH 結果；vmsync 任務重點確認網卡重接與換手。
5. 測試時清空告警設定或設主旨標籤（見上）。
6. 啟用 AD 登入與群組授權；確認防火牆對 8070 的放行範圍。
7. 為 `snapman-clones/`、`snapman-archive/` 與 vmsync 暫存規劃 datastore 容量。
8. 服務開機自啟（工作排程器 / NSSM）——目前需自行設定。

### 移除 / 還原

- **移除**：停服務、刪專案目錄（含 `.venv`、`data/`、`config.json`）。datastore 上的 `snapman-clones/`、`snapman-archive/` 與目的端 vmsync 複本**不會自動清**，請先於副本頁刪除副本 / 於 vCenter 手動處理，並 DETACH 目標 VM 上仍掛著的副本 DB。
- **還原**：新主機依「安裝與啟動」重建 `.venv`，放回備份的 `config.json` 與 `data/`，啟動即可；`session_secret` 隨 `config.json` 還原則既有登入 cookie 仍有效。
