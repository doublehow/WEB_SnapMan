# SnapMan — Snapshot Manager

自建的精簡版 SnapCenter / SnapManager，不綁特定儲存品牌。核心目標：

> 對**開機中**的來源 VM（MSSQL）拍一個**應用一致（application-consistent）的靜默快照**，
> 從一致點 **clone** 出 SQL 資料 VMDK，再**熱掛**給**開機中**的目標 VM，
> 讓目標每天拿到一份可用副本做 offload 運算。

開發指引與專案歷程見 [CLAUDE.md](CLAUDE.md)。

---

## 功能總覽

### 兩種任務類型

**1. 資料碟副本（disk）— 每日工作流 ①~⑩**

| # | 步驟 | 說明 |
|---|------|------|
| ① | 前置檢查 | 來源/目標開機、VMware Tools 正常、SQL Server VSS Writer 就緒 |
| ② | SQL CHECKPOINT | 對來源 DB 刷髒頁 |
| ③ | 靜默快照 | `CreateSnapshot(quiesce=True)` → VMware Tools 觸發 SQL VSS Writer，取**應用一致點** |
| ④ | Clone 資料 VMDK | 從一致點 clone 出一顆**獨立**碟（快照不能直接掛別台，必須 clone） |
| ⑤ | 立即移除快照 | 縮短 production stun 風險 |
| ⑤+ | 跨 VC 搬遷 | 僅 xvc 傳輸模式：暫存 clone 以殼 VM + Cross-vCenter Relocate 搬到目標側 |
| ⑥ | 卸載上一輪 clone | SINGLE_USER 斷開殘留連線 → DETACH 舊 DB → 磁碟 offline → 移除舊碟（檔案保留，交給保留策略） |
| ⑦ | 熱掛新 clone | hot-add 到開機中的目標 VM |
| ⑧ | 上線 + ATTACH | 磁碟 online、自動指派碟號、依來源 `sys.master_files` 權威路徑 `ATTACH` |
| ⑨ | 驗證 | `DBCC CHECKDB`（PHYSICAL_ONLY） |
| ⑩ | 收尾 | 更新狀態與 clone 紀錄 |

- **DB 清單留空 = 檔案磁碟分享模式**：跳過所有 SQL 步驟，快照仍走 VSS 靜默
  （檔案系統一致），目標端只掛碟 + 指派碟號。
- **跨 vCenter**：任務可分綁來源/目標 vCenter。傳輸模式二選一——
  `shared`（clone 直接寫入共享 NFS，前提：**同一個** NFS export 同名掛給兩座
  VC 的主機；前置檢查會比對 volume URL，兩地各一台「同名」NAS 不算共享）或
  `xvc`（免共享儲存，殼 VM + Cross-vCenter Relocate 搬遷）。

**2. 整機同步（vmsync）— 工作流 ①~⑦**

定時把指定 VM（**開機中亦可**，VSS 靜默）完整複製到另一座 vCenter，作為 DR standby：

> 前置檢查 → 靜默快照 → 從一致點 clone 完整暫存 VM → 移除快照 →
> Cross-vCenter Relocate（網卡自動重接指定目標 portgroup）→ 目標端換手 → 收尾

- 目標端**只保留最新一份**複本（新複本到位成功才刪上一份），保持**關機** standby。
- **覆蓋保護**：複本若被開機（可能正被 DR 使用），絕不覆蓋、run 失敗並告警。
- 關機中的來源 VM 也可同步（不需 Tools）；同一座 vCenter 亦支援（略過搬遷）。
- **範本對範本**：來源若是 VM 範本（vSphere 不允許對範本拍快照），②自動略過快照、
  直接以範本現行狀態 clone，複本到目的端定名後**自動標記為範本**；標記失敗時複本
  仍完整可用（只差型別），run 報錯提示手動 Mark as Template。
- 跨 VC 傳輸方式二選一：
  - **XVM 搬遷**（Cross-vCenter vMotion，舊稱 XVC；設定值仍為 `xvc`）：由來源側主機發起，其 ESXi 版本
    **與 build** 不得比目的端 vCenter 新，目的端落點主機亦不得比來源 vCenter 新（系統兩側自動挑相容主機）。
    儲存任務時會做 **XVM 相容性預檢**（不通過仍儲存但顯示警告），「檢視狀態」也會列出預檢結果與兩側挑到的主機。
    **不得高於目的端 vCenter**（系統自動挑符合上限的主機）。
  - **OVF 串流**：來源主機匯出 → 經 SnapMan 記憶體中轉（不落地、免準備
    空間）→ 目的主機匯入，**免主機版本相容、免共享儲存**；目的端 thin
    佈建。以「涓流保留」機制對付 ESXi 匯出流在接近結尾處的數分鐘停滯
    （上傳端保留尾段 64KB，停滯時涓流送出維持連線，不會被目的端以閒置
    切斷）。匯入完成後**自動補複製 .nvram**（OVF 本身不帶 NVRAM，EFI
    複本沒有它會因無開機項目而開不了機）。限制：VM 硬體版本仍須目的端
    主機支援、vTPM/加密 VM 不可匯出；需 SnapMan 可直連兩側 ESXi 的 443。
    傳輸中斷即整趟重來（無斷點續傳，失敗回滾後由下次排程重跑）。
- **容量與頻率的定位**（2026-09-21 決策）：兩種任務每輪都是**全量複製**（thin 碟只傳已配置區塊，
  thick 碟則整顆傳），未實作 CBT 增量。大容量 VM 的整機 DR 增量同步請交給 商業 VM 複寫軟體（vSphere Replication 或既有備份產品）；
  SnapMan 的 vmsync 定位為**範本、小型 VM、低頻（週）全量同步**，disk 任務則是它獨有的
  SQL 資料碟 offload 流程，不在商業備份軟體的範圍。
- 每次為**全量複製**（非增量），傳輸時間與 VM 大小成正比；來源側需暫存整台 VM 的空間。

### 排程

- 任務清單最左的「啟用 / 停用」即**排程啟用狀態**：啟用 = 依排程自動執行，停用 = 僅手動執行（於「編輯 → 排程」切換）。

每個任務有各自獨立的排程，兩種模式：

- **每日**：每天於指定時間（顯示時區、24 小時制）執行；catch-up 語意——
  因併發上限或服務重啟錯過時，當日內自動補跑。
- **一次性**：指定日期＋時間執行一次，成功起跑後**自動停用排程**；
  過期未跑（如服務停機）會補跑。

另有**任務串接**（本任務成功後自動觸發下一個任務，含循環偵測）與
**全域同時執行上限**＋**同 VM 互斥**（共用同一台來源/目標 VM 的任務自動排隊）。

### 副本管理（disk 任務）

- **多時間點保留**：「保留副本數」（含掛載中一份），超額舊副本每日自動刪除；
  刪檔失敗保留紀錄下輪重試，並對帳 datastore 實際檔案清孤兒。
- **任選掛載**：「🗂 副本」頁可把任一歷史副本掛回目標（卸下目前 → 掛上選定 →
  ATTACH + DBCC 的五步工作流）。
- **副本驗證 / DR 演練**：排程或手動「🧪 演練」——掛最舊保留副本跑 DBCC
  （＋選填自訂驗證 SQL），驗畢自動換回原掛載，發送演練報告。
- **副本歸檔**：每日成功後把當日副本再複製一份到歸檔 datastore，依保留份數修剪。
- 搭配 datastore 端去重（如 NAS 端的 Btrfs 去重）可壓低多份副本的空間成本。

### 監控與營運

- **儀表板**：今日成功/失敗、30 天趨勢圖（成功/失敗堆疊、耗時折線）、近期執行。
- **即時進度**：Run 詳情頁 WebSocket 串流每步狀態與 clone 進度百分比；可安全停止
  （步驟邊界生效；clone 中會取消 vSphere 工作並回滾）。
- **工作階段**：近百筆執行紀錄 + CSV 匯出。
- **行事曆**：固定 6 週月曆網格，每筆執行一個彩色 chip（時間＋任務名，
  依狀態上色、hover 看詳情、點入工作階段）；未來日期以虛線 chip 預告排程
  （每日排程逐日、一次性在其指定日期）。
- **報表**：各任務成功率、平均耗時、各步驟平均耗時。
- **容量**：datastore 用量與 SnapMan 副本檔案清單。
- **告警**：失敗必發、成功可選——Webhook（POST `{"text": ...}`）與/或 SMTP 郵件。
- **稽核**：登入/執行/停止/設定/副本操作全記錄（/logs），
  頁籤分「操作稽核」（人為操作）與「系統事件」（排程觸發、保留策略、串接）。
- **執行紀錄保留**：設定天數，每日自動清過期 Run/StepLog。

### 登入與帳號分權（RBAC）

- 本機管理員 **admin**（緊急備援；首次啟動自動產生初始密碼寫入 `data/initial_admin_password.txt`，
  登入後**強制變更**（≥12 字元、不得為 admin），忘記可用 `python -m app.set_admin_password` 重設 / `--disable` 停用）＋
  可選 **AD（NTLM）** 網域登入（Service Account 搜尋 → 使用者 bind 驗密 → 群組授權）。
- AD 群組只控制「誰能登入」；登入後權限由設定頁「帳號分權」表決定：
  **Full Admin**（全功能）/ **Admin ReadOnly**（全頁面唯讀）/
  **Audit**（僅儀表板、工作階段、行事曆、報表、記錄）。
  授權於伺服器端 middleware 強制、**每請求重查分權表**（10 秒 TTL 快取，降權 / 移除
  帳號最晚 10 秒生效）；WebSocket 同受保護。未指派角色的帳號預設 **Admin ReadOnly**。
- 登入失敗速率限制：同 IP＋帳號 5 分鐘 5 次、同 IP 不分帳號 5 分鐘 20 次（防密碼噴灑）；
  登入失敗訊息一律「帳號或密碼錯誤」，真實原因只寫稽核紀錄。
- AD 連線可選 **LDAPS（636）** 與憑證驗證（設定頁勾選；預設 389 以相容既有環境）。
- 全站 CSRF 同站檢查（Sec-Fetch-Site / Origin）、安全回應標頭（CSP、X-Frame-Options、
  nosniff、Referrer-Policy）；稽核紀錄含來源 IP。

### 介面

Veeam 式側欄主控台（Jinja2 + HTMX，無前端建置流程）；RWD 響應式（窄螢幕側欄收合
為抽屜）；暗黑模式（日/夜切換、記憶偏好、跟隨系統）。VM / VMDK / datastore /
portgroup 皆為下拉選單自動載入（啟動抓一次、背景每 30 分鐘刷新），免手打。

---

## 技術棧

- **後端**：Python + FastAPI；**vSphere 自動化**：pyVmomi
- **VM 內操作**：VMware Tools Guest Operations（不需打通到 guest 的網路；
  以 exit code 判斷成敗，並取回輸出供錯誤診斷）
- **資料**：SQLAlchemy + SQLite（`data/snapman.db`）
- **前端**：Jinja2 + HTMX（自帶 `app/web/static/htmx.min.js`，零外部 CDN）+ WebSocket（即時進度）
- **登入**：ldap3（AD NTLM，可選 LDAPS）+ Session；RBAC middleware
- **相依鎖定**：`requirements.txt` 為下限、`requirements.lock.txt` 精確鎖定（部署裝 lock）

---

## 安裝與啟動

```powershell
# 1. 建立虛擬環境並安裝相依（部署一律裝 lock 檔；升級時改 requirements.txt 下限 → 測試 → 重產 lock）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt

# 2. 啟動（綁定 0.0.0.0:8070）
.\.venv\Scripts\python.exe -m app.main
# 開發熱重載：
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8070 --reload

# 3. 上線前檢查（唯讀，不動任何 VM）
.\.venv\Scripts\python.exe scripts\preflight.py
```

> 改程式或模板後須重啟服務（手動啟動模式）。

## 使用方式

1. **登入**：瀏覽器開 `http://<主機>:8070`，以 admin 登入（初始密碼在 `data/initial_admin_password.txt`，
   首次登入會被導到設定頁強制變更，變更後該檔自動刪除）。
2. **設定**（右上角「設定」→ 寫入 `config.json`，即時套用不需重啟）：
   - 新增 vCenter（可多座）：主機 / 帳號 / 密碼 / 是否略過 TLS 驗證
   - Guest 帳密（Guest Operations 用；目標 SQL 需該帳號 sysadmin）
   - 變更 admin 密碼、啟用 AD 登入、帳號分權
   - 告警（Webhook / SMTP，含測試按鈕）、顯示時區、執行紀錄保留、併發上限
3. **建任務**（「任務」頁）：選任務類型後表單自動切換對應欄位；
   VM / 磁碟 / datastore / 網路都從下拉選。
4. **排程**：勾「啟用排程」，選每日或一次性；或隨時按「執行」手動執行。
5. **監控**：儀表板看總況、Run 詳情頁看即時進度、行事曆/報表看歷史。

### 設定檔說明

- `config.json` 含明碼帳密，**已列入 `.gitignore`**，切勿提交；
  請以檔案系統 ACL 限制存取。範例見 [config.example.json](config.example.json)。
- 優先序：`config.json` > 環境變數（`SNAPMAN_*`）> `.env` > 內建預設。
- 密碼欄位留空 = 不變更既有值。

### disk 任務的目標 VM 前置需求

- 安裝 SQL Server（**版本 ≥ 來源**，附掛低版本檔案無法降級）與 sqlcmd 工具。
- Guest 帳號在目標 SQL 具 sysadmin（Windows 驗證）。
- 具名執行個體：來源/目標可各自設定（例：趨勢 Apex One 的 `OFFICESCAN`），
  sqlcmd 自動帶 `-S .\{執行個體}`。

---

## 專案結構

```
app/
  config.py            # 設定（config.json / env / .env）＋ session secret
  database.py          # SQLAlchemy 引擎 + 輕量 schema 遷移
  models.py            # VCenter / Profile / Run / StepLog / CloneCopy / AuditLog / AccountRole
  auth.py              # AD（NTLM/LDAPS）驗證（含 MD4 相容修補、LDAP 逃逸）＋ 本機 admin 密碼生命週期
  set_admin_password.py # CLI：互動重設 / 停用本機 admin 密碼
  notify.py            # Webhook（URL 驗證）/ SMTP（STARTTLS 憑證驗證）告警
  timeutil.py          # 顯示時區轉換
  vsphere/
    base.py            # VSphereClient 介面
    real.py            # pyVmomi 實作（快照/clone/掛卸碟/XVM 搬遷/整機同步）
  workflow/
    steps.py           # STEPS（disk ①~⑩）/ MOUNT_STEPS（掛載五步）/ VMSYNC_STEPS（整機同步七步）
    manager.py         # RunManager：執行、持久化、進度廣播、回滾、保留策略、告警
  web/templates/       # HTMX 管理介面（儀表板/任務/工作階段/行事曆/報表/容量/記錄/設定）
  web/static/          # 自帶前端資源（htmx.min.js）
  main.py              # FastAPI 進入點（路由、RBAC/CSRF middleware、安全標頭、排程器、WebSocket、快取）
scripts/preflight.py   # 上線前唯讀檢查
requirements.txt       # 相依下限
requirements.lock.txt  # 部署用精確鎖定（含間接相依）
data/                  # 執行期產物（gitignore）：snapman.db、initial_admin_password.txt
config.json            # 執行期設定（gitignore，設定頁維護）
```

---

## 上線檢查清單

1. **變更 admin 初始密碼**（首次登入強制；preflight 對初始 / 不合規密碼會警告）。
2. 填妥 vCenter / Guest 帳密，建任務（用下拉選 VM/VMDK 避免打錯）。
3. 跑 `scripts/preflight.py` 至全部 ✅。
4. **先在測試環境完整跑一輪**：disk 任務重點確認步驟⑧的磁碟 online /
   簽章重寫與 ATTACH 結果；vmsync 任務重點確認網卡重接與換手。
5. **測試時清空 smtp/webhook 告警設定**，或設環境變數
   `SNAPMAN_ALERT_SUBJECT_TAG=[測試]` 讓告警主旨帶標籤，避免測試 run 發出真告警。
6. 啟用 AD 登入與群組授權；確認防火牆對 8070 的放行範圍。
7. 為 clone 目錄（datastore 上的 `snapman-clones/`、`snapman-archive/`）規劃容量。
8. 服務開機自啟（工作排程器 / NSSM）——目前需自行設定。

## 安全提醒

這個工具會操作 production SQL 與 vSphere：

- 靜默快照對繁忙 DB 有 **VSS freeze/thaw 停頓**與快照 consolidation 的 **VM stun** 風險。
- 每輪對目標 VM 反覆 attach/detach + SQL ATTACH/DETACH，DB 生命週期與碟號要嚴謹
  （DETACH 失敗會中止流程、不拔碟，即為此設計）。
- clone / 快照有孤兒清理機制，仍應定期巡檢 datastore 容量。
- 任一步失敗**絕不會動到來源 VM 的資料本體**（設計不變量）。為守住這點：SQL 執行個體名
  只接受英數 / 底線 / `$`（不接受 `HOST\INST`，一律指向該 VM 本機）；同一座 vCenter 時目標 VM
  不得等於來源 VM。
- vmsync 複本帶 `snapman.owner` 所有權標記（extraConfig），換手只覆蓋本任務建立的複本；
  同名但無標記的 VM / 範本一律不動，新複本保留為暫存名待人工處理。
- 網頁本體為 HTTP（接受風險，內網工具）；若要 TLS 請前置反向代理。
- 憑證（vCenter / guest / AD / SMTP）以明碼存於 `config.json` 與 `data/snapman.db`（接受風險），
  以檔案 ACL 保護，備份時一併視為機密。
- 2026-09-17 安全評估（OWASP Top 10 / ASVS L1）26 項發現已修 24 項；M3（HTTP）與明碼憑證為接受風險。
