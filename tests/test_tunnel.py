from relay import tunnel


def test_find_url_in_cloudflared_output():
    line = "2026-11-07T22:01:02Z INF |  https://brave-lion-river-cat.trycloudflare.com                    |"
    assert tunnel.find_url(line) == "https://brave-lion-river-cat.trycloudflare.com"
    assert tunnel.find_url("INF Registered tunnel connection") is None


def test_start_without_cloudflared(monkeypatch):
    monkeypatch.setattr(tunnel.shutil, "which", lambda name: None)
    assert tunnel.start(8000) == (None, None)


def test_banner_hides_token_and_shows_health():
    text = tunnel.banner("https://a-b.trycloudflare.com", "e38Ysecretsecret")
    assert "https://a-b.trycloudflare.com/health" in text
    assert "e38Y…" in text and "secretsecret" not in text
    assert "brew install cloudflared" in tunnel.banner(None, "x")
