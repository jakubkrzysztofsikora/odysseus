"""Static deployment gate for the Circit-branded public surfaces."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _read(path: str) -> str:
    return (STATIC / path).read_text(encoding="utf-8")


def test_index_critical_shell_uses_circit_brand_and_tokens():
    html = _read("index.html")
    assert "<title>Circit AI</title>" in html
    assert "/static/vendor/circit-ds/circit-tokens.css" in html
    assert "/static/vendor/circit-ds/circit-wordmark.svg" in html
    assert "/static/vendor/circit-ds/circit-favicon.jpg" in html
    assert '<span class="sidebar-brand-title">AI</span>' in html
    assert "circit-empty-state" in html
    assert "welcome-signal-row" in html
    assert '<h1 class="a11y-visually-hidden">Circit AI</h1>' in html
    assert 'id="current-meta">Circit AI<' in html
    assert "Message Circit AI..." in html
    assert '<span class="sidebar-brand-title">Odysseus</span>' not in html
    assert "Odysseus Chat" not in html
    assert "Message Odysseus" not in html
    assert "welcome-boat" not in html
    assert "hero__statbar" not in html


def test_runtime_visible_fallbacks_use_circit_brand():
    app_js = _read("app.js")
    sessions_js = _read("js/sessions.js")
    keyboard_js = _read("js/keyboard-shortcuts.js")
    manifest = _read("manifest.json")

    assert "Message Circit AI..." in app_js
    assert "Circit AI Chat" in app_js
    assert "Circit AI Chat" in sessions_js
    assert "Circit AI Chat" in keyboard_js
    assert '"name": "Circit AI"' in manifest
    assert '"short_name": "Circit AI"' in manifest

    assert "Message Odysseus" not in app_js
    assert "Odysseus Chat" not in app_js
    assert "Odysseus Chat" not in sessions_js
    assert "Odysseus Chat" not in keyboard_js


def test_login_critical_shell_uses_circit_brand_and_basis_font():
    html = _read("login.html")
    assert "<title>Circit AI \u2014 Login</title>" in html
    assert "/static/vendor/circit-ds/circit-tokens.css" in html
    assert "/static/vendor/circit-ds/circit-wordmark.svg" in html
    assert "/static/vendor/circit-ds/circit-favicon.jpg" in html
    assert "<span>AI</span>" in html
    assert "Basis Grotesque Pro" in html
    assert "<span>Odysseus</span>" not in html
    assert "<title>Odysseus" not in html


def test_theme_defaults_to_circit_palette_and_basis_font():
    theme = _read("js/theme.js")
    assert "circit:" in theme
    assert "const DEFAULT_THEME = 'circit';" in theme
    assert "const DEFAULT_FONT = 'sans';" in theme
    assert "Basis Grotesque Pro" in theme
    assert "const DEFAULT_THEME = 'dark';" not in theme


def test_vendored_circit_fonts_exist():
    token_css = _read("vendor/circit-ds/circit-tokens.css")
    font_paths = re.findall(r'url\("(/static/vendor/circit-ds/fonts/[^"]+\.woff2)"\)', token_css)
    assert len(font_paths) >= 5
    for public_path in font_paths:
        disk_path = STATIC / public_path.removeprefix("/static/")
        assert disk_path.exists(), public_path


def test_vendored_circit_logo_assets_exist():
    assert (STATIC / "vendor/circit-ds/circit-wordmark.svg").exists()
    assert (STATIC / "vendor/circit-ds/circit-favicon.jpg").exists()
