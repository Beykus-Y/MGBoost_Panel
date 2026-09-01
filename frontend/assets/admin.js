// PH7-16 Wave 0B: this file is loaded as an ES module, dynamically
// import()-ed by admin/app/main.js (not a classic <script> tag anymore --
// see index.html). Everything it needs from the extracted shell pieces
// (Wave 0A) is now an explicit import instead of a bare-name reference
// into a shared classic-script global scope. `bootstrap` is exported so
// main.js can register it as the post-login/session-restore callback
// (auth.js exposes onAuthenticated() for exactly that, to avoid an
// auth.js <-> admin.js import cycle).
//
// kernel.js/api.js/auth.js are loaded via versioned dynamic import(), same
// as the admin/*.js canonical modules below and for the same reason: a
// static `import ... from './admin/app/kernel.js'` specifier would not
// inherit this file's own `?v=` query, and the server sends a real
// `Cache-Control: public, max-age=3600` for JS assets -- without
// propagating the version explicitly, a stale-cached kernel/api/auth
// could silently pair with a freshly-deployed admin.js after a release.
// `import.meta.url` reflects the exact versioned specifier main.js used
// to import this module, so re-deriving `_MODULE_VERSION` from it here
// and reusing it for every dynamic import below (shell pieces and the
// canonical admin/*.js modules alike) keeps the whole graph on one
// deploy's URLs -- and, since the browser's module registry is keyed by
// resolved URL, every one of these dynamic imports converges on the exact
// same singleton kernel/api/auth instances main.js itself is using.
const _MODULE_VERSION = new URL(import.meta.url).search;
const {html,renderHtml,toast,closeModal} = await import(`./admin/app/kernel.js${_MODULE_VERSION}`);
const {api,proxyApi,adminFetch,getJson} = await import(`./admin/app/api.js${_MODULE_VERSION}`);
const {doLogin,doLogout} = await import(`./admin/app/auth.js${_MODULE_VERSION}`);

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
let userDeviceCounts = {};
let accountUi = null;
let routingUi = null;
let nodesUi = null;
let opsHealthUi = null;
let legacyTransitionsUi = null;
const ACCOUNT_UI_READY = import(`./admin/accounts.js${_MODULE_VERSION}`).then(module=>{
  accountUi=module.createAccountUi({adminFetch,getJson,html,renderHtml,showPage,toast});
  return accountUi;
});
// PH7-16 Wave 3: the Operations queue reuses accountUi's own `payments`
// instance (openLegacyTransitionById) instead of a second createPayments()
// -- one transition-modal implementation, two entry points.
const LEGACY_TRANSITIONS_READY = ACCOUNT_UI_READY.then(async readyAccountUi=>{
  const coreModule=await import(`./admin/core.js${_MODULE_VERSION}`);
  const module=await import(`./admin/legacy_transitions.js${_MODULE_VERSION}`);
  legacyTransitionsUi=module.createLegacyTransitionsQueue({html,renderHtml,toast,adminFetch,getJson,
    formatTimestamp:coreModule.formatTimestamp,humanLabel:coreModule.humanLabel,
    openLegacyTransitionById:readyAccountUi.payments.openLegacyTransitionById,openAccount:readyAccountUi.openAccount});
  return legacyTransitionsUi;
});
const ROUTING_UI_READY = import(`./admin/routing.js${_MODULE_VERSION}`).then(module=>{
  routingUi=module.createRoutingUi({adminFetch,getJson,html,renderHtml,toast});
  return routingUi;
});
const NODES_UI_READY = import(`./admin/operations/nodes.js${_MODULE_VERSION}`).then(module=>{
  nodesUi=module.createNodesUi({html,renderHtml,toast,closeModal,api,proxyApi,fmt,fmtMoney,getTrafficPeriod,getAllNodes,setAllNodes,getAllUsers,setAllUsers});
  return nodesUi;
});
const OPS_HEALTH_READY = (async()=>{
  const opsCore=await import(`./admin/core.js${_MODULE_VERSION}`);
  const module=await import(`./admin/ops_health.js${_MODULE_VERSION}`);
  opsHealthUi=module.createOpsHealth({html,renderHtml,toast,getJson,
    formatTimestamp:opsCore.formatTimestamp,formatDuration:opsCore.formatDuration});
  return opsHealthUi;
})();
let configsUi=null;
const CONFIGS_UI_READY = import(`./admin/technical/configs.js${_MODULE_VERSION}`).then(module=>{
  configsUi=module.createConfigsUi({html,renderHtml,toast,api,proxyApi,getAllInbounds,setAllInbounds});
  return configsUi;
});
let settingsUi=null;
const SETTINGS_UI_READY = import(`./admin/settings.js${_MODULE_VERSION}`).then(module=>{
  settingsUi=module.createSettingsUi({html,renderHtml,toast,proxyApi});
  return settingsUi;
});
let promoOps=null;
const PROMO_OPS_READY = (async()=>{
  const promoCore=await import(`./admin/core.js${_MODULE_VERSION}`);
  const {createModals}=await import(`./admin/modals.js${_MODULE_VERSION}`);
  const module=await import(`./admin/promo_ops.js${_MODULE_VERSION}`);
  const {openModal,confirmFlow}=createModals({html,renderHtml});
  promoOps=module.createPromoOps({html,renderHtml,toast,openModal,confirmFlow,
    formatTimestamp:promoCore.formatTimestamp,humanLabel:promoCore.humanLabel,adminFetch});
  return promoOps;
})();
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
function parseUTC(v){
  if(!v)return null;
  if(typeof v==='number')return new Date(v*1000);
  // Marzban returns naive UTC strings without 'Z' — force UTC to avoid local-time offset
  if(!v.endsWith('Z')&&!/[+-]\d{2}:?\d{2}$/.test(v))v+='Z';
  return new Date(v);
}
function fmtDate(ts){
  if(!ts)return'∞';
  const d=parseUTC(ts);
  return d.toLocaleDateString('ru-RU');
}
function fmtRelDate(ts){
  if(!ts)return'∞';
  const d=parseUTC(ts);
  const diff=d-Date.now();
  const days=Math.ceil(diff/86400000);
  if(days<0)return html`<span style="color:var(--red)">просрочен</span>`;
  if(days===0)return html`<span style="color:var(--amber)">сегодня</span>`;
  if(days<=3)return html`<span style="color:var(--amber)">${days}д</span>`;
  return`${days}д`;
}
function fmtOnline(dt){
  if(!dt)return'—';
  const d=parseUTC(dt);
  const diff=(Date.now()-d)/1000;
  if(diff<120)return html`<span style="color:var(--green)">сейчас</span>`;
  if(diff<3600)return`${Math.floor(diff/60)}м назад`;
  if(diff<86400)return`${Math.floor(diff/3600)}ч назад`;
  return`${Math.floor(diff/86400)}д назад`;
}
function statusBadge(s){
  const m={active:'badge-green',disabled:'badge-red',expired:'badge-red',limited:'badge-amber',on_hold:'badge-gray'};
  const l={active:'активен',disabled:'выкл',expired:'истёк',limited:'лимит',on_hold:'на паузе'};
  return html`<span class="badge ${m[s]||'badge-gray'}">${l[s]||s}</span>`;
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
  if(name==='accounts')accountUi?.loadAccounts();
  if(name==='migration')accountUi?.loadMigration();
  if(name==='users')loadUsers();
  if(name==='nodes')NODES_UI_READY.then(()=>nodesUi&&nodesUi.loadNodes());
  if(name==='ops-health')OPS_HEALTH_READY.then(()=>opsHealthUi&&opsHealthUi.loadOpsHealth());
  if(name==='legacy-transitions')LEGACY_TRANSITIONS_READY.then(()=>legacyTransitionsUi&&legacyTransitionsUi.loadQueue());
  if(name==='configs')CONFIGS_UI_READY.then(()=>configsUi&&configsUi.loadConfigsPage());
  if(name==='settings')SETTINGS_UI_READY.then(()=>settingsUi&&settingsUi.loadSettingsPage());
  if(name==='tickets'){loadTickets();}
  if(name==='stars'){loadStarsTariffs();loadStarsSettings();loadStarsPayments();loadStarsOrphans();}
  if(name==='routing')ROUTING_UI_READY.then(()=>routingUi&&routingUi.loadRouting());
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
    <div class="stat-card"><div class="stat-label">Онлайн</div><div class="stat-value" style="color:var(--green)">${sys.online_users}</div><div class="stat-sub">прямо сейчас</div></div>
    <div class="stat-card"><div class="stat-label">Входящий трафик</div><div class="stat-value">${fmt(sys.incoming_bandwidth)}</div><div class="stat-sub">${fmt(sys.incoming_bandwidth_speed)}/с</div></div>
    <div class="stat-card"><div class="stat-label">Исходящий трафик</div><div class="stat-value">${fmt(sys.outgoing_bandwidth)}</div><div class="stat-sub">${fmt(sys.outgoing_bandwidth_speed)}/с</div></div>
    <div class="stat-card"><div class="stat-label">CPU</div><div class="stat-value">${sys.cpu_usage.toFixed(1)}%</div><div class="stat-sub">${sys.cpu_cores} ядр</div></div>
    <div class="stat-card"><div class="stat-label">RAM</div><div class="stat-value">${mem}%</div><div class="stat-sub">${fmt(sys.mem_used)} / ${fmt(sys.mem_total)}</div></div>
  `);
  renderHtml(document.getElementById('dash-nodes'),html`${nodes.map(n=>html`
    <div style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:0.5px solid var(--border)">
      <span class="dot ${n.status==='connected'?'dot-green':'dot-red'}"></span>
      <div style="flex:1">
        <div style="font-size:13px">${n.name}</div>
        <div style="font-size:11px;color:var(--text2)">${n.address} · xray ${n.xray_version||'?'}</div>
      </div>
      <span class="badge ${n.status==='connected'?'badge-green':'badge-red'}">${n.status==='connected'?'ок':'офф'}</span>
    </div>
  `)}`);
  const totalTraffic=usages.reduce((s,u)=>s+u.uplink+u.downlink,0);
  renderHtml(document.getElementById('dash-node-traffic'),usages.length?html`${usages.map(u=>{
    const total=u.uplink+u.downlink;
    const pct=totalTraffic>0?Math.round(total/totalTraffic*100):0;
    return html`<div class="clickable" style="padding:8px 0;border-bottom:0.5px solid var(--border)" data-action="open-node-traffic" data-node-id="${u.node_id===null?'null':u.node_id}">
      <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
        <span style="color:var(--text2)">${u.node_name}</span>
        <span>${fmt(total)}</span>
      </div>
      <div class="traffic-bar"><div class="traffic-fill" style="width:${pct}%"></div></div>
    </div>`;
  })}`:html`<p style="color:var(--text3);font-size:13px;padding:1rem 0">Нет трафика за период</p>`);
}

// USERS
async function loadUsers(){
  const r=await api('/users?limit=500');
  const d=await r.json();
  allUsers=d.users||[];
  userDeviceCounts={};
  try{
    const countsR=await proxyApi('/admin/user-devices-counts',{method:'POST',body:JSON.stringify({usernames:allUsers.map(u=>u.username)})});
    if(countsR.ok)userDeviceCounts=await countsR.json();
  }catch(e){console.warn('device counts',e)}
  document.getElementById('users-count').textContent='('+allUsers.length+')';
  renderUsers(allUsers);
  // populate per-user select
  const sel=document.getElementById('pu-user');
  renderHtml(sel,html`${allUsers.map(u=>html`<option value="${u.username}">${u.username}</option>`)}`);
}
function filterUsers(){
  const q=document.getElementById('user-search').value.toLowerCase();
  renderUsers(allUsers.filter(u=>u.username.toLowerCase().includes(q)||(u.note||'').toLowerCase().includes(q)));
}
function renderUsers(users){
  renderHtml(document.getElementById('users-tbody'),html`${users.map(u=>{
    const f=nodeFilters[u.username];
    const hasFilter=f&&f.all===false&&(f.allowed_configs||[]).length>0;
    return html`
    <tr class="clickable" data-action="open-user" data-username="${u.username}">
      <td>
        <div style="font-weight:500">${u.username}${hasFilter?html` <span style="font-size:10px;color:var(--amber);border:0.5px solid var(--amber2);border-radius:3px;padding:1px 5px;vertical-align:middle">фильтр</span>`:''}</div>
        ${u.note?html`<div style="font-size:11px;color:var(--text2)">${u.note}</div>`:''}
      </td>
      <td>${statusBadge(u.status)}</td>
      <td>${fmt(u.used_traffic)}${u.data_limit?` / ${fmt(u.data_limit)}`:'  / ∞'}</td>
      <td>${fmtRelDate(u.expire)}</td>
      <td style="font-size:11px;color:var(--text2);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(u.sub_last_user_agent||'—').split('/')[0]}</td>
      <td>${userDeviceCounts[u.username]??0}</td>
      <td>${fmtOnline(u.online_at)}</td>
      <td><button data-action="open-user" data-username="${u.username}">···</button></td>
    </tr>
  `;
  })}`);
}

// USER DETAIL MODAL
async function openUser(username){
  const modal=document.getElementById('user-modal');
  const body=document.getElementById('user-modal-body');
  const footer=document.getElementById('user-modal-footer');
  document.getElementById('user-modal-title').textContent=username;
  renderHtml(body,html`<div class="loading"><span class="spinner"></span>Загрузка...</div>`);
  footer.replaceChildren();
  modal.classList.add('open');

  const [userR,usageR,devR]=await Promise.all([
    api('/user/'+encodeURIComponent(username)),
    api('/user/'+encodeURIComponent(username)+'/usage'),
    proxyApi('/admin/user-devices/'+encodeURIComponent(username)),
  ]);
  const u=await userR.json();
  const usage=await usageR.json();
  const devData=devR.ok?await devR.json():null;

  const usageByNode=usage.usages||[];
  const totalUsage=usageByNode.reduce((s,n)=>s+n.used_traffic,0);

  renderHtml(body,html`
    <div class="user-detail-grid">
      <div class="detail-item"><div class="detail-label">Статус</div><div class="detail-value">${statusBadge(u.status)}</div></div>
      <div class="detail-item"><div class="detail-label">Использовано</div><div class="detail-value">${fmt(u.used_traffic)}</div></div>
      <div class="detail-item"><div class="detail-label">Лимит</div><div class="detail-value">${u.data_limit?fmt(u.data_limit):'∞'}</div></div>
      <div class="detail-item"><div class="detail-label">Истекает</div><div class="detail-value">${fmtDate(u.expire)}</div></div>
      <div class="detail-item"><div class="detail-label">Создан</div><div class="detail-value">${fmtDate(u.created_at)}</div></div>
      <div class="detail-item"><div class="detail-label">Онлайн</div><div class="detail-value">${fmtOnline(u.online_at)}</div></div>
    </div>
    ${u.note?html`<div style="background:var(--bg4);border-radius:8px;padding:10px 12px;margin-bottom:1rem;font-size:13px"><span style="color:var(--text2)">Заметка: </span>${u.note}</div>`:''}
    <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Трафик по нодам</div>
    <div class="usage-node-list">
      ${usageByNode.map(n=>{
        const pct=totalUsage>0?Math.round(n.used_traffic/totalUsage*100):0;
        return html`<div class="usage-node-item">
          <div style="display:flex;justify-content:space-between">
            <span class="usage-node-name">${n.node_name}</span>
            <span class="usage-node-val">${fmt(n.used_traffic)} <span style="color:var(--text3);font-size:11px">${pct}%</span></span>
          </div>
          <div class="traffic-bar" style="margin-top:6px"><div class="traffic-fill" style="width:${pct}%"></div></div>
        </div>`;
      })}
    </div>
    <div style="margin-top:1rem">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em">Устройства (HWID)</div>
        <button style="font-size:11px;padding:3px 10px" data-action="change-device-limit" data-username="${username}" data-limit="${devData?devData.limit:3}">Изменить лимит</button>
      </div>
      ${devData?html`
        <div style="font-size:12px;color:var(--text2);margin-bottom:8px">
          Активных: <b>${devData.active_count}</b> / <b>${devData.limit===0?'∞':devData.limit}</b>
        </div>
        ${devData.devices.length?devData.devices.map(d=>html`
          <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:0.5px solid var(--border);font-size:12px">
            <div>
              <span style="${d.is_active?'color:var(--green)':'color:var(--text3)'}">${d.is_active?'●':'○'}</span>
              <span style="margin-left:6px;font-weight:500">${d.display_name||d.device_name||d.client_name||'Устройство'}</span>
              <span style="color:var(--text2);margin-left:6px">${d.platform||''}${d.client_name?` · ${d.client_name}`:''}</span>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <span style="color:var(--text3)">${fmtOnline(new Date(d.last_seen*1000).toISOString())}</span>
              <button style="font-size:11px;padding:2px 8px;color:var(--red)" data-action="admin-remove-device" data-device-id="${d.id}" data-username="${username}">✕</button>
            </div>
          </div>`):html`<div style="color:var(--text3);font-size:12px">Нет зарегистрированных устройств</div>`}
      `:html`<div style="color:var(--text3);font-size:12px">Нет данных</div>`}
    </div>
    <div style="margin-top:1rem" id="nf-section">
      <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Фильтр конфигов</div>
      <div class="loading"><span class="spinner"></span></div>
    </div>
    <div style="margin-top:1rem">
      <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Изменить</div>
      <div class="form-row">
        <div>
          <label>Дата истечения</label>
          <input type="date" id="edit-expire" value="${u.expire?new Date(u.expire*1000).toISOString().split('T')[0]:''}" />
        </div>
        <div>
          <label>Лимит (ГБ, 0=∞)</label>
          <input type="number" id="edit-limit" value="${u.data_limit?Math.round(u.data_limit/1073741824):0}" min="0" />
        </div>
      </div>
      <label>Заметка</label>
      <input type="text" id="edit-note" value="${u.note||''}" />
    </div>
  `);

  renderHtml(footer,html`
    <button class="danger" data-action="delete-user" data-username="${username}">Удалить</button>
    <button data-action="${u.status==='active'?'disable-user':'enable-user'}" data-username="${username}">${u.status==='active'?'Отключить':'Включить'}</button>
    <button data-action="reset-traffic" data-username="${username}">Сбросить трафик</button>
    <button class="primary" data-action="save-user" data-username="${username}">Сохранить</button>
  `);

  renderNodeFilterSection(username, u.links||[]);
}

function parseHostFromUri(uri){
  try{
    if(!uri||!uri.includes('://'))return null;
    let[scheme,rest]=uri.split('://',2);
    rest=rest.split('#')[0].split('?')[0];
    let auth=rest.split('/')[0];
    if(auth.includes('@')){auth=auth.slice(auth.lastIndexOf('@')+1)}
    else if(scheme.toLowerCase()==='ss'){
      try{
        const dec=atob(auth.replace(/-/g,'+').replace(/_/g,'/'));
        if(dec.includes('@'))auth=dec.slice(dec.lastIndexOf('@')+1);
      }catch{}
    }
    if(auth.startsWith('[')){return auth.slice(1,auth.indexOf(']'))||null}
    return auth.split(':')[0]||null;
  }catch{return null}
}

function parseFragmentFromUri(uri){
  try{
    if(!uri||!uri.includes('#'))return null;
    return decodeURIComponent(uri.split('#')[1])||null;
  }catch{return null}
}

function renderNodeFilterSection(username, links){
  const sec=document.getElementById('nf-section');
  if(!sec)return;

  // group configs by host, preserving order; skip hysteria2 (always pass-through)
  const hostConfigs={};
  const hostOrder=[];
  (links||[]).forEach(uri=>{
    const scheme=uri.split('://')[0].toLowerCase();
    if(scheme==='hysteria2')return;
    const host=parseHostFromUri(uri);
    const frag=parseFragmentFromUri(uri);
    if(!host||!frag)return;
    if(!hostConfigs[host]){hostConfigs[host]=[];hostOrder.push(host);}
    if(!hostConfigs[host].includes(frag))hostConfigs[host].push(frag);
  });

  // build node lookup: address -> name
  const nodeByAddr={};
  allNodes.forEach(n=>nodeByAddr[n.address]=n.name);

  const f=nodeFilters[username]||{all:true};
  // legacy formats → treat as all
  const isAll=f.all!==false||('hosts' in f)||('allowed_ips' in f);
  const allowedSet=new Set(f.allowed_configs||[]);

  if(!hostOrder.length){
    renderHtml(sec,html`
      <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Фильтр конфигов</div>
      <p style="font-size:12px;color:var(--text3)">Нет ссылок в подписке</p>`);
    return;
  }

  const groupRows=hostOrder.map((ip,groupIndex)=>{
    const nodeName=nodeByAddr[ip]||null;
    const configs=hostConfigs[ip];
    const groupId=`nfg-${groupIndex}`;

    const configChecks=configs.map(cfg=>{
      const checked=isAll||allowedSet.has(cfg);
      return html`<label style="display:flex;align-items:center;gap:6px;padding:3px 0;cursor:pointer">
        <input type="checkbox" class="nf-cfg" data-cfg="${cfg}" ${checked?html`checked`:''} style="width:auto" data-change-action="nf-cfg-toggle" />
        <span style="font-size:12px">${cfg}</span>
      </label>`;
    });

    const allGroupChecked=isAll||configs.every(c=>allowedSet.has(c));

    return html`<div style="margin:8px 0;border:1px solid var(--border);border-radius:6px;overflow:hidden">
      <div style="display:flex;align-items:center;gap:8px;padding:7px 10px;background:var(--bg3);cursor:pointer" data-action="toggle-nf-group" data-target="${groupId}">
        <input type="checkbox" class="nf-group-all" data-group-id="${groupId}" ${allGroupChecked?html`checked`:''} style="width:auto" data-change-action="nf-group-all-toggle" />
        <span style="font-size:13px;flex:1">
          ${nodeName?html`<span style="color:var(--text)">${nodeName}</span> `:''}
          <span style="font-family:monospace;font-size:11px;color:var(--text3)">${ip}</span>
        </span>
        <span style="font-size:11px;color:var(--text3)">${configs.length} конф.</span>
      </div>
      <div id="${groupId}" style="padding:6px 10px 6px 28px">${configChecks}</div>
    </div>`;
  });

  renderHtml(sec,html`
    <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Фильтр конфигов</div>
    <label style="display:flex;align-items:center;gap:8px;margin-bottom:10px;cursor:pointer">
      <input type="checkbox" id="nf-all" ${isAll?html`checked`:''} data-change-action="nf-all-toggle" style="width:auto" />
      <span style="font-size:13px">Все конфиги (без фильтра)</span>
    </label>
    <div id="nf-list" style="${isAll?'opacity:0.45;pointer-events:none':''}">${groupRows}</div>`);
}

function toggleNfGroup(id){
  const el=document.getElementById(id);
  if(el)el.style.display=el.style.display==='none'?'':'none';
}

function onNfAllToggle(){
  const all=document.getElementById('nf-all').checked;
  const list=document.getElementById('nf-list');
  list.style.opacity=all?'0.45':'';
  list.style.pointerEvents=all?'none':'';
}

function onNfGroupAllToggle(groupCb){
  const groupDiv=document.getElementById(groupCb.dataset.groupId);
  if(!groupDiv)return;
  groupDiv.querySelectorAll('.nf-cfg').forEach(cb=>{
    cb.checked=groupCb.checked;
  });
  _syncGroupAllCheckboxes();
}

function onNfCfgToggle(){
  _syncGroupAllCheckboxes();
}

function _syncGroupAllCheckboxes(){
  document.querySelectorAll('.nf-group-all').forEach(groupCb=>{
    const groupDiv=document.getElementById(groupCb.dataset.groupId);
    if(!groupDiv)return;
    const cfgs=[...groupDiv.querySelectorAll('.nf-cfg')];
    groupCb.checked=cfgs.length>0&&cfgs.every(c=>c.checked);
  });
}
async function saveUser(username){
  const expire=document.getElementById('edit-expire').value;
  const limitGB=parseFloat(document.getElementById('edit-limit').value)||0;
  const note=document.getElementById('edit-note').value;
  const body={note,data_limit:limitGB?Math.round(limitGB*1073741824):null};
  if(expire)body.expire=Math.floor(new Date(expire).getTime()/1000);
  else body.expire=null;

  // node filter
  const nfAllEl=document.getElementById('nf-all');
  if(nfAllEl){
    if(nfAllEl.checked){
      nodeFilters[username]={all:true};
    }else{
      const allowed_configs=[...document.querySelectorAll('.nf-cfg')].filter(c=>c.checked).map(c=>c.dataset.cfg);
      const totalCfgs=document.querySelectorAll('.nf-cfg').length;
      if(allowed_configs.length===totalCfgs){
        nodeFilters[username]={all:true};
      }else{
        nodeFilters[username]={all:false,allowed_configs};
      }
    }
    await proxyApi('/admin/node-filters',{method:'POST',body:JSON.stringify(nodeFilters)});
  }

  const r=await api('/user/'+encodeURIComponent(username),{method:'PUT',body:JSON.stringify(body)});
  if(r.ok){toast('Сохранено');closeModal('user-modal');loadUsers();}
  else toast('Ошибка','err');
}
async function deleteUser(username){
  if(!confirm('Удалить '+username+'?'))return;
  const r=await api('/user/'+encodeURIComponent(username),{method:'DELETE'});
  if(r.ok){toast('Удалён');closeModal('user-modal');loadUsers();}
  else toast('Ошибка','err');
}
async function disableUser(username){
  const r=await api('/user/'+encodeURIComponent(username),{method:'PUT',body:JSON.stringify({status:'disabled'})});
  if(r.ok){toast('Отключён');openUser(username);loadUsers();}
}
async function changeDeviceLimit(username,current){
  const val=prompt('Лимит устройств для '+username+' (0 = безлимит, 1–20):', current);
  if(val===null)return;
  const n=parseInt(val,10);
  if(isNaN(n)||n<0||n>20){toast('Некорректное значение','err');return;}
  const r=await proxyApi('/admin/user-devices/'+encodeURIComponent(username)+'/limit',{method:'POST',body:JSON.stringify({limit:n})});
  if(r.ok){toast('Лимит обновлён');openUser(username);}
  else toast('Ошибка','err');
}
async function adminRemoveDevice(deviceId,username){
  if(!confirm('Удалить устройство и снять HWID-блокировку?'))return;
  const r=await proxyApi('/admin/user-devices/device/'+deviceId,{method:'DELETE'});
  if(r.ok){toast('Устройство удалено');openUser(username);}
  else toast('Ошибка','err');
}
async function enableUser(username){
  const r=await api('/user/'+encodeURIComponent(username),{method:'PUT',body:JSON.stringify({status:'active'})});
  if(r.ok){toast('Включён');openUser(username);loadUsers();}
}
async function resetTraffic(username){
  if(!confirm('Сбросить трафик '+username+'?'))return;
  const r=await api('/user/'+encodeURIComponent(username)+'/reset',{method:'POST'});
  if(r.ok){toast('Трафик сброшен');openUser(username);loadUsers();}
}

// CREATE USER
async function openCreateUser(){
  document.getElementById('create-modal').classList.add('open');
  const el=document.getElementById('new-inbounds');
  if(!allInbounds||!Object.keys(allInbounds).length){
    try{const r=await api('/inbounds');allInbounds=await r.json();}catch{}
  }
  renderHtml(el,Object.keys(allInbounds).length?html`${Object.entries(allInbounds).map(([proto,items])=>html`
    <div style="margin-bottom:8px">
      <div style="font-size:12px;color:var(--text2);margin-bottom:4px;text-transform:uppercase">${proto}</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px 12px;margin-left:4px">
        ${items.map(it=>html`<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:12px">
          <input type="checkbox" class="nu-ib" data-proto="${proto}" data-tag="${it.tag}" checked style="width:auto" />
          <span>${it.tag}</span>
        </label>`)}
      </div>
    </div>
  `)}`:html`<p style="color:var(--text3);font-size:12px">Нет inbounds</p>`);
}
async function createUser(){
  const username=document.getElementById('new-username').value.trim();
  const expire=document.getElementById('new-expire').value;
  const limitGB=parseFloat(document.getElementById('new-limit').value)||0;
  const note=document.getElementById('new-note').value;
  if(!username){toast('Введи имя','err');return}
  const inbounds={};
  const proxies={};
  document.querySelectorAll('.nu-ib:checked').forEach(c=>{
    const p=c.dataset.proto;
    (inbounds[p]=inbounds[p]||[]).push(c.dataset.tag);
    proxies[p]=proxies[p]||{};
  });
  const body={username,note,proxies,inbounds,data_limit:limitGB?Math.round(limitGB*1073741824):null,data_limit_reset_strategy:'no_reset'};
  if(expire)body.expire=Math.floor(new Date(expire).getTime()/1000);
  const r=await api('/user',{method:'POST',body:JSON.stringify(body)});
  if(r.ok){toast('Создан ✓');closeModal('create-modal');loadUsers();}
  else{const e=await r.json();toast(e.detail||'Ошибка','err');}
}

let allInbounds={};
// PH7-16 Wave 4: shared with admin/technical/configs.js's Inbound Extras
// tab, same accessor-DI discipline as allNodes/allUsers -- one cache, not
// a second independently-fetched copy.
function getAllInbounds(){return allInbounds;}
function setAllInbounds(list){allInbounds=list;}
export async function bootstrap(){
  await ACCOUNT_UI_READY;
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

// TICKETS
let _currentTicketId=null;
const _TICKET_STATUS_LABELS={open:'Открыт',waiting_human:'Ждёт оператора',new_user:'Новый польз.',closed:'Закрыт'};
const _TICKET_STATUS_COLORS={open:'#4af',waiting_human:'#fa4',new_user:'#a4f',closed:'#888'};

async function loadTickets(status){
  const tbody=document.getElementById('tickets-tbody');
  renderHtml(tbody,html`<tr><td colspan="6"><div class="loading"><span class="spinner"></span></div></td></tr>`);
  try{
    const qs=status?`?status=${status}`:'';
    const r=await proxyApi('/admin/tickets'+qs);
    const tickets=await r.json();
    if(!tickets.length){renderHtml(tbody,html`<tr><td colspan="6" style="text-align:center;color:var(--text3)">Тикетов нет</td></tr>`);return;}
    renderHtml(tbody,html`${tickets.map(t=>html`
      <tr>
        <td>#${t.id}</td>
        <td><span style="color:${_TICKET_STATUS_COLORS[t.status]||'#888'};font-weight:600">${_TICKET_STATUS_LABELS[t.status]||t.status}</span></td>
        <td>${t.marzban_username||`tg:${t.telegram_id}`}</td>
        <td style="font-size:12px;color:var(--text2)">${_tsAgo(t.updated_at)}</td>
        <td style="font-size:12px;color:var(--text3)">${_fmtDate(t.created_at)}</td>
        <td><button data-action="open-ticket" data-ticket-id="${t.id}">Открыть</button></td>
      </tr>`)}`);
  }catch(e){renderHtml(tbody,html`<tr><td colspan="6" style="color:#f66">Ошибка загрузки</td></tr>`);}
}

async function openTicket(id){
  _currentTicketId=id;
  const r=await proxyApi(`/admin/tickets/${id}`);
  if(!r.ok)return;
  const {ticket,messages}=await r.json();
  document.getElementById('ticket-modal-title').textContent=
    `Тикет #${id} — ${ticket.marzban_username||`tg:${ticket.telegram_id}`} [${_TICKET_STATUS_LABELS[ticket.status]||ticket.status}]`;
  const chat=document.getElementById('ticket-chat');
  renderHtml(chat,messages.length?html`${messages.map(m=>{
    const bg=m.role==='user'?'var(--bg4)':m.role==='ai'?'#1a3a2a':'#2a2a1a';
    const label=m.role==='user'?'Пользователь':m.role==='ai'?'AI':'Оператор';
    return html`<div style="margin-bottom:8px;padding:8px;background:${bg};border-radius:6px">
      <div style="font-size:11px;color:var(--text3);margin-bottom:3px">${label} · ${_fmtDate(m.ts)}</div>
      <div style="white-space:pre-wrap;font-size:13px">${m.text}</div>
    </div>`;
  })}`:html`<div style="color:var(--text3);font-size:13px">Сообщений нет</div>`);
  chat.scrollTop=chat.scrollHeight;
  document.getElementById('ticket-reply-text').value='';
  document.getElementById('ticket-action-status').textContent='';
  document.getElementById('ticket-modal').classList.add('open');
}

async function sendTicketReply(){
  if(!_currentTicketId)return;
  const text=document.getElementById('ticket-reply-text').value.trim();
  if(!text)return;
  const status=document.getElementById('ticket-action-status');
  status.textContent='Отправка...';
  const r=await proxyApi(`/admin/tickets/${_currentTicketId}/reply`,{method:'POST',body:JSON.stringify({text})});
  if(!r.ok){status.textContent='Ошибка';return;}
  status.style.color='#6f6';status.textContent='Отправлено';
  setTimeout(()=>{status.textContent='';status.style.color='';},2000);
  document.getElementById('ticket-reply-text').value='';
  await openTicket(_currentTicketId);
}

async function closeTicket(){
  if(!_currentTicketId)return;
  const status=document.getElementById('ticket-action-status');
  status.textContent='Закрываю...';
  const r=await proxyApi(`/admin/tickets/${_currentTicketId}/close`,{method:'POST'});
  if(!r.ok){status.textContent='Ошибка';return;}
  closeModal('ticket-modal');
  loadTickets(document.getElementById('ticket-filter').value||undefined);
}

function _tsAgo(ts){
  const diff=Math.floor(Date.now()/1000)-ts;
  if(diff<60)return'только что';
  if(diff<3600)return`${Math.floor(diff/60)} мин назад`;
  if(diff<86400)return`${Math.floor(diff/3600)} ч назад`;
  return`${Math.floor(diff/86400)} дн назад`;
}
function _fmtDate(ts){return new Date(ts*1000).toLocaleString('ru',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});}
// TELEGRAM STARS
async function loadStarsSettings(){
  try{
    const r=await proxyApi('/admin/stars-settings');
    const data=await r.json();
    document.getElementById('stars-enabled').checked=!!data.enabled;
  }catch(e){}
}
async function saveStarsSettings(){
  const enabled=document.getElementById('stars-enabled').checked;
  await proxyApi('/admin/stars-settings',{method:'POST',body:JSON.stringify({enabled})});
}

async function loadStarsTariffs(){
  const tbody=document.getElementById('stars-tariffs-tbody');
  renderHtml(tbody,html`<tr><td colspan="5"><div class="loading"><span class="spinner"></span></div></td></tr>`);
  try{
    const r=await proxyApi('/admin/stars-tariffs');
    const tariffs=await r.json();
    if(!tariffs.length){
      renderHtml(tbody,html`<tr><td colspan="5" style="text-align:center;color:var(--text3)">Тарифы ещё не настроены — добавьте первый тариф</td></tr>`);
      return;
    }
    renderHtml(tbody,html`${tariffs.map(t=>html`
      <tr>
        <td>${t.name}</td>
        <td>${t.duration_days}</td>
        <td>${t.stars_price} ⭐️</td>
        <td><input type="checkbox" ${t.active?html`checked`:''} data-change-action="toggle-stars-tariff" data-tariff-id="${t.id}" style="width:auto" /></td>
        <td><button data-action="delete-stars-tariff" data-tariff-id="${t.id}">Удалить</button></td>
      </tr>`)}`);
  }catch(e){renderHtml(tbody,html`<tr><td colspan="5" style="color:#f66">Ошибка загрузки</td></tr>`);}
}

async function addStarsTariff(){
  const name=document.getElementById('new-tariff-name').value.trim();
  const duration_days=Number(document.getElementById('new-tariff-days').value);
  const stars_price=Number(document.getElementById('new-tariff-price').value);
  if(!name||!Number.isInteger(duration_days)||duration_days<1||duration_days>3650||
     !Number.isInteger(stars_price)||stars_price<1||stars_price>1000000)return;
  const r=await proxyApi('/admin/stars-tariffs',{method:'POST',body:JSON.stringify({name,duration_days,stars_price,active:true})});
  if(!r.ok)return;
  document.getElementById('new-tariff-name').value='';
  document.getElementById('new-tariff-days').value='';
  document.getElementById('new-tariff-price').value='';
  loadStarsTariffs();
}

async function toggleStarsTariff(id,active){
  const r=await proxyApi(`/admin/stars-tariffs/${id}/toggle`,{method:'POST',body:JSON.stringify({active})});
  if(!r.ok)loadStarsTariffs();
}

async function deleteStarsTariff(id){
  if(!confirm('Удалить тариф? На уже созданные счета это не повлияет.'))return;
  await proxyApi(`/admin/stars-tariffs/${id}`,{method:'DELETE'});
  loadStarsTariffs();
}

const _STARS_STATUS_COLORS={
  created:'#888',paid:'#4af',plan_committed:'#4af',applied:'#6f6',
  manual_review:'#fa4',apply_failed_user_missing:'#f66',
  apply_retry_exhausted:'#f66',refund_pending:'#fa4',refund_unknown:'#f66',refunded:'#a4f',
};
const _STARS_STATUS_LABELS={
  created:'Создан',paid:'Оплачен',plan_committed:'Тариф зафиксирован',applied:'Применён',
  manual_review:'Ручная проверка',apply_failed_user_missing:'Пользователь не найден',
  apply_retry_exhausted:'Повторы исчерпаны',refund_pending:'Возврат ожидается',
  refund_unknown:'Возврат не подтверждён',refunded:'Возвращён',
};
const _STARS_ACTIONABLE=new Set(['manual_review','apply_retry_exhausted']);
const _STARS_REFUNDABLE=new Set(['applied','manual_review','apply_retry_exhausted','apply_failed_user_missing']);

async function loadStarsPayments(status){
  const tbody=document.getElementById('stars-payments-tbody');
  renderHtml(tbody,html`<tr><td colspan="10"><div class="loading"><span class="spinner"></span></div></td></tr>`);
  try{
    const qs=status?`?status=${status}`:'';
    const r=await proxyApi('/admin/stars-payments'+qs);
    const rows=await r.json();
    if(!rows.length){renderHtml(tbody,html`<tr><td colspan="10" style="text-align:center;color:var(--text3)">Платежей нет</td></tr>`);return;}
    renderHtml(tbody,html`${rows.map(p=>{
      const actions=[];
      if(_STARS_ACTIONABLE.has(p.status)){
        actions.push(html`<button data-action="stars-payment-action" data-payment-id="${p.id}" data-payment-action="recheck">Проверить</button>`);
        actions.push(html`<button data-action="stars-payment-action" data-payment-id="${p.id}" data-payment-action="confirm-applied">Подтвердить</button>`);
        if(p.base_expire_observed!==null&&p.target_expire!==null){
          actions.push(html`<button data-action="stars-payment-action" data-payment-id="${p.id}" data-payment-action="requeue">Повторить</button>`);
        }
      }
      if(_STARS_REFUNDABLE.has(p.status)){
        actions.push(html`<button data-action="stars-payment-action" data-payment-id="${p.id}" data-payment-action="refund">Возврат</button>`);
      }
      if(p.status==='refund_pending'||p.status==='refund_unknown'){
        actions.push(html`<button data-action="stars-payment-action" data-payment-id="${p.id}" data-payment-action="reconcile-refund">Сверить возврат</button>`);
      }
      return html`<tr>
        <td>#${p.id}</td>
        <td>${p.marzban_username}</td>
        <td>${p.tariff_name} (${p.duration_days}д / ${p.stars_price}⭐️)</td>
        <td><span style="color:${_STARS_STATUS_COLORS[p.status]||'#888'};font-weight:600">${_STARS_STATUS_LABELS[p.status]||p.status}</span></td>
        <td>${p.created_by_telegram_id}</td>
        <td>${p.payer_telegram_id??'—'}</td>
        <td>${p.base_expire_observed??'—'} → ${p.target_expire??'—'}</td>
        <td>${p.applied_expire??'—'}</td>
        <td style="font-size:11px;color:var(--text3);max-width:220px;overflow:hidden;text-overflow:ellipsis">${p.manual_review_reason||''}</td>
        <td style="white-space:nowrap">${actions}</td>
      </tr>`;
    })}`);
  }catch(e){renderHtml(tbody,html`<tr><td colspan="10" style="color:#f66">Ошибка загрузки</td></tr>`);}
}

async function starsPaymentAction(id,action){
  if(action==='confirm-applied'&&!confirm('Подтвердить: зафиксировать текущее значение expire в Marzban как результат этого платежа?'))return;
  if(action==='refund'&&!confirm('Выполнить возврат Stars за этот платёж?'))return;
  const r=await proxyApi(`/admin/stars-payments/${id}/${action}`,{method:'POST'});
  const data=await r.json().catch(()=>({}));
  if(!r.ok){alert(data.error||'Ошибка');return;}
  if(data.message){alert(data.message);}
  const currentFilter=document.getElementById('stars-payments-filter').value;
  loadStarsPayments(currentFilter);
}

async function loadStarsOrphans(){
  const tbody=document.getElementById('stars-orphans-tbody');
  if(!tbody)return;
  renderHtml(tbody,html`<tr><td colspan="8"><div class="loading"><span class="spinner"></span></div></td></tr>`);
  try{
    const r=await proxyApi('/admin/stars-orphan-payments');
    const rows=await r.json();
    if(!rows.length){renderHtml(tbody,html`<tr><td colspan="8" style="text-align:center;color:var(--text3)">Непривязанных оплат нет</td></tr>`);return;}
    renderHtml(tbody,html`${rows.map(p=>{
      const actions=[];
      if(p.status==='manual_review')actions.push(html`<button data-action="stars-orphan-action" data-payment-id="${p.id}" data-payment-action="refund">Возврат</button>`);
      if(p.status==='refund_pending'||p.status==='refund_unknown')actions.push(html`<button data-action="stars-orphan-action" data-payment-id="${p.id}" data-payment-action="reconcile-refund">Сверить возврат</button>`);
      return html`<tr>
        <td>#${p.id}</td><td>${p.payer_telegram_id}</td>
        <td>${p.total_amount} ${p.currency}</td><td>${p.invoice_payload}</td>
        <td>${p.telegram_payment_charge_id}</td><td>${p.reason}</td>
        <td><span style="color:${_STARS_STATUS_COLORS[p.status]||'#888'};font-weight:600">${_STARS_STATUS_LABELS[p.status]||p.status}</span></td>
        <td style="white-space:nowrap">${actions}</td>
      </tr>`;
    })}`);
  }catch(e){renderHtml(tbody,html`<tr><td colspan="8" style="color:#f66">Ошибка загрузки</td></tr>`);}
}

async function starsOrphanAction(id,action){
  if(action==='refund'&&!confirm('Выполнить возврат Stars за эту непривязанную оплату?'))return;
  const r=await proxyApi(`/admin/stars-orphan-payments/${id}/${action}`,{method:'POST'});
  const data=await r.json().catch(()=>({}));
  if(!r.ok){alert(data.error||'Ошибка');return;}
  if(data.message)alert(data.message);
  loadStarsOrphans();
}

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
    case'load-nodes':work=nodesUi?.loadNodes();break;
    case'open-create-user':work=openCreateUser();break;
    case'create-user':work=createUser();break;
    case'open-user':work=openUser(username);break;
    case'open-user-from-node':closeModal('node-modal');work=openUser(username);break;
    case'change-device-limit':work=changeDeviceLimit(username,parseInteger(el.dataset.limit));break;
    case'admin-remove-device':work=adminRemoveDevice(parseInteger(el.dataset.deviceId),username);break;
    case'delete-user':work=deleteUser(username);break;
    case'disable-user':work=disableUser(username);break;
    case'enable-user':work=enableUser(username);break;
    case'reset-traffic':work=resetTraffic(username);break;
    case'save-user':work=saveUser(username);break;
    case'open-node':work=nodesUi?.openNode(nodeId);break;
    case'open-node-traffic':work=nodesUi?.openNodeTraffic(nodeId);break;
    case'open-node-settings':nodesUi?.openNodeSettings(nodeId);break;
    case'reconnect-node':event.stopPropagation();work=nodesUi?.reconnectNode(nodeId);break;
    case'save-node-settings':work=nodesUi?.saveNodeSettings(nodeId);break;
    case'add-quiet-hour':nodesUi?.addQuietHour();break;
    case'remove-quiet-hour':nodesUi?.removeQuietHour(parseInteger(el.dataset.quietIndex));break;
    case'toggle-nf-group':
      if(event.target.closest('input'))return;
      toggleNfGroup(el.dataset.target);break;
    case'add-global-config':work=configsUi?.addGlobalConfig();break;
    case'toggle-config':work=configsUi?.toggleConfig(parseInteger(el.dataset.configIndex));break;
    case'delete-config':work=configsUi?.deleteConfig(parseInteger(el.dataset.configIndex));break;
    case'add-per-user-config':work=configsUi?.addPerUserConfig();break;
    case'delete-per-user-config':work=configsUi?.deletePerUserConfig(username,parseInteger(el.dataset.configIndex));break;
    case'add-inbound-extra':work=configsUi?.addInboundExtra();break;
    case'delete-inbound-extra':work=configsUi?.deleteInboundExtra(el.dataset.inboundTag);break;
    case'format-inbound-extra':configsUi?.formatInboundExtraJson(el.dataset.inboundTag);break;
    case'update-inbound-extra':work=configsUi?.updateInboundExtra(el.dataset.inboundTag);break;
    case'save-settings':work=settingsUi?.saveSettings();break;
    case'save-bot-settings':work=settingsUi?.saveBotSettings();break;
    case'restart-bot':work=settingsUi?.restartBot();break;
    case'save-support-settings':work=settingsUi?.saveSupportSettings();break;
    case'open-ticket':work=openTicket(numericId);break;
    case'send-ticket-reply':work=sendTicketReply();break;
    case'close-ticket':work=closeTicket();break;
    case'add-stars-tariff':work=addStarsTariff();break;
    case'delete-stars-tariff':work=deleteStarsTariff(numericId);break;
    case'stars-payment-action':work=starsPaymentAction(numericId,el.dataset.paymentAction);break;
    case'stars-orphan-action':work=starsOrphanAction(numericId,el.dataset.paymentAction);break;
    case'routing-host-op':if(routingUi)routingUi.handleRoutingClick(el);break;
    case'open-legacy-transition':if(legacyTransitionsUi)legacyTransitionsUi.handleQueueClick(el);break;
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
    default:accountUi?.handleAccountClick(el,event);return;
  }
  if(work!==undefined)runAdminAction(work);
});

document.addEventListener('change',event=>{
  const el=event.target.closest('[data-change-action]');
  if(!el)return;
  let work;
  switch(el.dataset.changeAction){
    case'traffic-period':work=onTrafficPeriodChange();break;
    case'load-tickets':work=loadTickets(el.value||undefined);break;
    case'load-stars-payments':work=loadStarsPayments(el.value||undefined);break;
    case'save-stars-settings':work=saveStarsSettings();break;
    case'toggle-bot-proxy':settingsUi?.toggleBotProxy();break;
    case'nf-all-toggle':onNfAllToggle();break;
    case'nf-group-all-toggle':onNfGroupAllToggle(el);break;
    case'nf-cfg-toggle':onNfCfgToggle();break;
    case'toggle-stars-tariff':work=toggleStarsTariff(parseInteger(el.dataset.tariffId),el.checked);break;
    default:return;
  }
  if(work!==undefined)runAdminAction(work);
});

document.addEventListener('input',event=>{
  const el=event.target.closest('[data-input-action]');
  if(el?.dataset.inputAction==='filter-users')filterUsers();
});

// PH7-16 Wave 0B: the app-init trigger (restoreAdminSession()) moved to
// admin/app/main.js -- the composition root now calls it explicitly after
// dynamically import()-ing this module and registering `bootstrap` as
// auth.js's post-authentication callback (see main.js).
