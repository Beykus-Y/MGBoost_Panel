import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.routes.panel import handle_panel


ROOT = Path(__file__).resolve().parents[1]
ADMIN_JS = ROOT / "frontend" / "assets" / "admin.js"
ADMIN_MODULES = ROOT / "frontend" / "assets" / "admin"
ADMIN_HTML = ROOT / "frontend" / "index.html"


class FakeHandler:
    def __init__(self):
        self.wfile = io.BytesIO()
        self.status = None
        self.response_headers = []

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        pass

    def header(self, name):
        values = [value for key, value in self.response_headers if key.lower() == name.lower()]
        return values[-1] if values else None


def test_admin_sources_have_no_inline_handlers_or_unsafe_dynamic_sinks():
    html_source = ADMIN_HTML.read_text(encoding="utf-8")
    js_source = ADMIN_JS.read_text(encoding="utf-8")
    module_source = "\n".join(path.read_text(encoding="utf-8") for path in ADMIN_MODULES.glob("*.js"))
    combined = html_source + "\n" + js_source + "\n" + module_source

    assert not re.search(r"\son[a-z]+\s*=", combined, re.IGNORECASE)
    assert js_source.count(".innerHTML=") == 1
    assert "template.innerHTML=markup.value" in js_source
    assert "localStorage.setItem" not in js_source
    assert "Authorization" not in js_source
    assert "data-action" in combined
    assert "20260826-wave-a" in html_source

    actions = set(re.findall(r'data-action="([a-z0-9-]+)"', combined))
    actions.update({"enable-user", "disable-user"})
    handled_actions = set(re.findall(r"case'([a-z0-9-]+)'", js_source))
    handled_actions.update(re.findall(r"dataset\.action==='([a-z0-9-]+)'", module_source))
    assert actions <= handled_actions

    change_actions = set(re.findall(r'data-change-action="([a-z0-9-]+)"', combined))
    assert change_actions <= handled_actions


def test_account_human_surfaces_use_reusable_russian_labels_and_separated_technical_fields():
    html_source = ADMIN_HTML.read_text(encoding="utf-8")
    core_source = (ADMIN_MODULES / "core.js").read_text(encoding="utf-8")
    account_source = (ADMIN_MODULES / "accounts.js").read_text(encoding="utf-8")
    for english_tab in (">Overview<", ">Subscription<", ">Devices<", ">Technical<"):
        assert english_tab not in html_source
    for raw, label in (
        ("ACTIVE:'Активен'", "Активен"),
        ("UNLIMITED:'Безлимит'", "Безлимит"),
        ("BOUND:'Привязан'", "Привязан"),
        ("UNREGISTERED:'Не привязан'", "Не привязан"),
        ("PARENT_READY:'Готов'", "Готов"),
        ("NO_LINEAGE:'Нет миграции'", "Нет миграции"),
    ):
        assert raw in core_source, label
    assert "humanLabel(value" in core_source
    assert "slot_generation_id=" not in account_source
    assert "technicalField('Slot generation ID'" in account_source
    assert "technicalField('Child intent ID'" in account_source
    assert "<details class=\"technical-generation\"" in account_source
    assert "Лимит устройств" in account_source


def test_admin_page_enforces_script_csp_and_legacy_storage_cleanup():
    handler = FakeHandler()
    handle_panel(handler)

    assert handler.status == 200
    csp = handler.header("Content-Security-Policy")
    assert "script-src 'self'" in csp
    assert "script-src 'self' 'unsafe-inline'" not in csp
    assert "object-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert handler.header("Cache-Control") == "no-store"
    assert handler.header("Clear-Site-Data") == '"storage"'
    assert handler.header("Referrer-Policy") == "no-referrer"
    assert handler.header("X-Frame-Options") == "DENY"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required for admin JS security test")
def test_malicious_admin_api_values_are_escaped_by_real_render_path():
    js_source = ADMIN_JS.read_text(encoding="utf-8")
    js_source = re.sub(
        r"const ACCOUNT_UI_READY = import\('./admin/accounts\.js'\)\.then\(module=>\{.*?\n\}\);",
        "const ACCOUNT_UI_READY=Promise.resolve(null);",
        js_source,
        flags=re.DOTALL,
    )
    payload = "<img src=x onerror=globalThis.__xss=1>'\\\"&"
    script = f"""
const vm=require('vm');
const source={json.dumps(js_source)};
const element={{style:{{}},classList:{{add(){{}},remove(){{}}}},addEventListener(){{}},replaceChildren(){{}}}};
const document={{
  getElementById(){{return element;}},
  querySelectorAll(){{return[];}},
  querySelector(){{return element;}},
  addEventListener(){{}},
  createElement(){{return{{content:{{cloneNode(){{return{{}};}}}},set innerHTML(v){{this._html=v;}}}};}}
}};
const sandbox={{document,window:{{}},localStorage:{{removeItem(){{}}}},fetch:async()=>({{ok:false,status:401,json:async()=>({{}})}}),console,setTimeout,clearTimeout,Date,Promise,confirm:()=>false,prompt:()=>null,alert(){{}},globalThis:null}};
sandbox.globalThis=sandbox;
vm.createContext(sandbox);
vm.runInContext(source,sandbox);
let captured='';
sandbox.renderHtml=(_element,markup)=>{{captured=markup.value;}};
sandbox.renderUsers([{{
  username:{json.dumps(payload)}, note:{json.dumps(payload)}, sub_last_user_agent:{json.dumps(payload)},
  status:'active',used_traffic:0,data_limit:null,expire:null,online_at:null
}}]);
if(captured.includes('<img')||sandbox.__xss!==undefined)process.exit(2);
if(!captured.includes('&lt;img')||!captured.includes('&quot;')||!captured.includes('&#39;')||!captured.includes('&amp;'))process.exit(3);
"""
    completed = subprocess.run(
        [shutil.which("node"), "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
