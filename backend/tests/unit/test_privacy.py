import io
import logging

import pytest
from docket.core.privacy import RedactedLogStream, redact_text


@pytest.mark.parametrize("secret", [
    "helena@example.com", "123456789012", "+91 98765 43210", "१२३४५६७८९०",
    "password=hunter2", 'api_key="privateValue"', "Authorization: Bearer aaa.bbb.ccc",
    "sk-proj-abcdefghijklmnop", "ghp_abcdefghijklmnop", "AKIAABCDEFGHIJKLMNOP",
    "eyJabc.defghi.jklmno", "-----BEGIN PRIVATE KEY-----\nPRIVATECONTENT\n-----END PRIVATE KEY-----",
    "AWS_SECRET_ACCESS_KEY=privateValue", "DB_PASSWORD=hunter2", "postgres://admin:hunter2@localhost/db",
])
def test_common_identifiers_and_secrets_are_masked(secret):
    assert secret not in redact_text("before " + secret + " after")


def test_redaction_preserves_ordinary_prose_and_short_numbers():
    text = "Approved in 2026, with 420 rupees. हिंदी notes remain readable."
    assert redact_text(text) == text


def test_fragmented_writes_flush_and_multiline_private_keys_do_not_leak(tmp_path):
    path = tmp_path / "debug.log"
    stream = RedactedLogStream(path.open("w", encoding="utf-8"))
    for fragment in ["email: helena@", "example.com password=", "hunter2"]:
        stream.write(fragment); stream.flush()
        assert path.read_text() == ""
    stream.write("\n-----BEGIN PRIVATE KEY-----\n")
    stream.write("SUPERSECRETKEYBODY\n-----END PRIVATE KEY-----\n")
    stream.write("digits=12345678901")
    stream.close()
    text = path.read_text()
    assert "helena" not in text and "hunter2" not in text and "SUPERSECRET" not in text
    assert "12345678901" not in text and "[email]" in text and "[digits]" in text


def test_overlong_log_line_is_suppressed_with_bounded_buffer(tmp_path):
    path = tmp_path / "debug.log"
    stream = RedactedLogStream(path.open("w", encoding="utf-8"))
    stream.write("x" * 100000)
    assert len(stream._pending) <= stream.MAX_LINE
    stream.write("secret tail\nordinary next line\n")
    stream.close()
    text = path.read_text()
    assert "secret tail" not in text and "ordinary next line" in text


def test_tui_debug_log_masks_prints_and_logging(tmp_path, monkeypatch):
    from docket.interfaces.cli.quiet import quiet_streams
    monkeypatch.setenv("DOCKET_DEBUG", "1")
    path = tmp_path / "tui-debug.log"
    logger = logging.getLogger("docket-redaction-test")
    handler = logging.StreamHandler()
    logger.addHandler(handler); logger.setLevel(logging.WARNING)
    try:
        with quiet_streams(path):
            print("helena@", end="", flush=True)
            print("example.com password=hunter2")
            logger.warning("Account 123456789012 with api_key=privateValue")
    finally:
        logger.removeHandler(handler)
    content = path.read_text()
    assert all(value not in content for value in ("helena@example.com", "hunter2", "123456789012", "privateValue"))
