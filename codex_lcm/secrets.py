"""Secret redaction for codex-lcm — scrub sensitive values before persisting."""

from __future__ import annotations

import re
from typing import Any

# Ordered list of (name, compiled regex, replacement)
_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    # OpenAI- and Anthropic-style API keys
    ("api_key", re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "[REDACTED_API_KEY]"),
    # GitHub tokens
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    ("github_fine_grained", re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GITHUB_TOKEN]"),
    # Slack tokens
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "[REDACTED_SLACK_TOKEN]"),
    # AWS access keys
    ("aws_key", re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    ("aws_session_key", re.compile(r"ASIA[0-9A-Z]{16}"), "[REDACTED_AWS_KEY]"),
    (
        "aws_secret",
        re.compile(r"(?i)(aws_secret_access_key)\s*[:=]\s*\S+"),
        r"\1=[REDACTED]",
    ),
    # Google API keys
    ("google_key", re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED_GOOGLE_KEY]"),
    # JWT tokens (three dot-separated base64url segments)
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED_JWT]"),
    # Bearer authorization headers
    ("bearer", re.compile(r"(?i)bearer\s+[^\s\"',;]+"), "Bearer [REDACTED]"),
    # JSON credential fields (capture serializes structured tool payloads as JSON)
    (
        "json_password",
        re.compile(r'(?i)("(?:password|passwd|pwd)"\s*:\s*)"(?:\\.|[^"\\])*"'),
        r'\1"[REDACTED]"',
    ),
    (
        "json_token",
        re.compile(
            r'(?i)("(?:token|api[_-]?key|secret|client[_-]?secret|aws_secret_access_key)"\s*:\s*)'
            r'"(?:\\.|[^"\\])*"'
        ),
        r'\1"[REDACTED]"',
    ),
    # Generic password= / password: assignments
    ("password", re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    # Generic token= / token: / api_key= assignments
    ("token_assign", re.compile(r"(?i)(token|api[_-]?key|secret|client[_-]?secret)\s*[:=]\s*\S+"), r"\1=[REDACTED]"),
    # PEM private keys
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), "[REDACTED_PRIVATE_KEY]"),
]


def redact(text: str) -> str:
    """Redact common secret patterns from a string."""
    if not text:
        return text
    for _name, pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Return a recursively redacted copy of a JSON-like dictionary."""
    return {key: _redact_value(value) for key, value in data.items()}
