import io
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from src.routes.panel import handle_panel


ROOT = Path(__file__).resolve().parents[1]
ADMIN_MODULES = ROOT / "frontend" / "assets" / "admin"
ADMIN_APP = ROOT / "frontend" / "assets" / "admin" / "app"
ROUTER_JS = ADMIN_APP / "router.js"
ADMIN_HTML = ROOT / "frontend" / "index.html"

# PH7-16 Wave 0A moved the shared kernel/api/auth primitives out of the
# admin.js monolith into these files; Wave 0B converted them (and the
# monolith itself, dynamically import()-ed by admin/app/main.js) into real
# ES modules with explicit import/export; Wave 6 moved/renamed the
# monolith (once Waves 1-5 had emptied every domain screen out of it) to
# `admin/app/router.js` -- there is no file named `admin.js` left anywhere
# in the tree. Tests below load these in the same dependency order the
# real page does (kernel -> api -> auth -> router.js).
ADMIN_SHELL_FILES = [ADMIN_APP / "kernel.js", ADMIN_APP / "api.js", ADMIN_APP / "auth.js"]


def _admin_shell_source():
    return "\n".join(path.read_text(encoding="utf-8") for path in ADMIN_SHELL_FILES)


def _strip_module_syntax(source):
    """Turn ES module import/export/top-level-await syntax into plain
    script syntax.

    Only used for the Node `vm.runInContext` sandbox below, which executes
    plain scripts, not linked ES modules (and does not support top-level
    `await` outside an async wrapper). This does not touch any actual
    logic:
    - every static `import {...} from './x.js';` line is dropped (its
      targets are already inlined earlier, in dependency order, by the
      caller);
    - every `const {...} = await import(...)` / bare `await import(...)`
      statement (PH7-16 Wave 0B's versioned-dynamic-import pattern for
      kernel.js/api.js/auth.js) is dropped the same way, for the same
      reason -- the names it would have destructured are already defined
      by the earlier-inlined source;
    - a leading `export ` on a declaration is stripped (the declaration
      itself, e.g. `async function bootstrap(){...}`, is unchanged).

    Caller contract: run this ONLY after any block-specific regex
    substitution that still needs to match the original, un-stripped
    `await import(...)` text (e.g. the ACCOUNT_UI_READY/PROMO_OPS_READY
    stubbing below) -- this function is not selective about *which*
    await-import statement it drops.
    """
    source = re.sub(r"^import\s+.*?;\s*$", "", source, flags=re.MULTILINE)
    # `import.meta` is only valid syntax inside a real module goal --
    # `_MODULE_VERSION` is unused once the await-import lines that
    # consumed it are stripped below, so an empty-string stand-in is fine.
    # `var` (not `const`) because admin.js's own `_MODULE_VERSION` stand-in
    # (substituted separately, above, before this function ever runs) is
    # also declared at the same concatenated top level -- `const`/`const`
    # would collide as a duplicate declaration once both files' sources
    # are joined into one script.
    source = re.sub(
        r"const _MODULE_VERSION = new URL\(import\.meta\.url\)\.search;",
        "var _MODULE_VERSION='';",
        source,
    )
    source = re.sub(
        r"(?:const|let)\s*\{[^}]*\}\s*=\s*await\s+import\(.*?\);",
        "",
        source,
        flags=re.DOTALL,
    )
    source = re.sub(r"^\s*await\s+import\(.*?\);\s*$", "", source, flags=re.MULTILINE)
    source = re.sub(
        r"^export\s+(?=(?:async\s+function|function|class|const|let)\b)",
        "",
        source,
        flags=re.MULTILINE,
    )
    return source


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
    js_source = ROUTER_JS.read_text(encoding="utf-8")
    shell_source = _admin_shell_source()
    # App shell files are already included through shell_source/js_source;
    # recursively include every remaining domain module, including nested
    # technical/payments/support/operations directories.
    module_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in ADMIN_MODULES.rglob("*.js")
        if ADMIN_APP not in path.parents
    )
    combined = html_source + "\n" + shell_source + "\n" + js_source + "\n" + module_source

    assert not re.search(r"\son[a-z]+\s*=", combined, re.IGNORECASE)
    # renderHtml's single legitimate `.innerHTML=` sink now lives in
    # kernel.js (PH7-16 Wave 0A); router.js itself must not reintroduce one.
    assert js_source.count(".innerHTML=") == 0
    assert shell_source.count(".innerHTML=") == 1
    assert "template.innerHTML=markup.value" in shell_source
    assert "localStorage.setItem" not in combined
    assert "Authorization" not in combined
    assert "data-action" in combined
    assert "20260902-adminv2-catalog" in html_source

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
    assert "row.hwid_verifier" not in account_source
    assert "row.uuid_verifier" not in account_source
    assert "technicalField('HWID verifier'" not in account_source
    assert "technicalField('Credential verifier'" not in account_source
    assert "<details class=\"technical-generation\"" in account_source
    assert "Лимит устройств" in account_source


def test_canonical_applied_stars_payment_is_refundable_and_humanized():
    source = (ADMIN_MODULES / "payments" / "stars_legacy.js").read_text(encoding="utf-8")
    assert "canonical_applied:'badge-green'" in source
    assert "canonical_applied:'Применён к аккаунту'" in source
    refundable = re.search(r"const _STARS_REFUNDABLE=new Set\(\[([^]]+)", source)
    assert refundable and "'canonical_applied'" in refundable.group(1)


def test_transition_reason_prompt_cancel_and_invalid_input_return_before_request():
    source = (ADMIN_MODULES / "payments.js").read_text(encoding="utf-8")
    assert source.count("if(prompted===null)return;") == 2
    assert source.count("if(reason.length<8||reason.length>300)") == 2


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
    # PH7-16 Wave 0B: kernel.js/api.js/auth.js/router.js are all real ES
    # modules now (router.js is dynamically import()-ed by admin/app/main.js
    # -- see index.html; Wave 6 renamed it here from the top-level
    # `admin.js` it used to be). `vm.runInContext` below runs a plain
    # script, not linked ES modules and without top-level await, so: (1)
    # first replace the accounts.js/routing.js/promo_ops.js dynamic-import
    # blocks with stubs while their exact original text still matches
    # these regexes, (2) only then run _strip_module_syntax to drop every
    # remaining import/export/await-import wrapper -- doing it in the
    # other order would let the generic await-import stripper mangle these
    # blocks before the stubbing regexes below get a chance to match them.
    # The escaping/rendering logic under test is untouched either way.
    js_source = ROUTER_JS.read_text(encoding="utf-8")
    js_source = re.sub(
        # `import.meta.url` is only valid syntax inside a real module goal
        # -- vm.runInContext parses as a plain script -- so this reference
        # and the three PH7-16 Wave 0B kernel/api/auth dynamic-import lines
        # that consume it must go too. Their destructured names (html,
        # renderHtml, toast, closeModal, api, proxyApi, adminFetch,
        # doLogin, doLogout) are already provided by the concatenated
        # shell source that precedes router.js's source below -- this must
        # NOT eat the following `let allUsers = [];` etc. block, hence the
        # precise (non-greedy-free) match instead of spanning to
        # ACCOUNT_UI_READY.
        r"const _MODULE_VERSION = new URL\(import\.meta\.url\)\.search;\n"
        r"const \{html,renderHtml,toast,closeModal\} = await import\(`\./kernel\.js\$\{_MODULE_VERSION\}`\);\n"
        r"const \{api,proxyApi,adminFetch\} = await import\(`\./api\.js\$\{_MODULE_VERSION\}`\);\n"
        r"const \{doLogin,doLogout\} = await import\(`\./auth\.js\$\{_MODULE_VERSION\}`\);\n",
        "var _MODULE_VERSION='';\n",
        js_source,
    )
    js_source = re.sub(
        r"let marzbanUsersUi = null;.*?window\.__PROMO_OPS_READY=PROMO_OPS_READY;",
        """let marzbanUsersUi=null,ticketsUi=null,starsUi=null,configsUi=null,settingsUi=null,promoOps=null;
const ACCOUNT_UI_READY=Promise.resolve(null),MARZBAN_USERS_UI_READY=Promise.resolve(null),
TICKETS_UI_READY=Promise.resolve(null),STARS_UI_READY=Promise.resolve(null),
LEGACY_TRANSITIONS_READY=Promise.resolve(null),ROUTING_UI_READY=Promise.resolve(null),
NODES_UI_READY=Promise.resolve(null),OPS_HEALTH_READY=Promise.resolve(null),
CONFIGS_UI_READY=Promise.resolve(null),SETTINGS_UI_READY=Promise.resolve(null),PROMO_OPS_READY=Promise.resolve(null);
window.__PROMO_OPS_READY=PROMO_OPS_READY;""",
        js_source,
        flags=re.DOTALL,
    )
    # PH7-16 Wave 5 moved renderUsers out of the monolith into
    # admin/technical/marzban_users.js's createMarzbanUsersUi() factory --
    # the escaping logic under test (kernel.js's html/renderHtml/esc) is
    # unchanged, but exercising it now requires instantiating that factory
    # too, the same way router.js itself does at runtime.
    marzban_users_source = _strip_module_syntax(
        (ADMIN_MODULES / "technical" / "marzban_users.js").read_text(encoding="utf-8")
    )
    js_source = (
        _strip_module_syntax(_admin_shell_source())
        + "\n"
        + _strip_module_syntax(js_source)
        + "\n"
        + marzban_users_source
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
const marzbanUsers=sandbox.createMarzbanUsersUi({{
  html:sandbox.html,renderHtml:(_element,markup)=>{{captured=markup.value;}},toast(){{}},closeModal(){{}},
  api:async()=>({{ok:false}}),proxyApi:async()=>({{ok:false}}),promptReason:()=>null,
  getAllUsers:()=>[],setAllUsers(){{}},getAllNodes:()=>[],getAllInbounds:()=>({{}}),setAllInbounds(){{}},
  getNodeFilters:()=>({{}}),setNodeFilters(){{}},
  marzbanStatusBadgeClass:s=>({{active:'badge-green',disabled:'badge-red',expired:'badge-red',limited:'badge-amber',on_hold:'badge-gray'}}[s]||'badge-gray')
}});
marzbanUsers.renderUsers([{{
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
