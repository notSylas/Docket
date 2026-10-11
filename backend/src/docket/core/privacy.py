"""Conservative pattern redaction for logs and explicit exports.

This masks common identifiers and credentials, not every kind of personal data.
Stored originals and private feedback are deliberately never rewritten.
"""
import io
import re
import threading

_EMAIL = re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
_DIGITS = re.compile(r"\d(?:[ \t().+-]*\d){8,}")
_AUTH = re.compile(r"\b(Bearer|Basic)[ \t]+[A-Za-z0-9._~+/-]+=*", re.I)
_TOKEN = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|"
    r"github_pat_[A-Za-z0-9_]{12,}|AKIA[A-Z0-9]{16}|AIza[A-Za-z0-9_-]{30,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|hf_[A-Za-z0-9]{16,}|"
    r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
_KEY_VALUE = re.compile(
    r"\b((?:[A-Z0-9]+[_-])*(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
    r"secret[_ -]?access[_ -]?key|secret(?:[_ -]?key)?|private[_ -]?key|database[_ -]?url|"
    r"connection[_ -]?string|password|passwd|pwd|token|authorization))"
    r"([\"']?[ \t]*[:=][ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;}\]]+)", re.I
)
_BEGIN = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----")
_END = re.compile(r"-----END (?:[A-Z]+ )*PRIVATE KEY-----")
_PEM = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----[\s\S]*?(?:-----END (?:[A-Z]+ )*PRIVATE KEY-----|$)")
_URL_CREDENTIALS = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@", re.I)


def redact_text(text: str) -> str:
    text = _PEM.sub("[private key]", text)
    text = _URL_CREDENTIALS.sub(lambda match: match[1] + "[secret]@", text)
    text = _AUTH.sub(lambda match: match[1] + " [secret]", text)
    text = _TOKEN.sub("[secret]", text)
    text = _KEY_VALUE.sub(lambda match: match[1] + match[2] + "[secret]", text)
    text = _EMAIL.sub("[email]", text)
    return _DIGITS.sub("[digits]", text)


class RedactedLogStream:
    """Buffer through newlines so fragmented writes cannot leak credentials.

    Flush does not publish an unfinished line. Close redacts the final line.
    Overlong lines are suppressed, and PEM bodies stay hidden across lines.
    """
    MAX_LINE = 32768

    def __init__(self, stream):
        self.stream = stream
        self._pending = ""
        self._discard = False
        self._private_key = False
        self._closed = False
        self._lock = threading.RLock()

    @property
    def encoding(self):
        return self.stream.encoding

    @property
    def closed(self):
        return self._closed

    def isatty(self):
        return False

    def writable(self):
        return not self.closed

    def fileno(self):
        raise io.UnsupportedOperation("A redacted log has no raw file descriptor.")

    def _emit(self, line):
        if self._private_key or _BEGIN.search(line):
            self._private_key = not bool(_END.search(line))
            self.stream.write("[private key]\n")
        else:
            self.stream.write(redact_text(line))

    def write(self, text):
        with self._lock:
            if self.closed:
                raise ValueError("write to closed log")
            parts = text.split("\n")
            for index, part in enumerate(parts):
                if not self._discard:
                    if len(self._pending) + len(part) > self.MAX_LINE:
                        if _BEGIN.search(self._pending + part):
                            self._private_key = True
                        self._pending = ""
                        self._discard = True
                    else:
                        self._pending += part
                if index < len(parts) - 1:
                    self._emit("[long log line redacted]\n" if self._discard else self._pending + "\n")
                    self._pending, self._discard = "", False
            return len(text)

    def flush(self):
        with self._lock:
            if not self.closed:
                self.stream.flush()

    def close(self):
        with self._lock:
            if not self.closed:
                if self._pending or self._discard:
                    self._emit("[long log line redacted]\n" if self._discard else self._pending)
                self.stream.close()
                self._closed = True
