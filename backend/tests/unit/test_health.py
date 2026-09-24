from types import SimpleNamespace

from docket.inference.health import HealthReport, check_ollama, format_health_warning


class Client:
    def __init__(self, response=None, exc=None):
        self.response, self.exc = response, exc

    def list(self):
        if self.exc:
            raise self.exc
        return self.response


def typed(*names):
    return SimpleNamespace(models=[SimpleNamespace(model=n) for n in names])


def dicts(*names, key="model"):
    return {"models": [{key: n} for n in names]}


def test_all_present_typed():
    r = check_ollama(["qwen3:14b", "qwen3-embedding:0.6b"], client=Client(typed("qwen3:14b", "qwen3-embedding:0.6b")))
    assert r.reachable and r.missing_models == [] and r.ok
    assert format_health_warning(r) == []


def test_unreachable_never_raises():
    r = check_ollama(["x"], host="http://h:1", client=Client(exc=ConnectionError("refused")))
    assert not r.reachable and r.error and r.host == "http://h:1"
    (line,) = format_health_warning(r)
    assert line.startswith("Can't reach Ollama at http://h:1") and "ollama serve" in line


def test_real_closed_port_does_not_raise():
    r = check_ollama(["x"], host="http://127.0.0.1:1", timeout=0.5)
    assert not r.reachable


def test_missing_and_latest_normalization():
    r = check_ollama(["qwen3:14b", "foo"], client=Client(dicts("qwen3:8b", "foo:latest")))
    assert r.missing_models == ["qwen3:14b"]
    assert format_health_warning(r) == ["Model not found: qwen3:14b — run: ollama pull qwen3:14b"]
    r = check_ollama(["foo:latest"], client=Client(dicts("foo")))
    assert r.missing_models == []


def test_dict_shapes():
    assert check_ollama(["a:1"], client=Client(dicts("a:1"))).ok
    assert check_ollama(["a:1"], client=Client(dicts("a:1", key="name"))).ok
    assert check_ollama(["a:1"], client=Client(dicts())).missing_models == ["a:1"]
    assert check_ollama(["a:1"], client=Client(None)).missing_models == ["a:1"]


def test_format_none():
    assert format_health_warning(None) == []
    assert format_health_warning(HealthReport(reachable=True, host="h")) == []
