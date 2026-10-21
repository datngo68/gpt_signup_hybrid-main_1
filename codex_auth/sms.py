"""SMS Provider clients for phone verification in Codex OAuth.

Supports:
- ViOTP (https://viotp.com)
- Chothuesimcode (https://chothuesimcode.com)
"""
from __future__ import annotations

import abc
import asyncio
import re
import time
from typing import Any, Callable, Optional
import urllib.request
import urllib.parse
import json

import os
from pathlib import Path

from .errors import CodexAuthError

LogFn = Callable[[str], None]


def get_env_sms_key() -> Optional[str]:
    """Tìm API key SMS từ biến môi trường (OS env hoặc file .dev.var / .env)."""
    env_names = (
        "CODEX_SMS_API_KEY",
        "AIPKEY_SMS",
        "APIKEY_SMS",
        "VIOTP_API_KEY",
        "VIOTP_TOKEN",
        "CHOTHUESIMCODE_API_KEY",
    )
    # 1. Check trực tiếp trong os.environ
    for name in env_names:
        val = os.environ.get(name)
        if val and val.strip():
            return val.strip()

    # 2. Check file .dev.var hoặc .env trong root dự án
    root = Path(__file__).resolve().parent.parent
    for fname in (".dev.var", ".env", ".env.local"):
        fpath = root / fname
        if fpath.exists():
            try:
                for line in fpath.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, _, v = line.partition("=")
                    if k.strip().upper() in env_names:
                        v_clean = v.strip().strip("'\"")
                        if v_clean:
                            return v_clean
            except Exception:
                continue

    return None


class SmsError(CodexAuthError):
    """Lỗi trong quá trình thao tác với SMS API provider."""


def _http_get_json(url: str, timeout: float = 15.0) -> dict[str, Any]:
    """Helper GET request trả về JSON dictionary."""
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) CodexAuth/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8", errors="replace")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError("Expected object")
            return result
    except Exception:
        # urllib exceptions may include the full URL (including provider API key).
        raise SmsError(f"HTTP GET {url.split('?')[0]} thất bại") from None


def extract_otp_code(text: str) -> Optional[str]:
    """Tìm mã OTP 6 số trong nội dung tin nhắn SMS."""
    if not text:
        return None
    # 1. Tìm 6 chữ số liền kề: 123456
    m = re.search(r"\b(\d{6})\b", text)
    if m:
        return m.group(1)
    # 2. Tìm dạng 123-456 hoặc 123 456
    m_split = re.search(r"\b(\d{3})[- ](\d{3})\b", text)
    if m_split:
        return f"{m_split.group(1)}{m_split.group(2)}"
    return None


def clean_phone_vn(phone: str) -> tuple[str, str]:
    """Chuẩn hoá số điện thoại Việt Nam.

    Trả về (dial_code, national_number):
    Ví dụ: '0381234567' -> ('+84', '381234567')
           '84381234567' -> ('+84', '381234567')
           '+84381234567' -> ('+84', '381234567')
    """
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if cleaned.startswith("+84"):
        raw = cleaned[3:]
    elif cleaned.startswith("84") and len(cleaned) >= 11:
        raw = cleaned[2:]
    elif cleaned.startswith("0"):
        raw = cleaned[1:]
    else:
        raw = cleaned

    return "+84", raw


class SmsProvider(abc.ABC):
    """Interface trừu tượng cho dịch vụ nhận SMS OTP."""

    @abc.abstractmethod
    def get_balance(self) -> float:
        """Lấy số dư tài khoản."""

    @abc.abstractmethod
    def request_phone(self) -> tuple[str, str]:
        """Thuê số điện thoại mới.

        Returns:
            (request_id, phone_number)
        """

    @abc.abstractmethod
    async def get_otp(
        self,
        request_id: str,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
        log: LogFn = print,
    ) -> str:
        """Chờ và lấy mã OTP từ SMS.

        Returns:
            Mã OTP 6 số.

        Raises:
            SmsError nếu timeout hoặc nhà mạng huỷ.
        """

    @abc.abstractmethod
    def cancel(self, request_id: str) -> bool:
        """Huỷ số nếu không nhận được mã hoặc số bị lỗi."""


class ViotpProvider(SmsProvider):
    """Provider ViOTP với rental giới hạn ở Vinaphone và catalog được xác thực."""

    BASE_URL = "https://api.viotp.com"
    REQUIRED_NETWORK = "VINAPHONE"
    VINAPHONE_PREFIXES = ("081", "082", "083", "084", "085", "088", "091", "094")
    _CARRIER_FIELDS = ("network", "carrier", "provider", "telco", "operator")

    def __init__(self, api_key: Optional[str] = None, service_id: str = "1234", network: str = "VINAPHONE") -> None:
        key = (api_key or "").strip() or get_env_sms_key()
        if not key:
            raise SmsError("ViOTP yêu cầu api_key (truyền tham số hoặc đặt env AIPKEY_SMS / CODEX_SMS_API_KEY)")
        self.api_key = key
        self.service_id = str(service_id).strip() or "1234"
        self.network = str(network).strip().upper() if network else self.REQUIRED_NETWORK
        if self.network != self.REQUIRED_NETWORK:
            raise SmsError("ViOTP chỉ cho phép network=VINAPHONE")
        self._validated_service_id: Optional[str] = None

    def get_balance(self) -> float:
        url = f"{self.BASE_URL}/users/balance?token={urllib.parse.quote(self.api_key)}"
        res = _http_get_json(url)
        if res.get("status_code") != 200:
            raise SmsError(f"ViOTP check balance lỗi: {res.get('message', res)}")
        data = res.get("data", {})
        return float(data.get("balance", 0.0))

    def get_services(self) -> list[dict[str, Any]]:
        """Lấy catalog dịch vụ; lỗi/shape sai phải chặn trước rental."""
        url = f"{self.BASE_URL}/service/getv2?token={urllib.parse.quote(self.api_key)}&country=vn"
        res = _http_get_json(url)
        data = res.get("data")
        if res.get("status_code") != 200 or not isinstance(data, list):
            raise SmsError("ViOTP không xác thực được catalog dịch vụ")
        return [item for item in data if isinstance(item, dict)]

    def resolve_openai_service_id(self) -> str:
        """Xác thực service ID cấu hình map đúng OpenAI/ChatGPT, cache trong instance."""
        if self._validated_service_id is not None:
            return self._validated_service_id
        matches = []
        for service in self.get_services():
            name = str(service.get("name") or "").casefold()
            if "openai" in name or "chatgpt" in name:
                service_id = str(service.get("id") or "").strip()
                if service_id:
                    matches.append(service_id)
        if len(matches) != 1:
            raise SmsError("ViOTP catalog không xác định duy nhất dịch vụ OpenAI/ChatGPT")
        configured = self.service_id
        if configured.lower() in ("auto", "0"):
            configured = matches[0]
        if configured != matches[0]:
            raise SmsError("ViOTP service ID cấu hình không map tới OpenAI/ChatGPT")
        self._validated_service_id = configured
        return configured

    @classmethod
    def _response_carrier(cls, data: dict[str, Any]) -> Optional[str]:
        for field in cls._CARRIER_FIELDS:
            value = data.get(field)
            if value:
                return str(value).strip().upper().replace(" ", "")
        return None

    @classmethod
    def has_vinaphone_prefix(cls, phone: str) -> bool:
        digits = re.sub(r"\D", "", phone)
        if digits.startswith("84"):
            digits = "0" + digits[2:]
        return any(digits.startswith(prefix) for prefix in cls.VINAPHONE_PREFIXES)

    def request_phone(self) -> tuple[str, str]:
        service_id = self.resolve_openai_service_id()
        url = (
            f"{self.BASE_URL}/request/getv2"
            f"?token={urllib.parse.quote(self.api_key)}"
            f"&serviceId={urllib.parse.quote(service_id)}"
            f"&network={urllib.parse.quote(self.REQUIRED_NETWORK)}"
        )
        res = _http_get_json(url)
        if res.get("status_code") != 200:
            raise SmsError("ViOTP thuê số Vinaphone thất bại")

        data = res.get("data")
        if not isinstance(data, dict):
            raise SmsError("ViOTP trả về phiên thuê số không hợp lệ")
        request_id = str(data.get("request_id") or "")
        phone = str(data.get("phone_number") or data.get("re_phone_number") or "")
        carrier = self._response_carrier(data)
        carrier_is_vina = carrier in ("VINAPHONE", "VINA", "VNPT", "VNPTVINAPHONE")
        if carrier and not carrier_is_vina:
            if request_id:
                self.cancel(request_id)
            raise SmsError("ViOTP trả về số không thuộc network VINAPHONE")
        if not request_id or not re.fullmatch(r"\+?\d{9,15}", phone):
            if request_id:
                self.cancel(request_id)
            raise SmsError("ViOTP trả về phiên thuê số không hợp lệ")
        # Prefix chỉ là tín hiệu hỗ trợ: MNP có thể làm prefix khác carrier hiện tại.
        self.has_vinaphone_prefix(phone)
        return request_id, phone

    async def get_otp(
        self,
        request_id: str,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
        log: LogFn = print,
    ) -> str:
        deadline = time.monotonic() + timeout
        url = (
            f"{self.BASE_URL}/session/getv2"
            f"?requestId={urllib.parse.quote(request_id)}"
            f"&token={urllib.parse.quote(self.api_key)}"
        )

        while time.monotonic() < deadline:
            try:
                res = await asyncio.to_thread(_http_get_json, url)
                if res.get("status_code") == 200:
                    data = res.get("data", {})
                    status = data.get("Status")
                    # Status: 0: Đang chờ, 1: Thành công, 2: Hết hạn
                    if status == 1:
                        code = data.get("Code") or extract_otp_code(data.get("SmsContent", ""))
                        if code and re.fullmatch(r"\d{6}", str(code).strip()):
                            return str(code).strip()
                    elif status == 2:
                        raise SmsError("ViOTP: phiên thuê số đã hết hạn/bị huỷ")
            except SmsError:
                raise
            except Exception as exc:
                log(f"[viotp] poll error (sẽ thử lại): {exc}")

            await asyncio.sleep(poll_interval)

        raise SmsError(f"ViOTP: timeout {timeout}s chờ OTP")

    def cancel(self, request_id: str) -> bool:
        # ViOTP v2 không bắt buộc endpoint cancel (tự hết hạn sau session)
        return True


class ChothuesimcodeProvider(SmsProvider):
    """Provider cho Chothuesimcode (https://chothuesimcode.com).

    Service ID (appId) cho OpenAI thường là '1005' hoặc theo bảng app của bên cung cấp.
    """

    BASE_URL = "https://chothuesimcode.com/api"

    def __init__(self, api_key: Optional[str] = None, service_id: str = "1005") -> None:
        key = (api_key or "").strip() or get_env_sms_key()
        if not key:
            raise SmsError("Chothuesimcode yêu cầu api_key (truyền tham số hoặc đặt env)")
        self.api_key = key
        self.service_id = str(service_id).strip() or "1005"

    def get_balance(self) -> float:
        url = f"{self.BASE_URL}?act=account&apik={urllib.parse.quote(self.api_key)}"
        res = _http_get_json(url)
        if res.get("ResponseCode") != 0:
            raise SmsError(f"Chothuesimcode check balance lỗi: {res.get('Msg', res)}")
        result = res.get("Result", {})
        return float(result.get("Balance", 0.0))

    def request_phone(self) -> tuple[str, str]:
        url = (
            f"{self.BASE_URL}?act=number"
            f"&apik={urllib.parse.quote(self.api_key)}"
            f"&appId={urllib.parse.quote(self.service_id)}"
        )
        res = _http_get_json(url)
        if res.get("ResponseCode") != 0:
            raise SmsError(f"Chothuesimcode thuê số thất bại: {res.get('Msg', res)}")
        result = res.get("Result", {})
        request_id = str(result.get("Id") or "")
        phone = str(result.get("Number") or "")
        if not request_id or not re.fullmatch(r"\+?\d{9,15}", phone):
            if request_id:
                self.cancel(request_id)
            raise SmsError("Chothuesimcode trả về phiên thuê số không hợp lệ")
        return request_id, phone

    async def get_otp(
        self,
        request_id: str,
        timeout: float = 120.0,
        poll_interval: float = 3.0,
        log: LogFn = print,
    ) -> str:
        deadline = time.monotonic() + timeout
        url = (
            f"{self.BASE_URL}?act=code"
            f"&apik={urllib.parse.quote(self.api_key)}"
            f"&id={urllib.parse.quote(request_id)}"
        )

        while time.monotonic() < deadline:
            try:
                res = await asyncio.to_thread(_http_get_json, url)
                code_resp = res.get("ResponseCode")
                if code_resp == 0:
                    result = res.get("Result", {})
                    code = result.get("Code") or extract_otp_code(result.get("SMS", ""))
                    if code and re.fullmatch(r"\d{6}", str(code).strip()):
                        return str(code).strip()
            except SmsError:
                raise
            except Exception as exc:
                log(f"[chothuesimcode] poll error (sẽ thử lại): {exc}")

            await asyncio.sleep(poll_interval)

        raise SmsError(f"Chothuesimcode: timeout {timeout}s chờ OTP cho id {request_id}")

    def cancel(self, request_id: str) -> bool:
        url = (
            f"{self.BASE_URL}?act=cancel"
            f"&apik={urllib.parse.quote(self.api_key)}"
            f"&id={urllib.parse.quote(request_id)}"
        )
        try:
            res = _http_get_json(url)
            return res.get("ResponseCode") == 0
        except Exception:
            return False


def create_sms_provider(
    provider_name: Optional[str] = None,
    api_key: Optional[str] = None,
    service_id: Optional[str] = None,
    network: Optional[str] = "VINAPHONE",
) -> SmsProvider:
    """Factory khởi tạo SmsProvider theo tên (tự nạp API key từ ENV / .dev.var nếu None)."""
    norm = (provider_name or os.environ.get("CODEX_SMS_PROVIDER") or "viotp").strip().lower()
    key = (api_key or "").strip() or get_env_sms_key()
    if norm in ("viotp", "vi_otp"):
        return ViotpProvider(api_key=key, service_id=service_id or "1234", network=network or "VINAPHONE")
    if norm in ("chothuesimcode", "ctsc"):
        return ChothuesimcodeProvider(api_key=key, service_id=service_id or "1005")
    raise SmsError(f"Provider SMS không được hỗ trợ: {provider_name} (chỉ hỗ trợ 'viotp' hoặc 'chothuesimcode')")
