// PH7-16 Wave 6 -- this file is the former `frontend/assets/admin.js`
// (the original 1739-line monolith that every canonical `admin/*.js`
// domain module used to be dynamically `import()`-ed into), moved here
// verbatim and renamed once Waves 1-5 had emptied every domain screen out
// of it. What's left is exactly what the name says: the application's
// router (showPage/switchTab/the click-change-input dispatch tables) plus
// the composition root that wires every domain module's factory together
// (the *_UI_READY block below) and `bootstrap()`, the post-login entry
// point. There is no more file anywhere named `admin.js` -- per PH7-16's
// scope (ROADMAP.md), "the legacy monolith" is retired as a concept, not
// just thinned out. Loaded as an ES module, dynamically `import()`-ed by
// this same directory's main.js. `bootstrap` is exported so main.js can
// register it as the post-login/session-restore callback (auth.js
// exposes onAuthenticated() for exactly that, to avoid an
// auth.js <-> router.js import cycle).
//
// kernel.js/api.js/auth.js (siblings in this same admin/app/ directory)
// and every admin/*.js domain module are loaded via versioned dynamic
// import(), same reasoning as before the move: a static
// `import ... from './kernel.js'` specifier would not inherit this file's
// own `?v=` query, and the server sends a real
// `Cache-Control: public, max-age=3600` for JS assets -- without
// propagating the version explicitly, a stale-cached kernel/api/auth
// could silently pair with a freshly-deployed router.js after a release.
// `import.meta.url` reflects the exact versioned specifier main.js used
// to import this module, so re-deriving `_MODULE_VERSION` from it here
// and reusing it for every dynamic import below (shell siblings and the
// canonical admin/*.js modules alike) keeps the whole graph on one
// deploy's URLs -- and, since the browser's module registry is keyed by
// resolved URL, every one of these dynamic imports converges on the exact
// same singleton kernel/api/auth instances main.js itself is using.
const _MODULE_VERSION = new URL(import.meta.url).search;
const {html,renderHtml,toast,closeModal} = await import(`./kernel.js${_MODULE_VERSION}`);
const {api,proxyApi,adminFetch,getJson} = await import(`./api.js${_MODULE_VERSION}`);
const {doLogin,doLogout} = await import(`./auth.js${_MODULE_VERSION}`);

let allUsers = [];
let allNodes = [];
// PH7-16 Wave 2: admin/operations/nodes.js needs the same `allUsers`/
// `allNodes` caches the legacy Users screen and Dashboard already read
// and write here -- rather than give it its own independently-cached
// copy (the exact class of drift risk Wave 1 closed for getJson), it
// receives explicit getter/setter access to this file's single source of
// truth for both.
function getAllUsers(){return allUsers;}
function setAllUsers(list){allUsers=list;}
function getAllNodes(){return allNodes;}
function setAllNodes(list){allNodes=list;}
let nodeFilters = {};
// PH7-16 Wave 5: admin/technical/marzban_users.js shares this with
// bootstrap()'s initial fetch -- same accessor-DI discipline as
// allNodes/allUsers/allInbounds.
function getNodeFilters(){return nodeFilters;}
function setNodeFilters(f){nodeFilters=f;}
// PH7-16 Wave H closure (mandatory per Wave 5 instructions): the raw
// Marzban proxy's destructive operations and the Stars refund/reconcile-
// refund handlers now require a primary-admin capability + mandatory
// reason (Wave H). This is the one shared UI primitive both relocated
// screens (marzban_users.js, stars_legacy.js) use to collect that reason
// -- defined once here (the composition root) instead of duplicated in
// each file.
function promptReason(message){
  const reason=(window.prompt(message)||'').trim();
  if(reason.length<3||reason.length>300){toast('Причина обязательна (3–300 символов)','err');return null;}
  return reason;
}
let accountUi = null;
let routingUi = null;
let nodesUi = null;
let opsHealthUi = null;
let legacyTransitionsUi = null;
let marzbanUsersUi = null;
let ticketsUi = null;
let starsUi = null;
function showModuleUnavailable(pageName,label){
  const page=document.getElementById(`page-${pageName}`);
  if(!page||page.querySelector('.module-unavailable'))return;
  const notice=document.createElement('div');
  notice.className='notice notice-amber module-unavailable';
  notice.textContent=`Модуль недоступен: ${label}. Обновите страницу или повторите позже.`;
  page.prepend(notice);
}
function trackModule(label,pageName,promise){
  return promise.catch(error=>{
    console.warn(`admin module unavailable: ${label}`,error);
    showModuleUnavailable(pageName,label);
    return null;
  });
}
function withModule(ready,label,pageName,callback){
  return ready.then(ui=>{
    if(!ui){showModuleUnavailable(pageName,label);return;}
    return callback(ui);
  });
}
const MARZBAN_USERS_UI_READY = trackModule('Пользователи Marzban','users',(async()=>{
  const marzbanCore=await import(`../core.js${_MODULE_VERSION}`);
  const module=await import(`../technical/marzban_users.js${_MODULE_VERSION}`);
  marzbanUsersUi=module.createMarzbanUsersUi({html,renderHtml,toast,closeModal,api,proxyApi,promptReason,
    getAllUsers,setAllUsers,getAllNodes,getAllInbounds,setAllInbounds,getNodeFilters,setNodeFilters,
    marzbanStatusBadgeClass:marzbanCore.marzbanStatusBadgeClass});
  return marzbanUsersUi;
})());
const TICKETS_UI_READY = trackModule('Тикеты','tickets',import(`../support/tickets.js${_MODULE_VERSION}`).then(module=>{
  ticketsUi=module.createTicketsUi({html,renderHtml,closeModal,proxyApi});
  return ticketsUi;
}));
const STARS_UI_READY = trackModule('Telegram Stars','stars',import(`../payments/stars_legacy.js${_MODULE_VERSION}`).then(module=>{
  starsUi=module.createStarsLegacyUi({html,renderHtml,promptReason,proxyApi});
  return starsUi;
}));
const ACCOUNT_UI_READY = trackModule('Аккаунты','accounts',import(`../accounts.js${_MODULE_VERSION}`).then(module=>{
  accountUi=module.createAccountUi({adminFetch,getJson,html,renderHtml,showPage,toast});
  return accountUi;
}));
// PH7-16 Wave 3: the Operations queue reuses accountUi's own `payments`
// instance (openLegacyTransitionById) instead of a second createPayments()
// -- one transition-modal implementation, two entry points.
const LEGACY_TRANSITIONS_READY = trackModule('Легаси-переходы','legacy-transitions',ACCOUNT_UI_READY.then(async readyAccountUi=>{
  if(!readyAccountUi)throw new Error('Accounts module dependency unavailable');
  const coreModule=await import(`../core.js${_MODULE_VERSION}`);
  const module=await import(`../legacy_transitions.js${_MODULE_VERSION}`);
  legacyTransitionsUi=module.createLegacyTransitionsQueue({html,renderHtml,toast,adminFetch,getJson,
    formatTimestamp:coreModule.formatTimestamp,humanLabel:coreModule.humanLabel,
    openLegacyTransitionById:readyAccountUi.payments.openLegacyTransitionById,openAccount:readyAccountUi.openAccount});
  return legacyTransitionsUi;
}));
const ROUTING_UI_READY = trackModule('Роутинг хостов','routing',(async()=>{
  const routingCore=await import(`../core.js${_MODULE_VERSION}`);
  const module=await import(`../routing.js${_MODULE_VERSION}`);
  routingUi=module.createRoutingUi({adminFetch,getJson,html,renderHtml,toast,
    hostClassBadgeClass:routingCore.hostClassBadgeClass});
  return routingUi;
})());
const NODES_UI_READY = trackModule('Ноды','nodes',(async()=>{
  const nodesCore=await import(`../core.js${_MODULE_VERSION}`);
  const module=await import(`../operations/nodes.js${_MODULE_VERSION}`);
  nodesUi=module.createNodesUi({html,renderHtml,toast,closeModal,api,proxyApi,fmt,fmtMoney,getTrafficPeriod,getAllNodes,setAllNodes,getAllUsers,setAllUsers,
    nodeImportanceBadgeClass:nodesCore.nodeImportanceBadgeClass});
  return nodesUi;
})());
const OPS_HEALTH_READY = trackModule('Здоровье','ops-health',(async()=>{
  const opsCore=await import(`../core.js${_MODULE_VERSION}`);
  const module=await import(`../ops_health.js${_MODULE_VERSION}`);
  opsHealthUi=module.createOpsHealth({html,renderHtml,toast,getJson,
    formatTimestamp:opsCore.formatTimestamp,formatDuration:opsCore.formatDuration,
    healthBadgeClass:opsCore.healthBadgeClass});
  return opsHealthUi;
})());
let configsUi=null;
const CONFIGS_UI_READY = trackModule('Доп. конфиги','configs',import(`../technical/configs.js${_MODULE_VERSION}`).then(module=>{
  configsUi=module.createConfigsUi({html,renderHtml,toast,api,proxyApi,getAllInbounds,setAllInbounds});
  return configsUi;
}));
let settingsUi=null;
const SETTINGS_UI_READY = trackModule('Настройки','settings',import(`../settings.js${_MODULE_VERSION}`).then(module=>{
  settingsUi=module.createSettingsUi({html,renderHtml,toast,proxyApi});
  return settingsUi;
}));
let promoOps=null;
const PROMO_OPS_READY = trackModule('Промокоды','accounts',(async()=>{
  const promoCore=await import(`../core.js${_MODULE_VERSION}`);
  const {createModals}=await import(`../modals.js${_MODULE_VERSION}`);
  const module=await import(`../promo_ops.js${_MODULE_VERSION}`);
  const {openModal,confirmFlow}=createModals({html,renderHtml});
  promoOps=module.createPromoOps({html,renderHtml,toast,openModal,confirmFlow,
    formatTimestamp:promoCore.formatTimestamp,humanLabel:promoCore.humanLabel,adminFetch,
    promoStatusBadgeClass:promoCore.promoStatusBadgeClass,redemptionStatusBadgeClass:promoCore.redemptionStatusBadgeClass});
  return promoOps;
})());
window.__PROMO_OPS_READY=PROMO_OPS_READY;

const TRAFFIC_PERIODS = {
  '1h': { label: 'за 1 час', ms: 60 * 60 * 1000 },
  '12h': { label: 'за 12 часов', ms: 12 * 60 * 60 * 1000 },
  '24h': { label: 'за 24 часа', ms: 24 * 60 * 60 * 1000 },
  '3d': { label: 'за 3 дня', ms: 3 * 24 * 60 * 60 * 1000 },
  '7d': { label: 'за неделю', ms: 7 * 24 * 60 * 60 * 1000 },
  '30d': { label: 'за месяц', ms: 30 * 24 * 60 * 60 * 1000 },
};

function fmt(bytes){
  if(bytes===null||bytes===undefined)return'—';
  if(bytes<1024)return bytes+'B';
  if(bytes<1048576)return(bytes/1024).toFixed(1)+'KB';
  if(bytes<1073741824)return(bytes/1048576).toFixed(1)+'MB';
  return(bytes/1073741824).toFixed(2)+'GB';
}
function fmtMoney(value,currency='USD'){
  if(value===null||value===undefined||value==='')return'—';
  const n=Number(value);
  if(!Number.isFinite(n))return'—';
  return `${n.toFixed(n%1===0?0:2)} ${currency||'USD'}`;
}
function fmtGb(value){
  if(value===null||value===undefined||value==='')return'—';
  const n=Number(value);
  if(!Number.isFinite(n))return'—';
  return `${n.toFixed(n%1===0?0:1)} GB`;
}
function toApiDate(ms){
  return new Date(ms).toISOString().slice(0,19);
}

function toLocalInputValue(ms){
  const d=new Date(ms);
  d.setMinutes(d.getMinutes()-d.getTimezoneOffset());
  return d.toISOString().slice(0,16);
}

function getTrafficPeriod(){
  const sel=document.getElementById('traffic-period');
  const value=sel?sel.value:'24h';
  if(value==='all')return{query:'',label:'за все время'};

  const now=Date.now();
  let startMs=now-(TRAFFIC_PERIODS[value]?.ms||TRAFFIC_PERIODS['24h'].ms);
  let endMs=now;
  let label=TRAFFIC_PERIODS[value]?.label||TRAFFIC_PERIODS['24h'].label;

  if(value==='custom'){
    const from=document.getElementById('traffic-from');
    const to=document.getElementById('traffic-to');
    if(from&&!from.value)from.value=toLocalInputValue(now-24*60*60*1000);
    if(to&&!to.value)to.value=toLocalInputValue(now);
    startMs=from?.value?new Date(from.value).getTime():startMs;
    endMs=to?.value?new Date(to.value).getTime():endMs;
    if(!Number.isFinite(startMs)||!Number.isFinite(endMs)||startMs>=endMs){
      toast('Некорректный период','err');
      startMs=now-24*60*60*1000;endMs=now;
    }
    label='за выбранный период';
  }

  const query=`?start=${encodeURIComponent(toApiDate(startMs))}&end=${encodeURIComponent(toApiDate(endMs))}`;
  return{query,label,start:startMs,end:endMs};
}

function onTrafficPeriodChange(){
  const isCustom=document.getElementById('traffic-period')?.value==='custom';
  ['traffic-from','traffic-to'].forEach(id=>{
    const el=document.getElementById(id);
    if(el)el.style.display=isCustom?'':'none';
  });
  loadDashboard();
}

// NAV
function showPage(name){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  const page=document.getElementById('page-'+name);
  if(!page)return;
  page.classList.add('active');
  const nav=document.querySelector(`[data-page="${name}"]`);
  if(nav)nav.classList.add('active');
  if(name==='accounts')runAdminAction(withModule(ACCOUNT_UI_READY,'Аккаунты','accounts',ui=>ui.loadAccounts()));
  if(name==='migration')runAdminAction(withModule(ACCOUNT_UI_READY,'Аккаунты','migration',ui=>ui.loadMigration()));
  if(name==='users')runAdminAction(withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.loadUsers()));
  if(name==='nodes')runAdminAction(withModule(NODES_UI_READY,'Ноды','nodes',ui=>ui.loadNodes()));
  if(name==='ops-health')runAdminAction(withModule(OPS_HEALTH_READY,'Здоровье','ops-health',ui=>ui.loadOpsHealth()));
  if(name==='legacy-transitions')runAdminAction(withModule(LEGACY_TRANSITIONS_READY,'Легаси-переходы','legacy-transitions',ui=>ui.loadQueue()));
  if(name==='configs')runAdminAction(withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.loadConfigsPage()));
  if(name==='settings')runAdminAction(withModule(SETTINGS_UI_READY,'Настройки','settings',ui=>ui.loadSettingsPage()));
  if(name==='tickets')runAdminAction(withModule(TICKETS_UI_READY,'Тикеты','tickets',ui=>ui.loadTickets()));
  if(name==='stars')runAdminAction(withModule(STARS_UI_READY,'Telegram Stars','stars',ui=>{ui.loadStarsTariffs();ui.loadStarsSettings();ui.loadStarsPayments();ui.loadStarsOrphans();}));
  if(name==='routing')runAdminAction(withModule(ROUTING_UI_READY,'Роутинг хостов','routing',ui=>ui.loadRouting()));
}
function switchTab(id,el){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  el.classList.add('active');
}

// DASHBOARD
async function loadDashboard(){
  accountUi?.loadDashboard();
  const period=getTrafficPeriod();
  const [sysR,nodesR,usageR]=await Promise.all([api('/system'),api('/nodes'),api('/nodes/usage'+period.query)]);
  const sys=await sysR.json();
  const nodes=await nodesR.json();
  const usage=await usageR.json();
  const usages=usage.usages||[];
  allNodes=nodes;
  window._dashNodeUsages=usages;
  const mem=Math.round(sys.mem_used/sys.mem_total*100);
  const label=document.getElementById('traffic-period-label');
  if(label)label.textContent=period.label;
  renderHtml(document.getElementById('sys-stats'),html`
    <div class="stat-card"><div class="stat-label">Пользователи</div><div class="stat-value">${sys.total_user}</div><div class="stat-sub">${sys.users_active} активных · ${sys.users_expired} истекших</div></div>
    <div class="stat-card"><div class="stat-label">Онлайн</div><div class="stat-value stat-value--positive">${sys.online_users}</div><div class="stat-sub">прямо сейчас</div></div>
    <div class="stat-card"><div class="stat-label">Входящий трафик</div><div class="stat-value">${fmt(sys.incoming_bandwidth)}</div><div class="stat-sub">${fmt(sys.incoming_bandwidth_speed)}/с</div></div>
    <div class="stat-card"><div class="stat-label">Исходящий трафик</div><div class="stat-value">${fmt(sys.outgoing_bandwidth)}</div><div class="stat-sub">${fmt(sys.outgoing_bandwidth_speed)}/с</div></div>
    <div class="stat-card"><div class="stat-label">CPU</div><div class="stat-value">${sys.cpu_usage.toFixed(1)}%</div><div class="stat-sub">${sys.cpu_cores} ядр</div></div>
    <div class="stat-card"><div class="stat-label">RAM</div><div class="stat-value">${mem}%</div><div class="stat-sub">${fmt(sys.mem_used)} / ${fmt(sys.mem_total)}</div></div>
  `);
  renderHtml(document.getElementById('dash-nodes'),html`${nodes.map(n=>html`
    <div class="dash-node-row">
      <span class="dot ${n.status==='connected'?'dot-green':'dot-red'}"></span>
      <div class="dash-node-info">
        <div class="dash-node-name">${n.name}</div>
        <div class="dash-node-addr">${n.address} · xray ${n.xray_version||'?'}</div>
      </div>
      <span class="badge ${n.status==='connected'?'badge-green':'badge-red'}">${n.status==='connected'?'ок':'офф'}</span>
    </div>
  `)}`);
  const totalTraffic=usages.reduce((s,u)=>s+u.uplink+u.downlink,0);
  renderHtml(document.getElementById('dash-node-traffic'),usages.length?html`${usages.map(u=>{
    const total=u.uplink+u.downlink;
    const pct=totalTraffic>0?Math.round(total/totalTraffic*100):0;
    return html`<div class="clickable dash-traffic-row" data-action="open-node-traffic" data-node-id="${u.node_id===null?'null':u.node_id}">
      <div class="dash-traffic-header">
        <span class="muted2">${u.node_name}</span>
        <span>${fmt(total)}</span>
      </div>
      <div class="traffic-bar"><div class="traffic-fill" style="width:${pct}%"></div></div>
    </div>`;
  })}`:html`<p class="dash-traffic-empty">Нет трафика за период</p>`);
}

let allInbounds={};
// PH7-16 Wave 4: shared with admin/technical/configs.js's Inbound Extras
// tab, same accessor-DI discipline as allNodes/allUsers -- one cache, not
// a second independently-fetched copy.
function getAllInbounds(){return allInbounds;}
function setAllInbounds(list){allInbounds=list;}
export async function bootstrap(){
  const readyAccountUi=await ACCOUNT_UI_READY;
  if(!readyAccountUi){
    showModuleUnavailable('dashboard','Аккаунты');
    showModuleUnavailable('accounts','Аккаунты');
    return;
  }
  loadDashboard();
  loadAccountsSafely();
  try{
    const [nR,fR,iR]=await Promise.all([api('/nodes'),proxyApi('/admin/node-filters'),api('/inbounds')]);
    allNodes=await nR.json();
    nodeFilters=fR.ok?await fR.json():{};
    allInbounds=await iR.json();
  }catch(e){console.warn('bootstrap',e)}
}

function loadAccountsSafely(){accountUi?.loadAccounts().catch(error=>console.warn('accounts',error));}

function parseNodeId(value){
  if(value==='null'||value===''||value===undefined)return null;
  const id=Number(value);
  return Number.isInteger(id)?id:null;
}
function parseInteger(value){
  const parsed=Number(value);
  return Number.isInteger(parsed)?parsed:null;
}
function runAdminAction(work){
  Promise.resolve(work).catch(error=>{
    console.error('admin action failed',error);
    if(error?.message!=='unauth')toast('Операция не выполнена','err');
  });
}

document.addEventListener('click',event=>{
  const el=event.target.closest('[data-action]');
  if(!el)return;
  const action=el.dataset.action;
  const username=el.dataset.username;
  const nodeId=parseNodeId(el.dataset.nodeId);
  const numericId=parseInteger(el.dataset.ticketId??el.dataset.paymentId??el.dataset.tariffId);
  let work;
  switch(action){
    case'login':work=doLogin();break;
    case'logout':work=doLogout();break;
    case'show-page':showPage(el.dataset.page);break;
    case'switch-tab':switchTab(el.dataset.target,el);break;
    case'load-dashboard':work=loadDashboard();break;
    case'load-nodes':work=withModule(NODES_UI_READY,'Ноды','nodes',ui=>ui.loadNodes());break;
    case'open-create-user':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.openCreateUser());break;
    case'create-user':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.createUser());break;
    case'open-user':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.openUser(username));break;
    case'open-user-from-node':closeModal('node-modal');work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.openUser(username));break;
    case'change-device-limit':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.changeDeviceLimit(username,parseInteger(el.dataset.limit)));break;
    case'admin-remove-device':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.adminRemoveDevice(parseInteger(el.dataset.deviceId),username));break;
    case'delete-user':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.deleteUser(username));break;
    case'disable-user':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.disableUser(username));break;
    case'enable-user':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.enableUser(username));break;
    case'reset-traffic':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.resetTraffic(username));break;
    case'save-user':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.saveUser(username));break;
    case'open-node':work=withModule(NODES_UI_READY,'Ноды','nodes',ui=>ui.openNode(nodeId));break;
    case'open-node-traffic':work=withModule(NODES_UI_READY,'Ноды','nodes',ui=>ui.openNodeTraffic(nodeId));break;
    case'open-node-settings':work=withModule(NODES_UI_READY,'Ноды','nodes',ui=>ui.openNodeSettings(nodeId));break;
    case'reconnect-node':event.stopPropagation();work=withModule(NODES_UI_READY,'Ноды','nodes',ui=>ui.reconnectNode(nodeId));break;
    case'save-node-settings':work=withModule(NODES_UI_READY,'Ноды','nodes',ui=>ui.saveNodeSettings(nodeId));break;
    case'add-quiet-hour':work=withModule(NODES_UI_READY,'Ноды','nodes',ui=>ui.addQuietHour());break;
    case'remove-quiet-hour':work=withModule(NODES_UI_READY,'Ноды','nodes',ui=>ui.removeQuietHour(parseInteger(el.dataset.quietIndex)));break;
    case'toggle-nf-group':
      if(event.target.closest('input'))return;
      work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.toggleNfGroup(el.dataset.target));break;
    case'add-global-config':work=withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.addGlobalConfig());break;
    case'toggle-config':work=withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.toggleConfig(parseInteger(el.dataset.configIndex)));break;
    case'delete-config':work=withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.deleteConfig(parseInteger(el.dataset.configIndex)));break;
    case'add-per-user-config':work=withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.addPerUserConfig());break;
    case'delete-per-user-config':work=withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.deletePerUserConfig(username,parseInteger(el.dataset.configIndex)));break;
    case'add-inbound-extra':work=withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.addInboundExtra());break;
    case'delete-inbound-extra':work=withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.deleteInboundExtra(el.dataset.inboundTag));break;
    case'format-inbound-extra':work=withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.formatInboundExtraJson(el.dataset.inboundTag));break;
    case'update-inbound-extra':work=withModule(CONFIGS_UI_READY,'Доп. конфиги','configs',ui=>ui.updateInboundExtra(el.dataset.inboundTag));break;
    case'save-settings':work=withModule(SETTINGS_UI_READY,'Настройки','settings',ui=>ui.saveSettings());break;
    case'save-bot-settings':work=withModule(SETTINGS_UI_READY,'Настройки','settings',ui=>ui.saveBotSettings());break;
    case'restart-bot':work=withModule(SETTINGS_UI_READY,'Настройки','settings',ui=>ui.restartBot());break;
    case'save-support-settings':work=withModule(SETTINGS_UI_READY,'Настройки','settings',ui=>ui.saveSupportSettings());break;
    case'open-ticket':work=withModule(TICKETS_UI_READY,'Тикеты','tickets',ui=>ui.openTicket(numericId));break;
    case'send-ticket-reply':work=withModule(TICKETS_UI_READY,'Тикеты','tickets',ui=>ui.sendTicketReply());break;
    case'close-ticket':work=withModule(TICKETS_UI_READY,'Тикеты','tickets',ui=>ui.closeTicket());break;
    case'add-stars-tariff':work=withModule(STARS_UI_READY,'Telegram Stars','stars',ui=>ui.addStarsTariff());break;
    case'delete-stars-tariff':work=withModule(STARS_UI_READY,'Telegram Stars','stars',ui=>ui.deleteStarsTariff(numericId));break;
    case'stars-payment-action':work=withModule(STARS_UI_READY,'Telegram Stars','stars',ui=>ui.starsPaymentAction(numericId,el.dataset.paymentAction));break;
    case'stars-orphan-action':work=withModule(STARS_UI_READY,'Telegram Stars','stars',ui=>ui.starsOrphanAction(numericId,el.dataset.paymentAction));break;
    case'routing-host-op':work=withModule(ROUTING_UI_READY,'Роутинг хостов','routing',ui=>ui.handleRoutingClick(el));break;
    case'open-legacy-transition':work=withModule(LEGACY_TRANSITIONS_READY,'Легаси-переходы','legacy-transitions',ui=>ui.handleQueueClick(el));break;
    case'close-modal':closeModal(el.dataset.target);break;
    // PH7-16 Wave 1: accounts.js used to register its own separate
    // `document.addEventListener('click', ...)` for every action below
    // this line (open-account, account-tab, issue-account-credential,
    // copy-issued-credential, copy-technical, new-manual-payment,
    // legacy-commercial-transition, open-manual-payment, tg-rebind,
    // open-create-account, open-promo-manager, open-admin-grant, plus the
    // separate `[data-expiry-op]` attribute match) -- a second dispatch
    // table firing independently on the same click event. It is now a
    // plain function this single dispatcher falls back to for any action
    // it doesn't itself recognize, same bridge pattern already used for
    // routing-host-op above. handleAccountClick does its own promise
    // catch/toast internally (matching its original behavior exactly), so
    // this default case does not also route through runAdminAction below.
    default:runAdminAction(withModule(ACCOUNT_UI_READY,'Аккаунты','accounts',ui=>ui.handleAccountClick(el,event)));return;
  }
  if(work!==undefined)runAdminAction(work);
});

document.addEventListener('change',event=>{
  const el=event.target.closest('[data-change-action]');
  if(!el)return;
  let work;
  switch(el.dataset.changeAction){
    case'traffic-period':work=onTrafficPeriodChange();break;
    case'load-tickets':work=withModule(TICKETS_UI_READY,'Тикеты','tickets',ui=>ui.loadTickets(el.value||undefined));break;
    case'load-stars-payments':work=withModule(STARS_UI_READY,'Telegram Stars','stars',ui=>ui.loadStarsPayments(el.value||undefined));break;
    case'save-stars-settings':work=withModule(STARS_UI_READY,'Telegram Stars','stars',ui=>ui.saveStarsSettings());break;
    case'toggle-bot-proxy':work=withModule(SETTINGS_UI_READY,'Настройки','settings',ui=>ui.toggleBotProxy());break;
    case'nf-all-toggle':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.onNfAllToggle());break;
    case'nf-group-all-toggle':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.onNfGroupAllToggle(el));break;
    case'nf-cfg-toggle':work=withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.onNfCfgToggle());break;
    case'toggle-stars-tariff':work=withModule(STARS_UI_READY,'Telegram Stars','stars',ui=>ui.toggleStarsTariff(parseInteger(el.dataset.tariffId),el.checked));break;
    default:return;
  }
  if(work!==undefined)runAdminAction(work);
});

document.addEventListener('input',event=>{
  const el=event.target.closest('[data-input-action]');
  if(el?.dataset.inputAction==='filter-users')runAdminAction(withModule(MARZBAN_USERS_UI_READY,'Пользователи Marzban','users',ui=>ui.filterUsers()));
});

// PH7-16 Wave 0B: the app-init trigger (restoreAdminSession()) moved to
// admin/app/main.js -- the composition root now calls it explicitly after
// dynamically import()-ing this module and registering `bootstrap` as
// auth.js's post-authentication callback (see main.js).
