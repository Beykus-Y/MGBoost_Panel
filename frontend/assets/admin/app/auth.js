// PH7-16 Wave 0B — session bootstrap/login/logout.
// ES module. `bootstrap()` (the post-auth app init) lives in the
// application router/composition-root module, not here (PH7-16 Wave 6:
// that module is `admin/app/router.js`, a sibling of this file -- rather
// than an auth.js -> router.js import (which would make a real import
// cycle with router.js -> auth.js), this file exposes one explicit
// registration hook, onAuthenticated(), that main.js calls once, after
// dynamically importing router.js, to wire router.js's exported
// `bootstrap` as the callback run after a successful login or session
// restore. Same one-directional-import discipline as the
// onUnauthorizedSession() seam in api.js.
//
// api.js is loaded via a versioned dynamic import(), not a static one:
// a static `import ... from './api.js'` specifier does NOT inherit this
// file's own `?v=` query string, so under the server's real
// `Cache-Control: public, max-age=3600` for JS assets a stale-cached
// api.js could silently pair with a freshly-deployed auth.js. Propagating
// `import.meta.url`'s own query (which this file only has because *it*
// was itself reached via a versioned dynamic import -- see main.js) keeps
// every module in one deploy on the same cache-busted URL, so they all
// resolve to the exact same singleton instance everywhere they're loaded
// from (main.js, router.js, here).
const _MODULE_VERSION = new URL(import.meta.url).search;
const { PROXY_API, setCsrfToken, adminFetch, onUnauthorizedSession } =
  await import(`./api.js${_MODULE_VERSION}`);

let authenticatedCallback = () => {};
export function onAuthenticated(callback){
  authenticatedCallback = callback;
}

export async function doLogin(){
  const u=document.getElementById('login-user').value.trim();
  const p=document.getElementById('login-pass').value;
  const e=document.getElementById('login-err');
  e.style.display='none';
  try{
    const r=await fetch(PROXY_API+'/admin/session/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-MGBoost-Admin-Login':'1'},body:JSON.stringify({username:u,password:p})});
    const d=await r.json();
    if(!r.ok||!d.authenticated||!d.csrf_token)throw new Error();
    setCsrfToken(d.csrf_token);
    document.getElementById('login-pass').value='';
    document.getElementById('sidebar-admin').textContent=d.username;
    document.getElementById('login-page').style.display='none';
    document.getElementById('app').style.display='flex';
    await authenticatedCallback();
  }catch{e.style.display='block'}
}
export function showLoggedOut(){
  setCsrfToken('');
  document.getElementById('app').style.display='none';
  document.getElementById('login-page').style.display='flex';
}
export async function doLogout(){
  try{
    await adminFetch('/admin/session/logout',{method:'POST'});
  }catch{}
  showLoggedOut();
}
export async function restoreAdminSession(){
  try{
    const r=await fetch(PROXY_API+'/admin/session',{credentials:'same-origin',headers:{Accept:'application/json'}});
    if(!r.ok)throw new Error();
    const d=await r.json();
    if(!d.authenticated||!d.csrf_token)throw new Error();
    setCsrfToken(d.csrf_token);
    document.getElementById('sidebar-admin').textContent=d.username;
    document.getElementById('login-page').style.display='none';
    document.getElementById('app').style.display='flex';
    await authenticatedCallback();
  }catch{showLoggedOut()}
}

onUnauthorizedSession(showLoggedOut);

document.getElementById('login-pass').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});
