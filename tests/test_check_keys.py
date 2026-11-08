"""relay.check_keys: reads balances, flags low/failing keys, never crashes."""
import httpx

from relay import check_keys, config


class R:
    def __init__(self, code, body):
        self.status_code, self._body, self.text = code, body, str(body)

    def json(self):
        return self._body


def test_openrouter_low_balance(monkeypatch):
    monkeypatch.setattr(config, "JEV_API_KEY", "k")
    def get(url, **kw):
        if url.endswith("/key"):
            return R(200, {"data": {"limit_remaining": None}})
        return R(200, {"data": {"total_credits": 10, "total_usage": 9.5}})
    monkeypatch.setattr(httpx, "get", get)
    status, detail = check_keys.check_openrouter()
    assert status == check_keys.WARN and "$0.50" in detail


def test_anthropic_out_of_credit(monkeypatch):
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(httpx, "post", lambda url, **kw: R(400, {"error": {"message": "Your credit balance is too low"}}))
    status, detail = check_keys.check_anthropic()
    assert status == check_keys.FAIL and "credit balance" in detail


def test_gemini_rate_limited(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    monkeypatch.setattr(httpx, "post", lambda url, **kw: R(429, {"error": {"message": "quota"}}))
    assert check_keys.check_gemini()[0] == check_keys.WARN


def test_elevenlabs_characters_left(monkeypatch):
    monkeypatch.setattr(config, "ELEVENLABS_API_KEY", "k")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: R(200, {"character_limit": 30000, "character_count": 1000}))
    status, detail = check_keys.check_elevenlabs()
    assert status == check_keys.OK and "29,000" in detail


def test_missing_key_and_network_error(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")
    assert check_keys.check_gemini()[0] == check_keys.FAIL
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    def boom(*a, **kw):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "post", boom)
    assert check_keys.check_anthropic()[0] == check_keys.FAIL


def test_main_reports_worst(monkeypatch, capsys):
    monkeypatch.setattr(check_keys, "CHECKS", [("A", lambda: (check_keys.OK, "fine")),
                                               ("B", lambda: (check_keys.WARN, "low")),
                                               ("C", lambda: 1 / 0)])
    assert check_keys.main() == 1
    out = capsys.readouterr().out
    assert "[FAIL] C" in out and "check crashed" in out
