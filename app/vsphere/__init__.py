"""vSphere 抽象層。

get_client(vc) 回傳 pyVmomi 實作（RealVSphere），依 VCenter 紀錄建立連線；
不帶參數時退回全域設定（相容 preflight 等舊用法）。
"""
from __future__ import annotations

from .base import VMInfo, VSphereClient, VSphereError


def get_client(vc=None) -> VSphereClient:
    from .real import RealVSphere
    if vc is None:
        return RealVSphere()
    return RealVSphere(host=vc.host, user=vc.user,
                       password=vc.password, insecure=vc.insecure)


__all__ = ["get_client", "VSphereClient", "VMInfo", "VSphereError"]
