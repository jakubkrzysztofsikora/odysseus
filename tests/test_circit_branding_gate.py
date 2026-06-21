"""Static deployment gate for the Odysseus-branded public surfaces."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _read(path: str) -> str:
    return (STATIC / path).read_text(encoding="utf-8")


def test_index_critical_shell_uses_odysseus_brand_and_tokens():
    html = _read("index.html")
    assert "<title>Odysseus</title>" in html
    assert "/static/vendor/circit-ds/circit-tokens.css" in html
    assert "/static/vendor/circit-ds/odysseus-wordmark.svg" in html
    assert "/static/vendor/circit-ds/odysseus-favicon.svg" in html
    assert '<h1 class="a11y-visually-hidden">Odysseus</h1>' in html
    assert 'id="current-meta">Odysseus<' in html
    assert "Message Odysseus..." in html
    assert "<span>Circit AI</span>" not in html
    assert "Message Circit AI" not in html
    assert "Circit AI Chat" not in html


def test_runtime_visible_fallbacks_use_odysseus_brand():
    app_js = _read("app.js")
    sessions_js = _read("js/sessions.js")
    keyboard_js = _read("js/keyboard-shortcuts.js")
    manifest = _read("manifest.json")

    assert "Message Odysseus" in app_js
    assert "Odysseus Chat" in app_js
    assert "Odysseus Chat" in sessions_js
    assert "Odysseus Chat" in keyboard_js
    assert '"name": "Odysseus"' in manifest
    assert '"short_name": "Odysseus"' in manifest

    assert "Message Circit AI" not in app_js
    assert "Circit AI Chat" not in app_js
    assert "Circit AI Chat" not in sessions_js
    assert "Circit AI Chat" not in keyboard_js


def test_login_critical_shell_uses_odysseus_brand_and_basis_font():
    html = _read("login.html")
    assert "<title>Odysseus — Login</title>" in html
    assert "/static/vendor/circit-ds/circit-tokens.css" in html
    assert "/static/vendor/circit-ds/odysseus-wordmark.svg" in html
    assert "/static/vendor/circit-ds/odysseus-favicon.svg" in html
    assert "Basis Grotesque Pro" in html
    assert "<span>Circit AI</span>" not in html
    assert "<title>Circit AI" not in html


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
