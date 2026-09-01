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
const {api,proxyApi,adminFetch} = await import(`./admin/app/api.js${_MODULE_VERSION}`);
const {doLogin,doLogout} = await import(`./admin/app/auth.js${_MODULE_VERSION}`);

let allUsers = [];
let allNodes = [];
let nodeFilters = {};
let nodeSettings = {};
let dragIdx = null;
let perUserConfigs = {};
let userDeviceCounts = {};
let inboundClientExtras = {};
let accountUi = null;
let routingUi = null;
const ACCOUNT_UI_READY = import(`./admin/accounts.js${_MODULE_VERSION}`).then(module=>{
  accountUi=module.createAccountUi({adminFetch,html,renderHtml,showPage,toast});
  return accountUi;
});
const ROUTING_UI_READY = import(`./admin/routing.js${_MODULE_VERSION}`).then(module=>{
  routingUi=module.createRoutingUi({adminFetch,html,renderHtml,toast});
  return routingUi;
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
function nodeKey(id){return id===null||id===undefined||id===''?'null':String(id)}
function getNodeSetting(id){return nodeSettings[nodeKey(id)]||{currency:'USD',importance:'normal',can_remove:true}}
function importanceLabel(v){return{normal:'обычная',core:'важная',backup:'backup',test:'test',deprecated:'к выводу'}[v]||'обычная'}
function importanceClass(v){return{core:'badge-red',backup:'badge-blue',test:'badge-gray',deprecated:'badge-amber',normal:'badge-green'}[v]||'badge-green'}
function trafficCostLabel(setting,totalBytes){
  const price=Number(setting.traffic_price_per_tb);
  if(!Number.isFinite(price)||price<=0)return'—';
  const gb=(totalBytes||0)/1073741824;
  const included=Number(setting.traffic_included_gb)||0;
  const billable=Math.max(0,gb-included);
  const cost=billable/1024*price;
  return `${fmtMoney(cost,setting.currency)} за период`;
}
function billingGroupKey(id,setting){
  const group=(setting?.billing_group||'').trim();
  return group||`node:${nodeKey(id)}`;
}
function billingGroupLabel(id,setting){
  const group=(setting?.billing_group||'').trim();
  return group||`Нода ${nodeKey(id)}`;
}
function buildBillingGroups(usages){
  const groups={};
  (usages||[]).forEach(u=>{
    const s=getNodeSetting(u.node_id);
    const key=billingGroupKey(u.node_id,s);
    if(!groups[key])groups[key]={key,label:billingGroupLabel(u.node_id,s),total:0,nodes:[],setting:s};
    groups[key].total+=(u.uplink||0)+(u.downlink||0);
    groups[key].nodes.push(u.node_id);
    const current=groups[key].setting||{};
    groups[key].setting={
      ...current,
      ...s,
      traffic_included_gb:current.traffic_included_gb??s.traffic_included_gb,
      traffic_price_per_tb:current.traffic_price_per_tb??s.traffic_price_per_tb,
      currency:current.currency||s.currency||'USD',
    };
  });
  return groups;
}
function groupTrafficCostLabel(id,totalBytes,groups){
  const s=getNodeSetting(id);
  const key=billingGroupKey(id,s);
  const group=groups?.[key];
  if(!group)return trafficCostLabel(s,totalBytes);
  const label=trafficCostLabel(group.setting,group.total);
  if(label==='—')return'—';
  return group.nodes.length>1?`${label} · группа ${group.label}`:label;
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
  if(name==='nodes')loadNodes();
  if(name==='configs'){loadGlobalConfigs();loadPerUserConfigs();loadInboundExtras();}
  if(name==='settings'){loadSettings();loadBotSettings();loadSupportSettings();}
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

// NODES
async function loadNodes(){
  const period=getTrafficPeriod();
  const [nodesR,usageR,settingsR]=await Promise.all([api('/nodes'),api('/nodes/usage'+period.query),proxyApi('/admin/node-settings')]);
  const nodes=await nodesR.json();
  allNodes=nodes;
  const usage=await usageR.json();
  nodeSettings=settingsR.ok?await settingsR.json():{};
  const usageMap={};
  (usage.usages||[]).forEach(u=>usageMap[u.node_id??'null']=u);
  const billingGroups=buildBillingGroups(usage.usages||[]);

  renderHtml(document.getElementById('nodes-grid'),html`${nodes.map(n=>{
    const u=usageMap[n.id]||{uplink:0,downlink:0};
    const total=(u.uplink||0)+(u.downlink||0);
    const s=getNodeSetting(n.id);
    return html`<div class="node-card clickable" data-action="open-node" data-node-id="${n.id}">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
        <span class="dot ${n.status==='connected'?'dot-green':'dot-red'}"></span>
        <div class="node-name" style="flex:1">${n.name}</div>
        <button data-action="reconnect-node" data-node-id="${n.id}" style="padding:2px 8px;font-size:11px">⟳</button>
      </div>
      <div class="node-addr">${n.address}:${n.port}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0">
        <span class="badge ${importanceClass(s.importance)}">${importanceLabel(s.importance)}</span>
        <span class="badge ${s.can_remove?'badge-green':'badge-red'}">${s.can_remove?'можно убрать':'не трогать'}</span>
      </div>
      <div class="node-stats">
        <span>↑${fmt(u.uplink)}</span>
        <span>↓${fmt(u.downlink)}</span>
        <span style="color:var(--text3)">${n.xray_version||'?'}</span>
      </div>
      <div style="margin-top:10px;padding-top:10px;border-top:0.5px solid var(--border);font-size:12px;color:var(--text2)">
        <div style="display:flex;justify-content:space-between;gap:8px"><span>VPS / мес</span><b style="color:var(--text)">${fmtMoney(s.monthly_cost,s.currency)}</b></div>
        <div style="display:flex;justify-content:space-between;gap:8px"><span>Трафик</span><span>${s.traffic_price_per_tb?fmtMoney(s.traffic_price_per_tb,s.currency)+'/TB':'—'}</span></div>
        ${(s.provider||s.location)?html`<div style="margin-top:6px;color:var(--text3)">${[s.provider,s.location].filter(Boolean).join(' · ')}</div>`:''}
        ${s.billing_group?html`<div style="margin-top:4px;color:var(--text3)">группа: ${s.billing_group}</div>`:''}
        ${total&&s.traffic_price_per_tb?html`<div style="margin-top:4px;color:var(--text3)">${groupTrafficCostLabel(n.id,total,billingGroups)}</div>`:''}
      </div>
      <button data-action="open-node-settings" data-node-id="${n.id}" style="width:100%;margin-top:10px">Настроить</button>
    </div>`;
  })}`);

  const tbody=document.getElementById('node-traffic-tbody');
  renderHtml(tbody,html`${(usage.usages||[]).map(u=>{
    const total=u.uplink+u.downlink;
    const s=getNodeSetting(u.node_id);
    return html`<tr class="clickable" data-action="open-node-traffic" data-node-id="${u.node_id===null?'null':u.node_id}">
      <td>
        <div style="font-weight:500">${u.node_name}</div>
        <div style="font-size:11px;color:var(--text3)">${[s.provider,s.location].filter(Boolean).join(' · ')||importanceLabel(s.importance)}</div>
      </td>
      <td>${fmt(u.uplink)}</td>
      <td>${fmt(u.downlink)}</td>
      <td style="font-weight:500">${fmt(total)}</td>
      <td>${fmtMoney(s.monthly_cost,s.currency)}</td>
      <td>${groupTrafficCostLabel(u.node_id,total,billingGroups)}</td>
      <td><button data-action="open-node-settings" data-node-id="${u.node_id===null?'null':u.node_id}" style="padding:4px 10px;font-size:12px">Настроить</button></td>
    </tr>`;
  })}`);
}

async function reconnectNode(id){
  const r=await api('/node/'+id+'/reconnect',{method:'POST'});
  if(r.ok){toast('Reconnect послан');setTimeout(loadNodes,1000);}
  else toast('Ошибка','err');
}

function option(value,current,label){
  return html`<option value="${value}" ${value===current?html`selected`:''}>${label}</option>`;
}

function emptyToNumber(id){
  const raw=document.getElementById(id).value.trim().replace(',','.');
  if(raw==='')return null;
  const n=Number(raw);
  return Number.isFinite(n)&&n>=0?n:null;
}

function openNodeSettings(id){
  const node=allNodes.find(n=>sameNodeId(n.id,id));
  const s={currency:'USD',importance:'normal',can_remove:true,...getNodeSetting(id)};
  document.getElementById('node-modal-title').textContent=node?`Настройки ноды · ${node.name}`:'Настройки ноды';
  const body=document.getElementById('node-modal-body');
  renderHtml(body,html`
    <div style="font-size:13px;color:var(--text2);margin-bottom:1rem">
      Эти параметры хранятся только в MGBoost Panel и не меняют Marzban-ноду.
    </div>
    <label>Отображаемое имя (в боте)</label>
    <input type="text" id="node-display-name" maxlength="128" placeholder="${node?node.name:''}" value="${s.node_name||''}" style="margin-bottom:12px" />
    <div class="form-row">
      <div>
        <label>Провайдер</label>
        <input type="text" id="node-provider" maxlength="64" placeholder="Hetzner, Aeza..." value="${s.provider||''}" />
      </div>
      <div>
        <label>Локация</label>
        <input type="text" id="node-location" maxlength="64" placeholder="DE, NL, Estonia..." value="${s.location||''}" />
      </div>
    </div>
    <label>Группа тарификации</label>
    <input type="text" id="node-billing-group" maxlength="128" placeholder="например: Yandex Cloud / Москва" value="${s.billing_group||''}" />
    <div style="font-size:11px;color:var(--text3);margin-top:4px;margin-bottom:10px">
      Если несколько нод в одной группе, цена доп. трафика и включённый лимит считаются по суммарному трафику группы.
    </div>
    <div class="form-row">
      <div>
        <label>Стоимость VPS / месяц</label>
        <input type="number" id="node-monthly-cost" min="0" step="0.01" placeholder="например: 6.5" value="${s.monthly_cost??''}" />
      </div>
      <div>
        <label>Валюта</label>
        <input type="text" id="node-currency" maxlength="8" placeholder="USD" value="${s.currency||'USD'}" />
      </div>
    </div>
    <div class="form-row">
      <div>
        <label>Включённый трафик, GB</label>
        <input type="number" id="node-traffic-included" min="0" step="1" placeholder="пусто = неизвестно" value="${s.traffic_included_gb??''}" />
      </div>
      <div>
        <label>Цена доп. трафика за TB</label>
        <input type="number" id="node-traffic-price" min="0" step="0.01" placeholder="пусто = неизвестно" value="${s.traffic_price_per_tb??''}" />
      </div>
    </div>
    <div class="form-row">
      <div>
        <label>Роль ноды</label>
        <select id="node-importance">
          ${option('normal',s.importance,'Обычная')}
          ${option('core',s.importance,'Важная / core')}
          ${option('backup',s.importance,'Backup')}
          ${option('test',s.importance,'Тестовая')}
          ${option('deprecated',s.importance,'К выводу')}
        </select>
      </div>
      <div>
        <label>Кандидат на удаление</label>
        <select id="node-can-remove">
          <option value="true" ${s.can_remove?html`selected`:''}>Можно убрать, если метрики слабые</option>
          <option value="false" ${!s.can_remove?html`selected`:''}>Не трогать без ручного решения</option>
        </select>
      </div>
    </div>
    <label>Заметка</label>
    <textarea id="node-note" maxlength="512" rows="4" placeholder="Например: дешёвая, плохой провайдер, оставить как резерв...">${s.note||''}</textarea>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:1rem">
      <div class="detail-item"><div class="detail-label">Marzban ID</div><div class="detail-value">${node?node.id:'—'}</div></div>
      <div class="detail-item"><div class="detail-label">Адрес</div><div class="detail-value">${node?node.address:s.node_address||'—'}</div></div>
      <div class="detail-item"><div class="detail-label">Статус</div><div class="detail-value">${node?node.status:'—'}</div></div>
    </div>
    <div style="margin-top:1rem">
      <div style="font-size:12px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Тихие часы мониторинга (UTC)</div>
      <div style="font-size:12px;color:var(--text2);margin-bottom:8px">Во время тихих часов алерты в Telegram не отправляются (для прерываемых ВМ).</div>
      <div id="node-quiet-hours-list">${renderQuietHours(s.monitor_quiet_hours||[])}</div>
      <button style="margin-top:6px;font-size:12px" data-action="add-quiet-hour">+ Добавить окно</button>
    </div>
    <div class="modal-footer">
      <button data-action="close-modal" data-target="node-modal">Отмена</button>
      <button class="primary" data-action="save-node-settings" data-node-id="${id===null?'null':id}">Сохранить</button>
    </div>
  `);
  document.getElementById('node-modal').classList.add('open');
}

async function saveNodeSettings(id){
  const node=allNodes.find(n=>sameNodeId(n.id,id));
  const monthlyCost=emptyToNumber('node-monthly-cost');
  const trafficIncluded=emptyToNumber('node-traffic-included');
  const trafficPrice=emptyToNumber('node-traffic-price');
  if(monthlyCost===null&&document.getElementById('node-monthly-cost').value.trim()!==''){toast('Некорректная цена VPS','err');return}
  if(trafficIncluded===null&&document.getElementById('node-traffic-included').value.trim()!==''){toast('Некорректный включённый трафик','err');return}
  if(trafficPrice===null&&document.getElementById('node-traffic-price').value.trim()!==''){toast('Некорректная цена трафика','err');return}

  const payload={
    node_id:id,
    node_name:document.getElementById('node-display-name').value.trim()||(node?node.name:(getNodeSetting(id).node_name||'')),
    node_address:node?node.address:(getNodeSetting(id).node_address||''),
    billing_group:document.getElementById('node-billing-group').value.trim(),
    provider:document.getElementById('node-provider').value.trim(),
    location:document.getElementById('node-location').value.trim(),
    monthly_cost:monthlyCost,
    currency:(document.getElementById('node-currency').value.trim()||'USD').toUpperCase(),
    traffic_included_gb:trafficIncluded,
    traffic_price_per_tb:trafficPrice,
    importance:document.getElementById('node-importance').value,
    can_remove:document.getElementById('node-can-remove').value==='true',
    note:document.getElementById('node-note').value.trim(),
    monitor_quiet_hours:collectQuietHours(),
  };
  const r=await proxyApi('/admin/node-settings',{method:'POST',body:JSON.stringify(payload)});
  if(!r.ok){const e=await r.json().catch(()=>({error:'Ошибка'}));toast(e.error||'Ошибка','err');return}
  const saved=await r.json();
  nodeSettings[nodeKey(id)]=saved;
  toast('Настройки ноды сохранены');
  closeModal('node-modal');
  loadNodes();
}

function sameNodeId(a,b){
  return (a===null&&b===null)||String(a)===String(b);
}

async function loadUsersUsageForNode(id,period){
  try{
    const r=await api('/users/usage'+period.query);
    if(r.ok){
      const data=await r.json();
      const records=(data.usages||[]).filter(x=>sameNodeId(x.node_id,id));
      if(records.some(x=>x.username)){
        return records.map(x=>({username:x.username,traffic:x.used_traffic||0}));
      }
    }
  }catch(e){console.warn('users usage endpoint fallback',e)}

  if(!allUsers.length){const r=await api('/users?limit=500');allUsers=(await r.json()).users||[];}
  return Promise.all(allUsers.map(u=>
    api('/user/'+encodeURIComponent(u.username)+'/usage'+period.query).then(r=>r.json()).then(d=>{
      const rec=(d.usages||[]).find(x=>sameNodeId(x.node_id,id));
      return{username:u.username,traffic:rec?rec.used_traffic:0};
    }).catch(()=>({username:u.username,traffic:0}))
  ));
}

async function openNodeTraffic(id){
  const period=getTrafficPeriod();
  const node=allNodes.find(n=>sameNodeId(n.id,id));
  const usage=(window._dashNodeUsages||[]).find(u=>sameNodeId(u.node_id,id));
  const title=node?`${node.name} · ${node.address}`:(usage?usage.node_name:'Нода');
  document.getElementById('node-modal-title').textContent=`${title} · ${period.label}`;
  const body=document.getElementById('node-modal-body');
  renderHtml(body,html`<div class="loading"><span class="spinner"></span>Собираю трафик по клиентам...</div>`);
  document.getElementById('node-modal').classList.add('open');

  const results=await loadUsersUsageForNode(id,period);
  const sorted=results.filter(r=>r.traffic>0).sort((a,b)=>b.traffic-a.traffic);
  if(!sorted.length){renderHtml(body,html`<p style="color:var(--text3);padding:1rem 0">Нет трафика через эту ноду за выбранный период</p>`);return}
  renderHtml(body,html`<div class="table-wrap"><table>
    <thead><tr><th>Пользователь</th><th style="text-align:right">Трафик</th></tr></thead>
    <tbody>${sorted.map(r=>html`<tr class="clickable" data-action="open-user-from-node" data-username="${r.username}"><td>${r.username}</td><td style="text-align:right">${fmt(r.traffic)}</td></tr>`)}</tbody>
  </table></div>`);
}

async function openNode(id){
  return openNodeTraffic(id);
}

// GLOBAL CONFIGS
async function loadGlobalConfigs(){
  const r=await proxyApi('/admin/configs');
  const configs=await r.json();
  const list=document.getElementById('cfg-list');
  document.getElementById('cfg-count').textContent='('+configs.length+')';
  if(!configs.length){renderHtml(list,html`<p style="color:var(--text3);font-size:13px;padding:1rem 0">Нет конфигов</p>`);return}
  renderHtml(list,html`${configs.map((c,i)=>html`
    <div class="config-row" draggable="true" id="cfg-${i}" data-config-index="${i}">
      <span class="drag-handle">⠿</span>
      <div class="config-info">
        <div class="config-name-text">${c.name}</div>
        <div class="config-uri-text">${c.uri}</div>
      </div>
      <span class="badge ${c.enabled?'badge-green':'badge-red'}" style="cursor:pointer" data-action="toggle-config" data-config-index="${i}">${c.enabled?'вкл':'выкл'}</span>
      <button class="danger" style="padding:4px 10px;font-size:12px" data-action="delete-config" data-config-index="${i}">×</button>
    </div>
  `)}`);
  window._cfgs=configs;
}
async function addGlobalConfig(){
  const name=document.getElementById('cfg-name').value.trim();
  const uri=document.getElementById('cfg-uri').value.trim();
  const enabled=document.getElementById('cfg-enabled').value==='true';
  if(!uri){toast('URI обязателен','err');return}
  const r=await proxyApi('/admin/configs',{method:'POST',body:JSON.stringify({name:name||uri.slice(0,30),uri,enabled})});
  if(r.ok){toast('Добавлен');document.getElementById('cfg-name').value='';document.getElementById('cfg-uri').value='';loadGlobalConfigs();}
  else toast('Ошибка','err');
}
async function deleteConfig(idx){
   if(!confirm('Удалить?'))return;
   const cfg = window._cfgs[idx];
   if (!cfg || !cfg.id) {
       toast('Ошибка: конфиг не найден','err');
       return;
   }
   await proxyApi('/admin/configs/'+cfg.id,{method:'DELETE'});
   toast('Удалён');loadGlobalConfigs();
 }
async function toggleConfig(idx){
  const cfgs=window._cfgs||[];
  cfgs[idx].enabled=!cfgs[idx].enabled;
  await proxyApi('/admin/configs/reorder',{method:'POST',body:JSON.stringify(cfgs)});
  loadGlobalConfigs();
}
let _dragIdx=null;
function dragStart(i){_dragIdx=i;document.getElementById('cfg-'+i).style.opacity='0.4'}
function dragOver(e){e.preventDefault()}
function drop(i){
  if(_dragIdx===null||_dragIdx===i)return;
  const cfgs=window._cfgs||[];
  const moved=cfgs.splice(_dragIdx,1)[0];
  cfgs.splice(i,0,moved);
  proxyApi('/admin/configs/reorder',{method:'POST',body:JSON.stringify(cfgs)}).then(()=>loadGlobalConfigs());
}
function dragEnd(){_dragIdx=null;document.querySelectorAll('.config-row').forEach(r=>r.style.opacity='')}

// PER USER CONFIGS
async function loadPerUserConfigs(){
  const r=await proxyApi('/admin/per-user-configs');
  if(r.ok)perUserConfigs=await r.json();
  const username=document.getElementById('pu-user').value;
  renderPerUserConfigs(username);
}
document.getElementById('pu-user').addEventListener('change',e=>renderPerUserConfigs(e.target.value));
function renderPerUserConfigs(username){
  const configs=perUserConfigs[username]||[];
  const el=document.getElementById('per-user-configs');
  if(!configs.length){renderHtml(el,html`<p style="font-size:13px;color:var(--text3);padding:0.5rem 0">Нет индивидуальных конфигов</p>`);return}
  renderHtml(el,html`${configs.map((c,i)=>html`
    <div class="per-user-config">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:500">${c.name}</div>
        <div style="font-size:11px;color:var(--text3);font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c.uri}</div>
      </div>
      <button class="danger" style="padding:4px 10px;font-size:12px" data-action="delete-per-user-config" data-username="${username}" data-config-index="${i}">×</button>
    </div>
  `)}`);
}
async function addPerUserConfig(){
  const username=document.getElementById('pu-user').value;
  const uri=document.getElementById('pu-uri').value.trim();
  const name=document.getElementById('pu-name').value.trim();
  if(!uri){toast('URI обязателен','err');return}
  if(!perUserConfigs[username])perUserConfigs[username]=[];
  perUserConfigs[username].push({name:name||uri.slice(0,30),uri,enabled:true});
  await proxyApi('/admin/per-user-configs',{method:'POST',body:JSON.stringify(perUserConfigs)});
  toast('Добавлен');document.getElementById('pu-uri').value='';document.getElementById('pu-name').value='';
  renderPerUserConfigs(username);
}
async function deletePerUserConfig(username,idx){
  perUserConfigs[username].splice(idx,1);
  await proxyApi('/admin/per-user-configs',{method:'POST',body:JSON.stringify(perUserConfigs)});
  toast('Удалён');renderPerUserConfigs(username);
}

// INBOUND EXTRAS
async function loadInboundExtras(){
  if(!allInbounds||!Object.keys(allInbounds).length){
    try{const r=await api('/inbounds');allInbounds=await r.json();}catch{}
  }
  const dl=document.getElementById('ie-inbounds-list');
  const tags=[];
  Object.values(allInbounds).forEach(items=>items.forEach(i=>tags.push(i.tag)));
  renderHtml(dl,html`${tags.map(t=>html`<option value="${t}"></option>`)}`);

  try{
    const r=await proxyApi('/admin/settings');
    const data=await r.json();
    inboundClientExtras=data.inbound_client_extras||{};
  }catch{}
  renderInboundExtras();
}

function renderInboundExtras(){
  const list=document.getElementById('inbound-extra-list');
  const entries=Object.entries(inboundClientExtras);
  if(!entries.length){renderHtml(list,html`<p style="font-size:13px;color:var(--text3);padding:0.5rem 0">Нет добавленных параметров</p>`);return;}
  renderHtml(list,html`${entries.map(([tag,extra])=>html`
    <div style="background:var(--bg3);padding:10px;border-radius:8px;margin-bottom:10px;border:1px solid var(--border)">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <div style="font-weight:500;font-size:14px">${tag}</div>
        <button class="danger" style="padding:4px 10px;font-size:12px" data-action="delete-inbound-extra" data-inbound-tag="${tag}">Удалить</button>
      </div>
      <div style="font-size:12px;color:var(--text2);margin-bottom:4px">Параметры (URL query) или чистый JSON:</div>
      <textarea style="width:100%;height:100px;font-family:monospace;font-size:12px" data-inbound-value="${tag}" placeholder="extra=... или { &quot;xmux&quot;: ... }">${extra}</textarea>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:8px">
        <button data-action="format-inbound-extra" data-inbound-tag="${tag}">Сжать и закодировать JSON (если введён чистый JSON)</button>
        <button class="primary" data-action="update-inbound-extra" data-inbound-tag="${tag}">Сохранить</button>
      </div>
    </div>
  `)}`);
}

function inboundExtraField(tag){
  return [...document.querySelectorAll('[data-inbound-value]')].find(el=>el.dataset.inboundValue===tag);
}

async function addInboundExtra(){
  const input=document.getElementById('ie-inbound-select');
  const tag=input.value.trim();
  if(!tag){toast('Введите строку (например: type=xhttp или vless-xhttp)','err');return;}
  if(inboundClientExtras[tag]){toast('Уже добавлен','err');return;}
  inboundClientExtras[tag]='';
  input.value='';
  renderInboundExtras();
  await saveInboundExtras();
}

function formatInboundExtraJson(tag){
  const el=inboundExtraField(tag);
  if(!el)return;
  let val=el.value.trim();
  if(!val)return;
  // If it starts with { it might be raw JSON
  try{
    if(val.startsWith('{')){
      const j=JSON.parse(val);
      const str=JSON.stringify(j);
      val='extra='+encodeURIComponent(str);
      el.value=val;
      toast('JSON успешно закодирован');
    }else{
      toast('Не похоже на сырой JSON','err');
    }
  }catch(e){
    toast('Ошибка парсинга JSON: '+e.message,'err');
  }
}

async function updateInboundExtra(tag){
  const el=inboundExtraField(tag);
  if(!el)return;
  let val=el.value.trim();
  
  // auto-encode if user forgot and it's valid JSON
  if(val.startsWith('{')){
     try{
       const str=JSON.stringify(JSON.parse(val));
       val='extra='+encodeURIComponent(str);
       el.value=val;
       toast('JSON был автоматически закодирован');
     }catch(e){
       toast('Ошибка в JSON: '+e.message,'err');
       return;
     }
  }
  
  inboundClientExtras[tag]=val;
  await saveInboundExtras();
  toast('Сохранено');
}

async function deleteInboundExtra(tag){
  delete inboundClientExtras[tag];
  renderInboundExtras();
  await saveInboundExtras();
  toast('Удалено');
}

async function saveInboundExtras(){
  await proxyApi('/admin/settings',{method:'POST',body:JSON.stringify({inbound_client_extras:inboundClientExtras})});
}

let allInbounds={};
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

// SETTINGS
async function loadSettings(){
  const status=document.getElementById('settings-status');
  status.textContent='Загрузка...';
  try{
    const r=await proxyApi('/admin/settings');
    const data=await r.json();
    document.getElementById('set-sub-interval').value=data.sub_update_interval!=null?data.sub_update_interval:'';
    document.getElementById('set-block-contact').value=data.block_contact||'';
    document.getElementById('set-sub-title').value=data.sub_custom_title||'';
    document.getElementById('set-sub-desc').value=data.sub_custom_desc||'';
    status.textContent='';
  }catch(e){
    status.textContent='Ошибка загрузки настроек';
  }
}
async function saveSettings(){
  const status=document.getElementById('settings-status');
  const raw=document.getElementById('set-sub-interval').value.trim();
  const val=raw===''?null:parseInt(raw);
  if(val!==null&&(isNaN(val)||val<1||val>168)){
    status.textContent='Введите число от 1 до 168';
    return;
  }
  const contact=document.getElementById('set-block-contact').value.trim();
  const subTitle=document.getElementById('set-sub-title').value.trim();
  const subDesc=document.getElementById('set-sub-desc').value.trim();
  status.textContent='Сохранение...';
  try{
    await proxyApi('/admin/settings',{method:'POST',body:JSON.stringify({
      sub_update_interval:val,
      block_contact:contact||null,
      sub_custom_title:subTitle||null,
      sub_custom_desc:subDesc||null
    })});
    status.style.color='#6f6';
    status.textContent='Сохранено';
    setTimeout(()=>{status.textContent='';status.style.color='';},2000);
  }catch(e){
    status.style.color='';
    status.textContent='Ошибка сохранения';
  }
}

// QUIET HOURS
function renderQuietHours(list){
  if(!list||!list.length)return html`<div style="font-size:12px;color:var(--text3)">Не заданы</div>`;
  return html`${list.map((w,i)=>html`
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
      <input type="time" value="${w.from}" style="width:100px" data-qh-from="${i}" />
      <span style="font-size:12px;color:var(--text2)">—</span>
      <input type="time" value="${w.to}" style="width:100px" data-qh-to="${i}" />
      <button style="padding:2px 8px;font-size:12px" data-action="remove-quiet-hour" data-quiet-index="${i}">✕</button>
    </div>`)}`;
}
function addQuietHour(){
  const list=document.getElementById('node-quiet-hours-list');
  const existing=collectQuietHours();
  existing.push({from:'00:00',to:'01:00'});
  renderHtml(list,renderQuietHours(existing));
}
function removeQuietHour(i){
  const list=document.getElementById('node-quiet-hours-list');
  const existing=collectQuietHours();
  existing.splice(i,1);
  renderHtml(list,renderQuietHours(existing));
}
function collectQuietHours(){
  const froms=document.querySelectorAll('[data-qh-from]');
  const tos=document.querySelectorAll('[data-qh-to]');
  const result=[];
  froms.forEach((el,i)=>{
    const f=el.value.trim();
    const t=tos[i]?tos[i].value.trim():'';
    if(f&&t)result.push({from:f,to:t});
  });
  return result;
}

// BOT SETTINGS
function toggleBotProxy(){
  const on=document.getElementById('bot-proxy-enabled').checked;
  document.getElementById('bot-proxy-fields').style.display=on?'block':'none';
}
async function loadBotSettings(){
  try{
    const r=await proxyApi('/admin/bot-settings');
    if(!r.ok)return;
    const d=await r.json();
    document.getElementById('bot-enabled').checked=!!d.enabled;
    // Secrets are never sent back in plaintext — leave the field blank and
    // show a masked hint via placeholder; only a newly-typed value is saved.
    const tokenEl=document.getElementById('bot-token');
    tokenEl.value='';
    tokenEl.placeholder=d.token_set?'•••• настроено':'123456:ABCDEF...';
    document.getElementById('bot-channel').value=d.channel_id||'@MGBoost_News';
    document.getElementById('bot-proxy-enabled').checked=!!d.proxy_enabled;
    document.getElementById('bot-proxy-host').value=d.proxy_host||'';
    document.getElementById('bot-proxy-port').value=d.proxy_port||1080;
    document.getElementById('bot-proxy-user').value=d.proxy_user||'socks';
    const proxyPassEl=document.getElementById('bot-proxy-pass');
    proxyPassEl.value='';
    proxyPassEl.placeholder=d.proxy_pass_set?'•••• настроено':'telegram';
    toggleBotProxy();
  }catch(e){console.warn('loadBotSettings',e);}
}
async function saveBotSettings(){
  const status=document.getElementById('bot-settings-status');
  status.textContent='Сохранение...';
  try{
    const payload={
      enabled:document.getElementById('bot-enabled').checked,
      channel_id:document.getElementById('bot-channel').value.trim()||'@MGBoost_News',
      proxy_enabled:document.getElementById('bot-proxy-enabled').checked,
      proxy_host:document.getElementById('bot-proxy-host').value.trim(),
      proxy_port:parseInt(document.getElementById('bot-proxy-port').value)||1080,
      proxy_user:document.getElementById('bot-proxy-user').value.trim()||'socks',
    };
    // Only send secret fields if the admin actually typed a new value —
    // omitting the key means "keep the existing secret as-is".
    const newToken=document.getElementById('bot-token').value.trim();
    if(newToken)payload.token=newToken;
    const newProxyPass=document.getElementById('bot-proxy-pass').value.trim();
    if(newProxyPass)payload.proxy_pass=newProxyPass;
    const r=await proxyApi('/admin/bot-settings',{method:'POST',body:JSON.stringify(payload)});
    if(!r.ok){const e=await r.json().catch(()=>({}));status.textContent=e.error||'Ошибка';return;}
    status.style.color='#6f6';status.textContent='Сохранено';
    setTimeout(()=>{status.textContent='';status.style.color='';},2000);
    loadBotSettings();
  }catch(e){status.style.color='';status.textContent='Ошибка';}
}

async function restartBot(){
  const status=document.getElementById('bot-settings-status');
  status.textContent='Перезапуск...';
  try{
    const r=await proxyApi('/admin/bot-restart',{method:'POST'});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){status.textContent=d.error||'Ошибка';return;}
    status.style.color='#6f6';
    status.textContent=d.started?'Бот запущен':'Бот остановлен (токен не задан)';
    setTimeout(()=>{status.textContent='';status.style.color='';},3000);
  }catch(e){status.style.color='';status.textContent='Ошибка';}
}

// SUPPORT SETTINGS
async function loadSupportSettings(){
  try{
    const r=await proxyApi('/admin/bot-settings');
    if(!r.ok)return;
    const d=await r.json();
    document.getElementById('bot-support-enabled').checked=!!d.support_enabled;
    const keyEl=document.getElementById('bot-openrouter-key');
    keyEl.value='';
    keyEl.placeholder=d.openrouter_api_key_set?'•••• настроено':'sk-or-v1-...';
    document.getElementById('bot-openrouter-model').value=d.openrouter_model||'openai/gpt-4o-mini';
    document.getElementById('bot-admin-tg-id').value=d.admin_tg_id||'';
    document.getElementById('bot-support-faq').value=d.support_faq||'';
  }catch(e){console.warn('loadSupportSettings',e);}
}
async function saveSupportSettings(){
  const status=document.getElementById('support-settings-status');
  status.textContent='Сохранение...';
  try{
    const payload={
      support_enabled:document.getElementById('bot-support-enabled').checked,
      openrouter_model:document.getElementById('bot-openrouter-model').value.trim()||'openai/gpt-4o-mini',
      admin_tg_id:document.getElementById('bot-admin-tg-id').value.trim(),
      support_faq:document.getElementById('bot-support-faq').value,
    };
    // Only send the key if the admin actually typed a new one — omitting
    // it means "keep the existing key as-is".
    const newKey=document.getElementById('bot-openrouter-key').value.trim();
    if(newKey)payload.openrouter_api_key=newKey;
    const r=await proxyApi('/admin/bot-settings',{method:'POST',body:JSON.stringify(payload)});
    if(!r.ok){const e=await r.json().catch(()=>({}));status.textContent=e.error||'Ошибка';return;}
    status.style.color='#6f6';status.textContent='Сохранено';
    setTimeout(()=>{status.textContent='';status.style.color='';},2000);
    loadSupportSettings();
  }catch(e){status.style.color='';status.textContent='Ошибка';}
}

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
    case'load-nodes':work=loadNodes();break;
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
    case'open-node':work=openNode(nodeId);break;
    case'open-node-traffic':work=openNodeTraffic(nodeId);break;
    case'open-node-settings':openNodeSettings(nodeId);break;
    case'reconnect-node':event.stopPropagation();work=reconnectNode(nodeId);break;
    case'save-node-settings':work=saveNodeSettings(nodeId);break;
    case'add-quiet-hour':addQuietHour();break;
    case'remove-quiet-hour':removeQuietHour(parseInteger(el.dataset.quietIndex));break;
    case'toggle-nf-group':
      if(event.target.closest('input'))return;
      toggleNfGroup(el.dataset.target);break;
    case'add-global-config':work=addGlobalConfig();break;
    case'toggle-config':work=toggleConfig(parseInteger(el.dataset.configIndex));break;
    case'delete-config':work=deleteConfig(parseInteger(el.dataset.configIndex));break;
    case'add-per-user-config':work=addPerUserConfig();break;
    case'delete-per-user-config':work=deletePerUserConfig(username,parseInteger(el.dataset.configIndex));break;
    case'add-inbound-extra':work=addInboundExtra();break;
    case'delete-inbound-extra':work=deleteInboundExtra(el.dataset.inboundTag);break;
    case'format-inbound-extra':formatInboundExtraJson(el.dataset.inboundTag);break;
    case'update-inbound-extra':work=updateInboundExtra(el.dataset.inboundTag);break;
    case'save-settings':work=saveSettings();break;
    case'save-bot-settings':work=saveBotSettings();break;
    case'restart-bot':work=restartBot();break;
    case'save-support-settings':work=saveSupportSettings();break;
    case'open-ticket':work=openTicket(numericId);break;
    case'send-ticket-reply':work=sendTicketReply();break;
    case'close-ticket':work=closeTicket();break;
    case'add-stars-tariff':work=addStarsTariff();break;
    case'delete-stars-tariff':work=deleteStarsTariff(numericId);break;
    case'stars-payment-action':work=starsPaymentAction(numericId,el.dataset.paymentAction);break;
    case'stars-orphan-action':work=starsOrphanAction(numericId,el.dataset.paymentAction);break;
    case'routing-host-op':if(routingUi)routingUi.handleRoutingClick(el);break;
    case'close-modal':closeModal(el.dataset.target);break;
    default:return;
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
    case'toggle-bot-proxy':toggleBotProxy();break;
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

document.addEventListener('dragstart',event=>{
  const row=event.target.closest('.config-row[data-config-index]');
  if(row)dragStart(parseInteger(row.dataset.configIndex));
});
document.addEventListener('dragover',event=>{
  if(event.target.closest('.config-row[data-config-index]'))dragOver(event);
});
document.addEventListener('drop',event=>{
  const row=event.target.closest('.config-row[data-config-index]');
  if(row){event.preventDefault();drop(parseInteger(row.dataset.configIndex));}
});
document.addEventListener('dragend',dragEnd);

// PH7-16 Wave 0B: the app-init trigger (restoreAdminSession()) moved to
// admin/app/main.js -- the composition root now calls it explicitly after
// dynamically import()-ing this module and registering `bootstrap` as
// auth.js's post-authentication callback (see main.js).
