"""Tests for secret redaction using synthetic, non-credential fixtures."""

from codex_lcm.secrets import redact, redact_dict


def synthetic(prefix: str, character: str, length: int) -> str:
    return prefix + (character * length)


def test_openai_api_key():
    value = synthetic("s" + "k-", "a", 32)
    assert value not in redact(f'key="{value}"')


def test_github_token():
    value = synthetic("gh" + "p_", "c", 36)
    assert value not in redact(f"token={value}")


def test_jwt():
    value = "e" + "yJ" + ("a" * 12) + "." + ("b" * 12) + "." + ("c" * 12)
    assert value not in redact(f"auth={value}")


def test_bearer_header():
    value = synthetic("", "z", 40)
    cleaned = redact(f"Authorization: Bearer {value}")
    assert value not in cleaned
    assert "[REDACTED]" in cleaned


def test_bearer_header_redacts_base64_characters_completely():
    value = "abcDEF0123+/==XYZ"
    cleaned = redact(f"Authorization: Bearer {value}")
    assert cleaned == "Authorization: Bearer [REDACTED]"


def test_password_assignment():
    assert "password=[REDACTED]" in redact("password=hunter2")


def test_json_credential_assignments():
    dirty = (
        '{"password": "hunter2", "client_secret": "base64+/=value", '  # pragma: allowlist secret
        '"aws_secret_access_key": "awsBase64+/=secret"}'  # pragma: allowlist secret
    )
    cleaned = redact(dirty)
    assert "hunter2" not in cleaned
    assert "base64+/=value" not in cleaned
    assert "awsBase64+/=secret" not in cleaned


def test_private_key_block():
    begin = "-" * 5 + "BEGIN RSA PRIVATE KEY" + "-" * 5  # pragma: allowlist secret
    end = "-" * 5 + "END RSA PRIVATE KEY" + "-" * 5
    value = f"{begin}\nsynthetic-data\n{end}"
    cleaned = redact(value)
    assert value not in cleaned
    assert cleaned == "[REDACTED_PRIVATE_KEY]"


def test_redact_dict_recursive():
    api_key = synthetic("s" + "k-", "d", 32)
    data = {
        "a": api_key,
        "b": {"c": "token=abc", "d": ["safe", {"nested": api_key}]},
    }

    out = redact_dict(data)

    assert api_key not in str(out)
    assert "token=[REDACTED]" in str(out)
    assert data["a"] == api_key


def test_short_values_untouched():
    assert redact("hello world") == "hello world"


def test_release_credential_matrix():
    synthetic_values = [
        synthetic("s" + "k-", "a", 32),
        synthetic("s" + "k-ant-", "b", 32),
        synthetic("gh" + "p_", "c", 36),
        synthetic("github_" + "pat_", "d", 40),
        synthetic("AK" + "IA", "E", 16),
        synthetic("AS" + "IA", "F", 16),
        "aws_secret_access_key=" + ("g" * 40),
        "Bearer " + ("h" * 40),
        "e" + "yJ" + ("i" * 12) + "." + ("j" * 12) + "." + ("k" * 12),
    ]
    begin = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5  # pragma: allowlist secret
    end = "-" * 5 + "END PRIVATE KEY" + "-" * 5
    synthetic_values.append(f"{begin}\nsynthetic-data\n{end}")

    for value in synthetic_values:
        assert value not in redact(value), value.split("=", 1)[0]