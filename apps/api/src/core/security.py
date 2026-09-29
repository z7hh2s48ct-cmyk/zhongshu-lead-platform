from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from .config import get_settings

settings = get_settings()
_password_hasher = PasswordHasher()
_fernet_key = base64.urlsafe_b64encode(hashlib.sha256(settings.field_encryption_key.encode("utf-8")).digest())
_fernet = Fernet(_fernet_key)


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def validate_internal_password(password: str) -> None:
    """Raise ``ValueError`` when an internal password is outside the agreed range."""

    if not 8 <= len(password) <= 128:
        raise ValueError("密码长度必须为 8 到 128 位")


def ensure_canonical_jwt(token: str) -> None:
    """Reject alternate Base64URL spellings of an otherwise identical JWT.

    JWT segments are unpadded Base64URL. Some decoders tolerate non-zero padding
    bits, allowing a different token string to decode to the same bytes. System
    tokens are always emitted canonically, so accepting alternate encodings adds
    ambiguity without compatibility benefit.
    """

    parts = token.split(".")
    if len(parts) != 3 or any(not part for part in parts):
        raise jwt.InvalidTokenError("invalid jwt serialization")
    for segment in parts:
        try:
            padded = segment + ("=" * (-len(segment) % 4))
            decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
            canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        except Exception as exc:
            raise jwt.InvalidTokenError("invalid jwt base64url encoding") from exc
        if not hmac.compare_digest(segment, canonical):
            raise jwt.InvalidTokenError("non-canonical jwt base64url encoding")


def create_access_token(subject: str, session_version: int, roles: list[str], company_id: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "sv": session_version,
        "roles": roles,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_minutes)).timestamp()),
    }
    if company_id:
        payload["company_id"] = company_id
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict[str, Any]:
    ensure_canonical_jwt(token)
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


def encrypt_text(value: str) -> str:
    return _fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return _fernet.decrypt(value.encode("ascii")).decode("utf-8")
    except InvalidToken:
        return None


def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    return digits


def phone_tail4(value: str | None) -> str | None:
    """脱敏号码 `138****8000` 本就展示后四位，明文存后四位用于等值搜索不扩大暴露面。

    归一化后不足 4 位（脏号/占位）返回 None，不可搜索。
    """

    if not value:
        return None
    normalized = normalize_phone(value)
    return normalized[-4:] if len(normalized) >= 4 else None


def _phone_hmac(value: str, secret: str) -> str:
    normalized = normalize_phone(value)
    return hmac.new(secret.encode("utf-8"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_phone(value: str) -> str:
    """Legacy compatible HMAC field used by V1.0.1 records."""

    return _phone_hmac(value, settings.phone_hash_secret)


def fingerprint_phone(value: str, *, secret: str | None = None) -> str:
    """Return the non-reversible V1.2 phone deduplication fingerprint."""

    return _phone_hmac(value, secret or settings.effective_phone_fingerprint_secret)


def mask_phone(value: str | None) -> str | None:
    if not value:
        return None
    normalized = normalize_phone(value)
    if len(normalized) < 7:
        return "****"
    return normalized[:3] + "****" + normalized[-4:]


# N3：外发异常文本（如 httpx 报错）会原样携带完整微信 API URL，
# 其中 access_token/secret/appid 等查询参数即凭据。负向后行断言排除
# error_code=、status_code= 等键名后缀撞车。
_CREDENTIAL_QUERY_PARAM = re.compile(
    r"(?i)(?<![a-z0-9_])(access_token|app_secret|appsecret|secret|appid|authorization|password|passwd|token|code)=([^&\s'\"<>]+)"
)


def scrub_credentials(text: str | None, *, max_length: int = 500) -> str | None:
    """Strip credential query-param values from exception/receipt text."""

    if not text:
        return text
    scrubbed = _CREDENTIAL_QUERY_PARAM.sub(r"\1=***", text)
    if len(scrubbed) > max_length:
        scrubbed = scrubbed[:max_length] + "…[truncated]"
    return scrubbed


def generate_token(bytes_length: int = 32) -> str:
    return secrets.token_urlsafe(bytes_length)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_signed_state(payload: dict[str, Any], *, expires_minutes: int = 10, purpose: str = "state") -> str:
    now = datetime.now(timezone.utc)
    data = {
        **payload,
        "purpose": purpose,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expires_minutes)).timestamp()),
    }
    return jwt.encode(data, settings.jwt_secret, algorithm="HS256")


def decode_signed_state(token: str, *, purpose: str = "state") -> dict[str, Any]:
    ensure_canonical_jwt(token)
    data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    if data.get("purpose") != purpose:
        raise jwt.InvalidTokenError("state purpose mismatch")
    return data
