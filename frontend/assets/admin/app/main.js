// PH7-16 Wave 0B — application shell entry point.
// The single `<script type="module">` tag in index.html. Composition
// root: dynamically imports kernel.js (side effect only -- wires the
// modal-overlay dismiss-on-backdrop-click handler) and auth.js, then
// dynamically imports router.js and registers its exported `bootstrap`
// as auth.js's post-authentication callback, before finally kicking off
// the same session-restore flow router.js's own trailing
// `restoreAdminSession();` call used to trigger directly.
//
// PH7-16 Wave 6: router.js is what `../../admin.js` was renamed to once
// Waves 1-5 had emptied every domain screen out of it -- it now lives
// alongside this file as the fourth `admin/app/` shell module (kernel/
// api/auth/router), matching the target file tree PH7-16 was scoped
// against. There is no file named `admin.js` left anywhere in the tree.
//
// Single source of truth for cache-busting: the `?v=` query string on
// *this* file's own <script src> in index.html. `import.meta.url` reads
// it back once here, and every dynamic import() this shell (and
// router.js, and auth.js internally) performs propagates the exact same
// string -- see the comments in router.js and auth.js for why that
// propagation is load-bearing, not cosmetic, given the server's real
// `Cache-Control: public, max-age=3600` on JS assets.
const MODULE_VERSION = new URL(import.meta.url).search;

function showStartupFailure(){
  const loginError=document.getElementById('login-err');
  if(loginError){loginError.textContent='Модуль администрирования недоступен. Обновите страницу или повторите позже.';loginError.style.display='block';}
  const app=document.getElementById('app');
  if(app){
    app.style.display='flex';
    const main=document.getElementById('main');
    if(main){const notice=document.createElement('div');notice.className='notice notice-amber module-unavailable';notice.textContent='Модуль администрирования недоступен. Обновите страницу или повторите позже.';main.prepend(notice);}
  }
}

try{
  await import(`./kernel.js${MODULE_VERSION}`);
  const {onAuthenticated, restoreAdminSession} = await import(`./auth.js${MODULE_VERSION}`);
  const {bootstrap} = await import(`./router.js${MODULE_VERSION}`);
  onAuthenticated(async()=>{
    try{await bootstrap();}
    catch(error){console.warn('admin bootstrap unavailable',error);showStartupFailure();}
  });
  await restoreAdminSession();
}catch(error){
  console.warn('admin startup unavailable',error);
  showStartupFailure();
}
