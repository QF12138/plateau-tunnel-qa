"""Protect saved credentials; never copy environment credentials into the database.

Windows uses the current OS user's DPAPI. Portable deployments can supply a
separate Fernet key through TUNNEL_QA_SECRETS_ENCRYPTION_KEY. No plaintext fallback.
"""

import base64
import ctypes
import sys

from cryptography.fernet import Fernet, InvalidToken

from tunnel_qa.core.config import Settings
from tunnel_qa.core.errors import AppError

PREFIX = "protected:v1:"


def _dpapi(data: bytes, *, decrypt: bool = False) -> bytes:
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(Blob)]
    function.restype = ctypes.c_int
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    # CRYPTPROTECT_UI_FORBIDDEN: an unattended service must not open a dialog.
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise OSError("系统凭据保护不可用")
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel.LocalFree(result.data)


def protect_secret(value: str, settings: Settings) -> str:
    try:
        if settings.secrets_encryption_key is not None:
            cipher = Fernet(settings.secrets_encryption_key.get_secret_value().encode())
            return PREFIX + "fernet:" + cipher.encrypt(value.encode()).decode()
        if sys.platform == "win32":
            return PREFIX + "dpapi:" + base64.b64encode(_dpapi(value.encode())).decode()
    except (OSError, ValueError) as exc:
        raise AppError("SECRET_PROTECTION_FAILED", "凭据加密失败，未保存密钥", status_code=503) from exc
    raise AppError(
        "SECRET_KEY_REQUIRED", "请先配置服务器的 SECRETS_ENCRYPTION_KEY，再保存 API Key",
        status_code=503,
    )


def reveal_secret(value: str, settings: Settings) -> str:
    try:
        if value.startswith(PREFIX + "fernet:") and settings.secrets_encryption_key:
            cipher = Fernet(settings.secrets_encryption_key.get_secret_value().encode())
            return cipher.decrypt(value.removeprefix(PREFIX + "fernet:").encode()).decode()
        if value.startswith(PREFIX + "dpapi:") and sys.platform == "win32":
            return _dpapi(base64.b64decode(value.removeprefix(PREFIX + "dpapi:"), validate=True), decrypt=True).decode()
    except (OSError, ValueError, InvalidToken) as exc:
        raise AppError("SECRET_UNAVAILABLE", "无法解密已保存凭据，请检查服务账号或加密密钥", status_code=503) from exc
    raise AppError("SECRET_UNAVAILABLE", "凭据保护方式或服务器加密密钥不匹配", status_code=503)
