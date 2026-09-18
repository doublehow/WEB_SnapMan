"""上線前檢查（preflight）：驗證設定、各 vCenter 連線與各 Profile 的環境就緒度。

用法：.venv\\Scripts\\python.exe scripts\\preflight.py

檢查項目：
  1. 設定完整性（guest 憑證、admin 預設密碼警告、vCenter 清單）
  2. 各 vCenter 連線與 VM / datastore 列舉
  3. 每個 Profile（依其綁定的 vCenter）：來源/目標 VM 存在且開機、Tools 運作、
     來源資料碟存在、SQL Server VSS Writer 就緒（需 guest 憑證）

只做唯讀查詢與 guest 內的狀態檢查，不會建立快照、不會 clone、不會掛碟。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Profile, VCenter  # noqa: E402
from app.vsphere import get_client  # noqa: E402

_fail = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global _fail
    mark = "✅" if ok else "❌"
    if not ok:
        _fail += 1
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))


def main() -> int:
    init_db()

    print("== 1) 設定完整性 ==")
    check("Guest 密碼已設定", bool(settings.guest_password))
    from app.auth import admin_password_problem
    if settings.local_admin_initial:
        check("本機管理員密碼", False, "仍為系統產生的初始密碼，請以 admin 登入後變更")
    elif settings.local_admin_password and admin_password_problem(settings.local_admin_password):
        check("本機管理員密碼", False, admin_password_problem(settings.local_admin_password))
    else:
        check("本機管理員密碼", True, "已變更")

    with SessionLocal() as db:
        vcenters = db.query(VCenter).order_by(VCenter.id).all()
        profiles = db.query(Profile).order_by(Profile.id).all()
        db.expunge_all()
    check("vCenter 清單", bool(vcenters), f"共 {len(vcenters)} 座")

    for vc in vcenters:
        print(f"== 2) vCenter：{vc.name}（{vc.host}）==")
        if vc.insecure:
            print("  ⚠ 略過 TLS 憑證驗證（insecure）")
        client = get_client(vc)
        try:
            client.connect()
        except Exception as exc:
            check("連線", False, str(exc))
            continue
        check("連線", True)
        try:
            vms = {v.name: v for v in client.list_vms()}
            check("列舉 VM", True, f"共 {len(vms)} 台")
            ds = client.list_datastores()
            check("列舉 datastore", True, f"共 {len(ds)} 座")

            for p in profiles:
                if p.vcenter_id != vc.id:
                    continue
                print(f"  --- Profile：{p.name} ---")
                if getattr(p, "job_type", "disk") == "vmsync":
                    # 整機同步：只驗來源（開機中才需 Tools 靜默；關機可直接同步）；
                    # 目的 datastore / 網路於執行時的前置檢查驗證（目標 VC 端）
                    vm = vms.get(p.source_vm)
                    if vm is None:
                        check(f"來源 VM {p.source_vm}", False, "vCenter 上找不到")
                    elif vm.power_state == "poweredOn":
                        check(f"來源 VM {p.source_vm} Tools（VSS 靜默）", vm.tools_running)
                    else:
                        check(f"來源 VM {p.source_vm}", True, "關機中（同步不需 Tools）")
                    check("目的 datastore 已指定", bool(p.target_datastore))
                    check("目標網路已指定", bool(p.target_network))
                    continue
                for role, vm_name in (("來源", p.source_vm), ("目標", p.target_vm)):
                    vm = vms.get(vm_name)
                    if vm is None:
                        check(f"{role} VM {vm_name}", False, "vCenter 上找不到")
                        continue
                    check(f"{role} VM {vm_name} 開機", vm.power_state == "poweredOn", vm.power_state)
                    check(f"{role} VM {vm_name} Tools", vm.tools_running)
                if p.source_vm in vms:
                    try:
                        disks = client.list_disks(p.source_vm)
                        labels = [d.label for d in disks]
                        hit = p.source_data_disk in labels or any(
                            p.source_data_disk in d.file_name for d in disks
                        )
                        check(f"來源資料碟 {p.source_data_disk}", hit, f"現有：{', '.join(labels)}")
                    except Exception as exc:
                        check("列舉來源磁碟", False, str(exc))
                if p.target_datastore:
                    check(f"目標 datastore {p.target_datastore}", p.target_datastore in ds)
                if settings.guest_password and p.source_vm in vms:
                    try:
                        ok = client.check_sql_writer(
                            p.source_vm, settings.guest_user, settings.guest_password
                        )
                        check("SQL Server VSS Writer", ok)
                    except Exception as exc:
                        check("SQL Server VSS Writer", False, str(exc))
                check("DB 清單", bool(p.database_list),
                      ", ".join(p.database_list) or "未指定任何資料庫")
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    unbound = [p.name for p in profiles if p.vcenter_id is None]
    if unbound:
        check("任務 vCenter 綁定", False, f"未綁定：{', '.join(unbound)}")

    print()
    if _fail:
        print(f"結果：{_fail} 項未通過 — 請排除後再上線")
        return 1
    print("結果：全部通過 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
