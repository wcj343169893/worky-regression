"""看板資料層：查 worky DB 組出任務清單 / 詳情 / 統計。

純讀取。按業務模塊拆分（對應前端頂部主菜單）：
  - base      連線、白名單篩選、labor/employer/shop 名稱解析
  - contract  承攬制任務看板（任務清單 / 詳情 / 統計）
  - jobs      工作系統看板（工作清單 / 詳情 / 統計）
  - manage    打工夥伴 / 商家 / 店鋪管理清單
  - settings  系統設置（唯讀）

`DashboardService` 把各 mixin 組合起來，對外 API 與拆分前完全一致。
"""
from __future__ import annotations

from ...config import Settings
from .accounts import AccountsMixin
from .backend import BackendMixin
from .base import ServiceBase
from .contract import ContractMixin
from .jobs import JobMixin
from .manage import ManageMixin
from .settings import SettingsMixin


class DashboardService(ContractMixin, JobMixin, ManageMixin, SettingsMixin,
                       BackendMixin, AccountsMixin, ServiceBase):
    """組合各業務 mixin；建構與連線邏輯在 ServiceBase。"""


__all__ = ["DashboardService", "Settings"]
