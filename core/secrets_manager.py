from __future__ import annotations

"""
SOLOEARN Secrets Management Helper
Safe loading, validation, masking, and lifecycle management for application secrets.
Provides constant-time comparison (HMAC/timing attack defense), cryptographically secure
token generation (secrets.token_urlsafe), and recursive secret redaction for logs and exception traces.
"""

import hmac
import os
import re
import secrets
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


REQUIRED_SECRETS = {
    "OLLAMA_BASE_URL": "Ollama API endpoint (must be localhost)",
    "OLLAMA_MODEL": "Local LLM model name (e.g. qwen3:4b-instruct)",
}

OPTIONAL_SECRETS = {
    "SOLOEARN_OUTREACH_PROVIDER": "External outreach delivery provider",
    "SOLOEARN_OUTREACH_ACK": "Outreach provider acknowledgment key",
    "SOLOEARN_UPI_VPA": "UPI Virtual Payment Address for collections",
    "SOLOEARN_WEBHOOK_SECRET": "HMAC secret for payment webhook verification",
    "SOLOEARN_UPI_PAYEE_NAME": "Payee name for UPI QR generation",
    "OLLAMA_TIMEOUT_SECONDS": "Ollama request timeout in seconds",
    "MAX_UPLOAD_MB": "Maximum file upload size in megabytes",
}

# Regex patterns for sensitive keys and tokens
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?i)(secret|token|password|passwd|pwd|key|auth|bearer|signature|pin|otp|cvv|webhook_secret|vpa)"
)

TOKEN_PATTERN = re.compile(
    r"\b(tok_[A-Za-z0-9_\-]+|dlv_[A-Za-z0-9_\-]+|whsec_[A-Za-z0-9_\-]+)\b"
)

AUTH_HEADER_PATTERN = re.compile(
    r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]+"
)


def constant_time_compare(val1: str | bytes, val2: str | bytes) -> bool:
    """Strict constant-time comparison to eliminate timing attacks (HMAC / compare_digest)."""
    if val1 is None or val2 is None:
        return False
    b1 = val1.encode("utf-8") if isinstance(val1, str) else bytes(val1)
    b2 = val2.encode("utf-8") if isinstance(val2, str) else bytes(val2)
    return hmac.compare_digest(b1, b2)


def generate_secure_token(prefix: str = "", nbytes: int = 32) -> str:
    """Generates a cryptographically strong random token using secrets.token_urlsafe (256-bit entropy)."""
    entropy_token = secrets.token_urlsafe(nbytes)
    return f"{prefix}{entropy_token}" if prefix else entropy_token


def mask_secret(value: str, visible_chars: int = 3) -> str:
    """Returns a masked version of a secret value for safe display.

    Examples:
        mask_secret("my-secret-key-123") -> "my-***-123"
        mask_secret("short") -> "***"
        mask_secret("") -> "***"
    """
    if not value or len(value) <= visible_chars * 2:
        return "***"
    return f"{value[:visible_chars]}***{value[-visible_chars:]}"


def redact_secrets_from_text(text: str, custom_secrets: list[str] | None = None) -> str:
    """Redacts known tokens, auth headers, and active secrets from a text string to prevent log leakage."""
    if not text or not isinstance(text, str):
        return str(text) if text is not None else ""

    result = text
    # Mask common token patterns
    result = TOKEN_PATTERN.sub(lambda m: mask_secret(m.group(1)), result)
    result = AUTH_HEADER_PATTERN.sub(r"\1***REDACTED***", result)

    # Redact known environment secrets if provided or registered
    if custom_secrets:
        for sec in custom_secrets:
            if sec and len(sec) >= 4 and sec in result:
                result = result.replace(sec, mask_secret(sec))

    return result


def redact_sensitive_dict(data: Any) -> Any:
    """Recursively walks dictionaries and lists, masking any values associated with sensitive keys."""
    if isinstance(data, dict):
        cleaned: dict[str, Any] = {}
        for k, v in data.items():
            if SENSITIVE_KEY_PATTERN.search(str(k)):
                if isinstance(v, str):
                    cleaned[k] = mask_secret(v)
                elif isinstance(v, (int, float, bool)):
                    cleaned[k] = "***REDACTED***"
                elif v is None:
                    cleaned[k] = None
                else:
                    cleaned[k] = redact_sensitive_dict(v)
            else:
                cleaned[k] = redact_sensitive_dict(v)
        return cleaned
    elif isinstance(data, list):
        return [redact_sensitive_dict(item) for item in data]
    elif isinstance(data, tuple):
        return tuple(redact_sensitive_dict(item) for item in data)
    elif isinstance(data, str):
        return redact_secrets_from_text(data)
    return data


class SecretsManager:
    """Manages application secrets with validation, masking, and safe access."""

    def __init__(self, env_file: str | Path = ".env"):
        self._env_file = Path(env_file)
        self._loaded = False

    def __repr__(self) -> str:
        return f"<SecretsManager env_file={self._env_file.name} loaded={self._loaded}>"

    def __str__(self) -> str:
        return self.__repr__()

    def load_secrets(self) -> None:
        """Loads secrets from the .env file into the environment."""
        if self._env_file.exists():
            load_dotenv(self._env_file, override=False)
        self._loaded = True

    def get_secret(self, key: str, required: bool = False, default: str = "") -> str:
        """Retrieves a secret value from the environment.

        Args:
            key: Environment variable name.
            required: If True, raises ValueError when the secret is missing.
            default: Default value if the secret is not set.

        Returns:
            The secret value or default.

        Raises:
            ValueError: If required=True and the secret is missing or empty.
        """
        if not self._loaded:
            self.load_secrets()

        value = os.environ.get(key, "").strip()
        if not value:
            if required:
                raise ValueError(
                    f"Required secret '{key}' is not set. "
                    f"Add it to {self._env_file} or set it as an environment variable."
                )
            return default
        return value

    def validate_secrets(self) -> dict[str, Any]:
        """Validates all required and optional secrets.

        Returns:
            A dictionary with validation results:
            {
                "valid": bool,
                "required": {"KEY": {"present": bool, "description": str}},
                "optional": {"KEY": {"present": bool, "description": str}},
                "missing_required": ["KEY1", ...],
                "missing_optional": ["KEY1", ...],
            }
        """
        if not self._loaded:
            self.load_secrets()

        result: dict[str, Any] = {
            "required": {},
            "optional": {},
            "missing_required": [],
            "missing_optional": [],
        }

        for key, desc in REQUIRED_SECRETS.items():
            present = bool(os.environ.get(key, "").strip())
            result["required"][key] = {"present": present, "description": desc}
            if not present:
                result["missing_required"].append(key)

        for key, desc in OPTIONAL_SECRETS.items():
            present = bool(os.environ.get(key, "").strip())
            result["optional"][key] = {"present": present, "description": desc}
            if not present:
                result["missing_optional"].append(key)

        result["valid"] = len(result["missing_required"]) == 0
        return result

    def get_safe_summary(self) -> dict[str, str]:
        """Returns a masked summary of all configured secrets for display/logging.

        Never exposes full secret values.
        """
        if not self._loaded:
            self.load_secrets()

        summary: dict[str, str] = {}
        all_keys = list(REQUIRED_SECRETS.keys()) + list(OPTIONAL_SECRETS.keys())
        for key in all_keys:
            val = os.environ.get(key, "")
            if val:
                summary[key] = mask_secret(val)
            else:
                summary[key] = "(not set)"
        return summary
