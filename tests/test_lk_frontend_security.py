import html
import re
from html.parser import HTMLParser
from pathlib import Path

from src.routes.sub import _browser_page


ROOT = Path(__file__).resolve().parents[1]
LK_JS = ROOT / "frontend" / "assets" / "lk.js"
LK_HTML = ROOT / "frontend" / "lk.html"
BROWSER_HTML = ROOT / "frontend" / "browser_page.html"
BROWSER_JS = ROOT / "frontend" / "assets" / "browser_page.js"


class _DisplayTextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self._inside_display = False
        self.text = ""
        self.images = 0

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images += 1
        if tag == "div" and dict(attrs).get("id") == "url-display":
            self._inside_display = True

    def handle_endtag(self, tag):
        if tag == "div" and self._inside_display:
            self._inside_display = False

    def handle_data(self, data):
        if self._inside_display:
            self.text += data


def test_lk_has_no_inline_handlers_or_html_string_sinks():
    js = LK_JS.read_text(encoding="utf-8")
    html_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (LK_HTML, BROWSER_HTML)
    )
    combined = html_sources + "\n" + js + "\n" + BROWSER_JS.read_text(encoding="utf-8")

    assert not re.search(r"\son[a-z]+\s*=", combined, re.IGNORECASE)
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in js
    assert "textContent" in js
    assert "addEventListener" in js
    assert "dataset.deviceId" in js
    assert "normalizeDeviceId" in js
    assert "escHtml" not in js


def test_browser_subscription_page_uses_external_script_and_text_node():
    template = BROWSER_HTML.read_text(encoding="utf-8")
    assert "__SUB_URL_JSON__" not in template
    assert '<script src="/assets/browser_page.js?v=ph2-02" defer></script>' in template
    assert not re.search(r"<script(?:\s[^>]*)?>\s*[^<\s]", template, re.IGNORECASE)

    malicious_url = "https://example.test/sub/x?</div><img src=x onerror=alert(1)>&q='\"\\"
    rendered = _browser_page(malicious_url).decode("utf-8")
    parser = _DisplayTextParser()
    parser.feed(rendered)
    assert parser.images == 0
    assert html.unescape(parser.text) == malicious_url
    assert malicious_url not in rendered


def test_device_actions_use_validated_opaque_ids_not_names():
    js = LK_JS.read_text(encoding="utf-8")
    assert "renameDevice(${" not in js
    assert "deleteDevice(${" not in js
    assert "dataset.deviceId = deviceId" in js
    assert "dataset.action = 'rename-device'" in js
    assert "dataset.action = 'delete-device'" in js
    assert "renameDevice(id, name)" in js
