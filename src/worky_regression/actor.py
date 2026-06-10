"""Actor：一個角色 = phone + user_id + 已登入的 client。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .client import WorkyClient, md5


class LoginFailedError(RuntimeError):
    pass


@dataclass
class Actor:
    role: str                # "publisher" / "receiver"
    user_type: int           # 1=employer, 2=labor
    phone: str
    user_id: int
    client: WorkyClient
    shop_id: int | None = None
    display_name: str = ""
    _logged_in: bool = field(default=False, init=False)

    @property
    def login_path(self) -> str:
        return "/employer/login/confirm" if self.user_type == 1 else "/labor/login/confirm"

    @property
    def login_send_path(self) -> str:
        return "/employer/login" if self.user_type == 1 else "/labor/login"

    def login(self) -> None:
        """真實登入流程（對所有帳號統一，不用固定碼）：
        先 POST /labor|/employer/login 發碼（測試環境 response 帶 data.code），
        再以 md5(code) 打 confirm。
        """
        resp = self.client.post(self.login_send_path, body={"phone": self.phone})
        if resp.status_code != 200:
            raise LoginFailedError(
                f"{self.role} login(send) failed: HTTP {resp.status_code} "
                f"body={resp.text[:500]}"
            )
        payload = resp.json()
        if payload.get("success") is False:
            raise LoginFailedError(
                f"{self.role} login(send) API error code={payload.get('code')} "
                f"message={payload.get('message')!r} data={payload.get('data')}"
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        code = (data or {}).get("code")
        if not code:
            raise LoginFailedError(
                f"{self.role} login(send): response 無 code（僅測試環境會回 code）：{data}"
            )
        self._confirm_login(str(code))

    def _confirm_login(self, code: str) -> None:
        """打 login/confirm，password=md5(驗證碼)；成功則寫入 token。"""
        body = {
            "phone": self.phone,
            "password": md5(code),
        }
        resp = self.client.post(self.login_path, body=body)
        if resp.status_code != 200:
            raise LoginFailedError(
                f"{self.role} login failed: HTTP {resp.status_code} "
                f"body={resp.text[:500]}"
            )
        payload = resp.json()
        # 接案者/商家 API 統一回包：{success, code, message, data: {...}}；錯誤時 success=false
        if payload.get("success") is False:
            raise LoginFailedError(
                f"{self.role} login API error code={payload.get('code')} "
                f"message={payload.get('message')!r} data={payload.get('data')}"
            )
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        token = data.get("accessToken") or data.get("access_token")
        if not token:
            raise LoginFailedError(f"{self.role} login: no accessToken in response: {data}")
        self.client.set_access_token(
            token=token,
            expired_at=data.get("accessTokenExpiredAt") or data.get("accessTokenExpired") or 0,
            refresh_token=data.get("refreshToken", ""),
            refresh_expired_at=data.get("refreshTokenExpiredAt") or data.get("refreshTokenExpired") or 0,
        )
        self._logged_in = True

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    def __repr__(self) -> str:
        return f"Actor(role={self.role}, id={self.user_id}, phone={self.phone}, logged_in={self._logged_in})"
