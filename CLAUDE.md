# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概要

SnapMan:自建精簡版 SnapCenter,不綁儲存品牌。兩種任務類型:

- **disk(資料碟副本)**:對開機中的來源 VM(MSSQL)拍應用一致靜默快照 → 從一致點 clone 出獨立資料 VMDK → 熱掛給開機中的目標 VM 並 SQL ATTACH + DBCC 驗證,讓目標每天拿到可用副本做 offload 運算。
- **vmsync(整機同步)**:定時把指定 VM(開機中亦可)完整複製到另一座 vCenter 作 DR standby,目標端只留最新一份、保持關機,複本被開機時有覆蓋保護。

功能介紹與使用方式見 [README.md](README.md)。

全專案(程式註解、UI、commit 訊息)使用**繁體中文**。

## 常用指令

```powershell
# 安裝相依(虛擬環境在 .venv;部署裝 lock 檔,升級才動 requirements.txt 並重產 lock)
.\.venv\Scripts\python.exe -m pip install -r requirements.lock.txt

# 啟動(綁 0.0.0.0:8070)
.\.venv\Scripts\python.exe -m app.main
# 開發熱重載
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8070 --reload

# 上線前唯讀檢查(不動任何 VM)
.\.venv\Scripts\python.exe scripts\preflight.py
```

沒有自動化測試套件、沒有 lint 設定;驗證方式是 preflight 腳本 + 在測試環境實跑工作流(web 層改動可用 TestClient 在專案外的 sandbox 複本做功能測試,勿在專案目錄直接跑——會寫 config.json / data/)。改程式/模板後**必須重啟服務**(新模板配舊程式會 500)。

**⚠ --reload 模式下改檔 = 重啟**:服務若以 `uvicorn --reload` 跑著,任何對 app/ 檔案的編輯都會觸發熱重載、**殺掉執行中的 run**(Run #52 實測:OVF 串流中被 reload 中斷,關閉還被串流執行緒卡 300 秒)。正式執行一律用不帶 --reload 的指令;**有 run 在跑時絕不編輯 app/ 下的檔案**,除非確認服務非 reload 模式。

## 架構

### 分層

- [app/main.py](app/main.py) — 唯一的 FastAPI 進入點:所有路由、登入/RBAC middleware、登入速率限制、WebSocket 進度串流、vCenter 清單快取(啟動抓一次、背景每 30 分鐘刷新)、排程器(每日/一次性/驗證排程,皆 catch-up 補跑)、保留策略/歸檔觸發、任務表單白名單驗證與串接循環偵測。
- [app/workflow/steps.py](app/workflow/steps.py) — 步驟定義。`STEPS`(disk ①~⑩)、`MOUNT_STEPS`(掛載歷史副本五步)、`VMSYNC_STEPS`(整機同步七步:快照 → clone 完整 VM → 跨 VC 搬遷 → 目標端換手)。每步是 async 函式,收 `StepContext`、回傳訊息字串或拋例外代表失敗。Profile 依 `job_type` 決定走哪組;vmsync 的步驟 key(clone_vm/vs_*)刻意與 disk 不重疊,避免誤觸 manager 的副本記帳 hook。
- [app/workflow/manager.py](app/workflow/manager.py) — `RunManager` 單例:啟動/停止 run、逐步持久化 StepLog、WebSocket 事件廣播、失敗回滾、副本記帳(CloneCopy)與保留策略、結果告警。
- [app/vsphere/base.py](app/vsphere/base.py) — `VSphereClient` Protocol;[real.py](app/vsphere/real.py) 是 pyVmomi 實作。所有方法同步阻塞,工作流以 `asyncio.to_thread` 包裝呼叫。
- [app/models.py](app/models.py) — SQLAlchemy 模型:`VCenter` / `Profile` / `Run` / `StepLog` / `CloneCopy` / `AuditLog` / `AccountRole`。SQLite 存 `data/snapman.db`;[database.py](app/database.py) 的 `_migrate()` 做輕量 ALTER 遷移(新欄位務必補進 `new_columns`)。
- [app/web/templates/](app/web/templates/) — Jinja2 + HTMX,Veeam 式側欄主控台;無前端建置流程。

### 併發模型(manager.py 核心不變量)

- 同一 Profile 同時只能有一個 run;全域同時執行數受 `max_concurrent_runs` 限制。
- **同 VM 互斥**:`_vm_locks` 以 `(vCenter host, VM 名)` 為 key,共用來源/目標 VM 的任務自動排隊(掛碟 SCSI unit 與 guest 內磁碟腳本會互踩);vmsync 的目標端 key 用複本最終名。
- 「檢查+登記」在同一個 event-loop tick 內完成(中間無 await),以此避免 race——修改啟動邏輯時不可在檢查與登記之間插入 await。
- 停止:`request_stop()` 只接受 active run、於步驟邊界生效;clone 進行中另會取消 vSphere 工作,之後走回滾。
- manager 內同步 SQLite 直接跑在 event loop 上是刻意取捨;若改網路型 DB 須改包 `asyncio.to_thread`。

### 跨 vCenter

Profile 可分綁來源/目標 vCenter,工作流雙 client(disk:①~⑤ 用來源、⑥~⑨ 用目標)。disk 任務兩種 `transfer_mode`:

- `shared`:clone 直接寫入共享 NFS(前提:datastore 同時掛給兩座 vCenter 的主機且**同名**)。
- `xvc`:clone 先落來源側暫存 datastore,再以 Cross-vCenter Relocate(殼 VM + ServiceLocator)搬到目標側。

vmsync 跨 VC 傳輸方式二選一(`transfer_mode`:xvc / ovf;表單欄位名 `vmsync_transfer`,與 disk 的 shared/xvc 同欄位不同值域):

- `xvc`(UI 正名 **XVM**,Cross-vCenter vMotion):RelocateVM + ServiceLocator,網卡重對應在 RelocateSpec 的 deviceChange 完成;**兩側主機都要讓對端 vCenter 認得**——版本較低可、同版本 build 不得高於該 vCenter(`_host_ok_for_vc`,見 Run #119),儲存任務時 `_xvm_check` 預檢並於「檢視狀態」顯示。
- `ovf`:OVF/HTTP 串流(real.py `ovf_stream_vm`)——ExportVm lease 逐碟 GET,管線化餵給 ImportVApp lease 的 chunked POST,經 SnapMan 記憶體中轉不落地(有界佇列 ≤64MB)。免主機版本相容、免共享儲存。**涓流保留**:上傳恆保留尾段 64KB,來源停滯時每 15 秒滴 1 byte 維持連線(Run #54 根因的解法,勿移除);下載端逾時 600 秒容忍停滯。networkMapping 處理網卡;keepalive 執行緒對兩端 lease 回報進度、每 3 分鐘發進度/速率報告;失敗 abort 兩端 lease(目的端半成品自動清除)。**NVRAM 補複製**(`_copy_nvram`):OVF lease 只傳磁碟,.nvram 不隨行,EFI 複本會因無開機項目開不了機(JOB03 實案)——匯入完成後以 vCenter `/folder` API(帶 SOAP session cookie,多 DC 必帶 dcPath)GET 來源 .nvram、PUT 到目的 VM 資料夾;暫存複本無 nvram 時退回原始來源 VM;EFI 複製失敗即步驟失敗(目的端半成品由下輪前綴認領清理),BIOS 僅記警告。限制:硬體版本仍須目的端支援、vTPM/加密不可匯出(precheck 先擋)。

同 VC 時略過傳輸、改 ReconfigVM 重接網卡。(曾短暫有 shared 交接模式,已依使用者決策移除——見歷程。)

### Guest 內操作慣例(steps.py)

- Guest Operations 以 **exit code** 判斷成敗;各失敗原因用不同 exit code 區分(如 4=檔案不存在、6=ATTACH 失敗、7=碟號被占、8=無 sqlcmd)。
- `sqlcmd` 一律帶 `-b`(SQL 錯誤才會反映到 exit code),具名執行個體自動帶 `-S .\{instance}`。
- 程式路徑用絕對路徑(`StartProgramInGuest` 不保證解析 PATH);PowerShell 腳本經 `_ps_encoded()` 走 `-EncodedCommand` 避免引號逃逸。
- guest 逾時依步驟性質指定(`_T_SQL`=1h、`_T_DBCC`=6h,預設 600 秒);DB 名/碟號在表單入口以白名單驗證(main.py `_validate_profile_input`),組命令時仍維持 `]]` bracket 逃逸慣例。

### 設定

[app/config.py](app/config.py):優先序 `config.json` > 環境變數(`SNAPMAN_*`)> `.env` > 內建預設。`config.json` 由 Web 設定頁維護、含明碼帳密、已在 .gitignore,**切勿提交**。`save_settings()` 加鎖 + 原子寫入(暫存檔 + os.replace),就地更新記憶體單例,執行中的工作流立即讀到新值(不需重啟)。

### RBAC

LDAP 群組只控制「誰能登入」;登入後權限由 `account_roles` 表決定:`full_admin` / `admin_readonly` / `audit`。授權於 main.py 的 middleware **伺服器端強制**(模板隱藏按鈕只是輔助);內建 `admin` 恆為 full_admin(防鎖死)。
本機 admin 密碼生命週期(auth.py,參考 VCOD):`local_admin_password` 預設空,啟動時空且 AD 未啟用 → `bootstrap_local_admin()` 產生隨機初始密碼寫 `data/initial_admin_password.txt`(0600)並標 `local_admin_initial`;登入時 `local_admin_must_change()`(初始或不符 `admin_password_problem()`:≥12 字元、不得為 admin)→ session `must_change_pw`,middleware 只放行 `/settings` 與 `/logout`;設定頁改密經 `set_local_admin_password()` 清旗標並刪檔。CLI `python -m app.set_admin_password`(互動輸入;`--disable` 需 AD 已啟用)。密碼仍為明碼存 config.json(接受風險)。改動路由時注意 `_AUDIT_PATHS` 白名單;`/logout` 為 POST 且任何角色可執行。

## 安全鐵則

- 本工具操作 production SQL 與 vSphere:任一步失敗**絕不可動到來源 VM 的資料本體**;DETACH 失敗即中止、不拔碟。
- **測試時務必先清空 smtp/webhook 告警設定**,否則測試 run 會發出真告警;或設 `SNAPMAN_ALERT_SUBJECT_TAG` 讓主旨帶「[測試]」前綴(此欄位不入 UI/config.json)。
- 清理/刪除邏輯要防孤兒 clone 吃爆 datastore,也要防誤刪(參考 `_is_file_not_found` 的保守判斷);「刪檔失敗保留紀錄下輪重試、檔案確定不在才移除紀錄」是保留策略與歸檔修剪共同的語意。
- vmsync 換手:新複本到位成功才刪上一份;複本被開機絕不覆蓋;**只覆蓋帶 `snapman.owner=profile:<id>` 標記的舊複本**(⑥ 先對暫存複本 `set_vm_owner_tag`,再比對舊複本標記),無標記一律不動。`_find_vm` 遇同名多台直接拒絕。
- **任何會進 guest 命令字串的 Profile 欄位都要白名單**:DB 名 `^[\w\-. ]+$`、碟號單字母、SQL 執行個體名 `[A-Za-z0-9_$]{1,16}`(main.py 入口 + steps.py `_check_instance` 第二道);`_server()` 一律 `.\INST`,不接受 `HOST\INST`;同 VC 時 `target_vm != source_vm`。新增欄位若會組進命令,兩層都要補。
- SnapMan 產物認領一律 `_claimed(prefix, name)`(前綴 + 純時間戳 fullmatch),不用 startswith。
- web 層安全基線(main.py):角色每請求 `_role_for_session()` 重算(勿再從 cookie 讀 role);非 GET 經 `_cross_site()` 同站檢查(**Referrer-Policy 不可設 no-referrer**:會讓同站表單 POST 的 Origin 變 `null`,而純 HTTP 下 Chrome 不送 Sec-Fetch-Site,登出/儲存全被擋——UAT 實測;現為 same-origin,Origin `null`/缺席不擋);所有回應經 `_secured()` 加安全標頭(CSP 為 self + inline,新增外部資源請落地 `app/web/static/`,不引 CDN);登入失敗訊息對外一律一般化、細節進 `_audit`(含來源 IP);唯讀角色可見的例外用 `_brief_err()`。

## 專案歷程與解決細節

開發過程中踩過並解掉的坑,改相關程式前先讀這段,避免走回頭路:

- **2026-07-02 端到端驗證**:十步全綠含每日換碟循環。環境:來源 SQL 2022 具名執行個體、DB 位於 OS 碟、來源/目標同範本(磁碟簽章相同)、目標 SQL 2022 Express。
- **2026-07-02 快照後 clone 的 backing 選取**:拍完快照後 VM 現行 backing 是 delta(被鎖定),`CopyVirtualDisk` 必須取「快照當下 config 記錄的 backing」,攤平 parent 鏈成獨立碟(real.py `clone_data_disk`)。
- **2026-07-02 碟號指派(⑧)**:Windows mountmgr 在磁碟 online 後會**自動**配發碟號給所有分割區,「碟號存在」推不出「指對分割區」。解法:拿來源 `sys.master_files` 的相對路徑經 volume GUID 路徑逐一**探測分割區內容**(ACL 探不到時退探目錄/上層),命中者才是資料分割區——演繹不猜測。檔案模式改用⑦回傳的 SCSI unit 定位(Windows `SCSITargetId` = vSphere unit number,`SCSILogicalUnit` 恆 0)。
- **2026-07-02 DBCC 搶鎖(⑨)**:⑧到⑨之間使用者重連(SSMS)搶占會導致 5030 → 先 SINGLE_USER 踢人再驗、驗完恢復 MULTI_USER,同批次執行;DBCC 失敗時 DB 可能停留 SINGLE_USER(錯誤訊息有註明)。
- **2026-07-02 Run #7 教訓(過期指標)**:⑨失敗留下過期 last_clone_path,下一輪⑥清錯對象、兩顆 clone 並存。解法:記帳「即時」跟上實際狀態(⑥完成即清指標、⑦完成即記錄),不等 run 成功才更新。
- **2026-07-02 Guest 輸出雜訊**:PowerShell 重導 stderr 會混入 CLIXML 序列化雜訊(可能出現在開頭,不能截斷,要逐行過濾);中文 Windows 輸出常是 cp950,utf-8 失敗後退回。
- **2026-07-06 JOB01 教訓(保留策略)**:刪檔失敗即移除紀錄 → 檔案無人管,datastore 累積 9 份而紀錄剩 4 份。解法:刪檔失敗**保留紀錄**下輪重試、「檔案已不存在」才移除紀錄;另加 datastore 實際檔案對帳清孤兒。歸檔修剪 2026-07-30 亦改用同語意。
- **2026-07-06 svMotion 對帳(⑥)**:svMotion 會把掛載中的 clone 連 VM 搬走(路徑改變),只信紀錄會拔不到又留孤兒。解法:以檔名前綴 `snapman-{來源VM}-data-` 盤點目標 VM 實際掛載的碟,basename 對應紀錄路徑,紀錄外的本任務碟一併卸載。
- **2026-07-06 多 datacenter vCenter**:可帶 `datacenter` 參數的 API(如 `DeleteVirtualDisk`)**必須帶**,省略回「A specified parameter was not correct: dc」;從 datastore 路徑反查 DC。
- **2026-07-09 Run #34 教訓(xvc session 逾時)**:長時搬遷期間「另一座」vCenter 的 client 閒置被回收 session,後續呼叫拋 NotAuthenticated。解法:`_content()` 每次驗 session、死了自動重連;殼 VM 名帶來源 VM 前綴,殘留由下一輪認領清除。
- **2026-07-09 併發 race 修正**:早期用 `lock.locked()` 檢查、稍後才在背景 task 取鎖,存在 race;改為同 tick「檢查+登記」(見併發模型)。
- **2026-07-09 原生 time 選擇器**:彈窗跳動且顯示 12 小時制 → 排程時間改 24 小時制時/分下拉。
- **2026-07-30 code review 修正 14 項**(commit 789f009):歸檔孤兒、驗證排程補跑+稽核、WS stopped 回放、串接循環偵測、guest 逾時放寬、LDAP filter 逃逸+群組精確比對、表單白名單驗證、登入速率限制、config 原子寫入、CSV 公式注入防護等。明碼密碼存放為已知取捨(使用者決定不處理,靠檔案 ACL 兜底)。
- **2026-07-30 vmsync 整機同步**(commit f34a607):設計決策——只留最新一份、VSS 靜默、網卡對應指定 portgroup。當時尚未實測;後續 Run #49(XVC 被拒)、#118(OVF 範本對範本全綠)已驗證 clone / 網卡重接 / 換手 / MarkAsTemplate。
- **2026-07-30 一次性排程**(commit 652aceb):schedule_mode daily/once,once 起跑成功即停用、過期補跑。
- **2026-07-30 Run #49 教訓(版本三明治,vmsync)**:來源 vCenter(8.0.3,主機只有 6.7 與 8.0.3)→ 目的 vCenter(7.0.3):8.0.3 主機發起被拒、6.7 主機放不下 vmx-17 範本,**XVC 在此環境無解**。曾為此加過 shared 交接模式(unregister → register),因環境無真共享 datastore(見下條)依使用者決策移除;最終解法是 **OVF 串流傳輸方式**(不做跨 VC 操作,兩側各自本地匯出/匯入,徹底繞開主機型別檢查)。SnapMan 主機與 DR 同網段,資料路徑 HQ→(WAN)→SnapMan→(區網)→DR,WAN 只過一次;已驗證 SnapMan 直連兩側 ESXi 443 可通、範本無 vTPM/加密(vmx-17 ≤ 目的端上限 vmx-19)。
- **2026-07-30 xvc 主機挑選(兩個方向都會被拒)**:XVC 搬遷由來源側主機(殼 VM / 暫存複本所在)發起,目的端 vCenter 不認識「與自己版本不相容」的主機型別即拒絕(vCenter does not support hosts of this type)——6.x 主機被 7.x vCenter 拒過,**Run #49 實測 8.0.3 主機也被 7.0.3 vCenter 拒**。`pick_xvc_host` 現以「目的端 vCenter 版本」為上限、取符合上限中最高者;殼 VM 固定 vmx-13 避免硬體版本問題。
- **2026-07-30 同名 ≠ 共享(兩地各一台同型號 NAS)**:兩座 vCenter 各自掛著「同名」的 NFS datastore,實為兩地不同 NAS——**名稱相同推不出同一儲存**。disk 共享模式的前置檢查以 `datastore.summary.url`(volume UUID 由 NFS server+path 導出)比對驗證真共享,同名假共享在①就擋下。此環境 HQ↔DR **無真共享 datastore**。
- **2026-07-30 Run #51/#53/#54 教訓(OVF 直傳被「來源收尾停滯」殺死)**:三次 OVF 串流皆在資料傳完前後被切斷(10053/10054),依序排除了 vCenter proxy、防火牆、端點防護與備份軟體後,隔離下載測試找到根因——**ESXi 匯出流在接近結尾處會停滯近 5 分鐘**(處理稀疏區塊/收尾;範本 15.9 GB 的流在 15.1 GB 處停),管線化直傳讓上傳連線在停滯期間閒置而被目的端切斷。解法:**涓流保留**(上傳恆保留尾段 64KB,停滯時每 15 秒滴 1 byte 維持連線活性)——曾短暫改成兩階段落地本機暫存,因暫存空間無法事前準確估計(只能用磁碟容量上限當門檻)依使用者決策改回不落地串流。使用者的個人電腦 UI 匯出「失敗在最後」是同根因的旁證。
- **2026-08-03 OVF 複本開不了機(NVRAM 缺口)**:JOB03 複本 EFI 開機失敗,根因是 OVF lease 只傳磁碟、.nvram(EFI 開機項目)不隨行,複本開機時 ESXi 建空白 NVRAM、無 Windows Boot Manager 項目。實測比對來源/複本組態:其餘(硬體版本/SecureBoot/CPU/記憶體/reservation 等)皆完整保留,OVF 軌真正獨有缺口僅 NVRAM、thin 強制、vTPM 匯不出;MAC/UUID/genid 換新是「每輪產新 VM」的 vmsync 天性(兩軌皆同,clone 就換了)。解法:匯入後補複製 .nvram(見跨 vCenter 一節)。既有壞複本手動救法:進 EFI setup 加開機項目指到 `\EFI\Microsoft\Boot\bootmgfw.efi`。
- **2026-09-17 範本對範本(JOB03 Template_Ubuntu24)**:vmsync 來源選 VM 範本,②拍快照回 `The operation is not supported on the object`——vSphere 不允許對範本 CreateSnapshot。解法:`VMInfo.is_template`(取 `summary.config.template`),①記 `scratch["vs_src_template"]` 並跳過 Tools 檢查、②改走 `step_vs_snapshot` 略過、③ `clone_vm_from_snapshot(snapshot_id=None)` 以現行狀態 clone(範本無 resource pool,host/pool 必須明示)、⑥定名後 `mark_as_template`(複本先以一般 VM 傳輸,因範本不可 Relocate/OVF 匯出);標記失敗仍算步驟失敗但 `vs_done` 已設、不回滾銷毀複本。**Run #118 實測全綠**(2026-09-17,OVF 傳輸 20 分鐘):CloneVM_Task 對範本、OVF 匯入後 MarkAsTemplate 皆成功。
- **2026-09-17 安全評估修補(OWASP Top 10 / ASVS L1,26 項)**:報告在專案外(`SnapMan-SecurityAssessment-*.md`,不入版控)。High 三項——SQL 執行個體名未驗證可在來源 production VM 執行任意 cmd(`_sqlcmd` 把 instance 放在引號外)、`_server` 接受 `HOST\INST` 讓目標端 sqlcmd 打到來源、vmsync ⑥ 以名稱銷毀同名關機 VM——分別以白名單、一律本機、所有權標記解決。M1 預設密碼 admin/admin 改為 VCOD 式初始密碼 + 強制變更。使用者決策:M3(網頁 HTTP)保留為接受風險;M4 LDAPS、M6 SMTP 憑證驗證做成選項不強制(`ad_use_ssl`/`ad_ssl_verify`/`smtp_tls_verify`)。`default_role` 預設改 admin_readonly、`vcenter_insecure` 預設改 False——升級後未指派角色的 AD 帳號變唯讀、新增 vCenter 預設驗證憑證,既有資料不變。htmx 由 unpkg 改落地 `app/web/static/`(雙 CDN sha256 一致)。pyasn1 升 0.6.4(三個 DoS CVE)並補 requirements.lock.txt。
- **2026-09-18 Run #119 教訓(XVM 同版本不同 build 也被拒)**:HQ vCenter 8.0.3 build 24674346 → DR vCenter 8.0.3 build 25600417,⑤ 仍回 `vCenter does not support hosts of this type ( )`。實查兩側版本:DR 有 8.0.3 build 25595708 ×2 與 7.0.3 ×3 主機,`get_vmsync_dest_info` 挑「版本最高」落點 = 8.0.3/25595708,**比發起端(HQ)vCenter 的 build 新**,HQ vCenter 不認得。結論:XVM 兩端 vCenter 都必須認得對方那台主機——版本較低可、同版本 build 不得高於該 vCenter;兩個方向都要比。解法:`_host_ok_for_vc()` + `vcenter_release()`(版本+build),`pick_xvc_host(ds, max_version, max_build)` 兩側共用,目的端落點改以「發起端 vCenter」為上限(此環境會挑 7.0.3 主機;範本 vmx-14 可跑);disk 流程目標 VM 所在主機不相容則 precheck 直接報可讀錯誤。使用者要求正名 **XVM**(Cross-vCenter vMotion),UI/文件全改,設定值 `xvc` 與程式識別字不動。
- **2026-09-21 定位決策(不做 CBT 增量)**:使用者提出 3T 來源碟每輪全量複製太久、浪費頻寬,討論 CBT。結論:CBT 只回答「哪些區塊變了」,讀寫區塊內容需 VDDK(C 函式庫、授權/散布限制、要 proxy)或自建「`/folder` Range GET + guest raw write + `CreateChildDisk_Task` 子碟」的純 API 路線(可行性未驗證、工程量約再一倍);vmsync 的增量同步本質就是 vSphere Replication / 商業複寫產品在做的事。**決策:不改程式**——大容量 VM 的整機增量 DR 交給商業 VM 複寫軟體（vSphere Replication 或既有備份產品）;SnapMan vmsync 定位為範本 / 小 VM / 低頻全量,disk 任務(SQL 資料碟 offload)為獨有價值、維持現狀。若日後要做,先量 thin/thick 與日變更率(開 CBT 拍兩次快照 `QueryChangedDiskAreas` 即可算),再做 Phase 0 驗證 `/folder` Range GET 吞吐。

## 文件同步

新增/移除功能或改變使用方式時,同步更新 README.md(功能總覽/使用方式)與本檔(架構、歷程)。README 面向使用者(功能介紹、使用方式);本檔面向開發(架構、不變量、歷程與解決細節)。
