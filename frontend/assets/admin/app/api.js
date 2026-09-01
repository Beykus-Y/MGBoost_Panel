// PH7-16 Wave 0B — shared admin fetch client (CSRF/cookies/401 handling).
// ES module. `csrfToken` used to be a bare top-level `let` other classic
// scripts mutated directly by name; modules don't allow assigning to an
// imported binding, so it is now private to this file behind explicit
// getCsrfToken/setCsrfToken exports. Likewise, this file must not import
// auth.js (that would be a real circular import: auth.js already needs
// adminFetch/PROXY_API from here) -- instead it exposes one explicit,
// documented registration hook, onUnauthorizedSession(), that auth.js
// calls once at load to wire its own showLoggedOut() as the 401 handler.
// This is the one intentional compatibility seam Wave 0B introduces; it
// replaces the old implicit "adminFetch calls the global showLoggedOut()"
// classic-script behavior with an explicit, single-purpose callback.
export const PROXY_API = '/sub-admin-api';

let csrfToken = '';
let unauthorizedHandler = () => {};

export function getCsrfToken(){
  return csrfToken;
}
export function setCsrfToken(token){
  csrfToken = token;
}
export function onUnauthorizedSession(handler){
  unauthorizedHandler = handler;
}

export async function api(path,opts={}){
  return adminFetch('/admin/marzban'+path,opts);
}
export async function proxyApi(path,opts={}){
  return adminFetch(path,opts);
}
export async function adminFetch(path,opts={}){
  const method=(opts.method||'GET').toUpperCase();
  const headers={'Content-Type':'application/json',...(opts.headers||{})};
  if(!['GET','HEAD','OPTIONS'].includes(method)&&csrfToken)headers['X-CSRF-Token']=csrfToken;
  const r=await fetch(PROXY_API+path,{...opts,method,credentials:'same-origin',headers});
  if(r.status===401){unauthorizedHandler();throw new Error('unauth')}
  return r;
}

// PH7-16 Wave 1: was independently redefined, byte-for-byte identical
// except for JSON-parse-failure handling, in both admin/accounts.js and
// admin/routing.js. Canonicalized here on the more defensive of the two
// (routing.js's `.catch(()=>({}))`) -- strictly safer, since it only
// changes behavior on a response whose body fails to parse as JSON at
// all, converting an uncaught SyntaxError into the same `request failed`
// error every caller already handles for a non-ok response.
export async function getJson(path,opts={}){
  const response=await adminFetch(path,opts);
  const data=await response.json().catch(()=>({}));
  if(!response.ok)throw new Error(data.error||'request failed');
  return data;
}

// Legacy builds stored the Marzban JWT here.  Remove it before any request;
// authentication now uses only an HttpOnly server-side session cookie.
localStorage.removeItem('mz_token');
