const MARZBAN = '';
const PROXY_API = '/sub-admin-api';
let TOKEN = localStorage.getItem('mz_token') || '';
let allUsers = [];
let allNodes = [];
let nodeFilters = {};
let nodeSettings = {};
let dragIdx = null;
let perUserConfigs = {};
let userDeviceCounts = {};

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
  if(days<0)return'<span style="color:var(--red)">просрочен</span>';
  if(days===0)return'<span style="color:var(--amber)">сегодня</span>';
  if(days<=3)return`<span style="color:var(--amber)">${days}д</span>`;
  return`${days}д`;
}
function fmtOnline(dt){
  if(!dt)return'—';
  const d=parseUTC(dt);
  const diff=(Date.now()-d)/1000;
  if(diff<120)return'<span style="color:var(--green)">сейчас</span>';
  if(diff<3600)return`${Math.floor(diff/60)}м назад`;
  if(diff<86400)return`${Math.floor(diff/3600)}ч назад`;
  return`${Math.floor(diff/86400)}д назад`;
}
function statusBadge(s){
  const m={active:'badge-green',disabled:'badge-red',expired:'badge-red',limited:'badge-amber',on_hold:'badge-gray'};
  const l={active:'активен',disabled:'выкл',expired:'истёк',limited:'лимит',on_hold:'на паузе'};
  return`<span class="badge ${m[s]||'badge-gray'}">${l[s]||s}</span>`;
}
function toast(msg,type='ok'){
  const t=document.getElementById('toast');
  t.textContent=msg;t.className='show '+(type==='ok'?'ok':'err');
  clearTimeout(t._t);t._t=setTimeout(()=>t.className='',2500);
}

async function api(path,opts={}){
  const r=await fetch(MARZBAN+'/api'+path,{...opts,headers:{Authorization:'Bearer '+TOKEN,'Content-Type':'application/json',...(opts.headers||{})}});
  if(r.status===401){doLogout();throw new Error('unauth')}
  return r;
}
async function proxyApi(path,opts={}){
  return fetch(PROXY_API+path,{...opts,headers:{Authorization:'Bearer '+TOKEN,'Content-Type':'application/json',...(opts.headers||{})}});
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

// AUTH
async function doLogin(){
  const u=document.getElementById('login-user').value.trim();
  const p=document.getElementById('login-pass').value;
  const e=document.getElementById('login-err');
  e.style.display='none';
  try{
    const r=await fetch(MARZBAN+'/api/admin/token',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:`username=${encodeURIComponent(u)}&password=${encodeURIComponent(p)}`});
    const d=await r.json();
    if(!d.access_token)throw new Error();
    TOKEN=d.access_token;
    localStorage.setItem('mz_token',TOKEN);
    document.getElementById('sidebar-admin').textContent=u;
    document.getElementById('login-page').style.display='none';
    document.getElementById('app').style.display='flex';
    bootstrap();
  }catch{e.style.display='block'}
}
function doLogout(){
  localStorage.removeItem('mz_token');TOKEN='';
  document.getElementById('app').style.display='none';
  document.getElementById('login-page').style.display='flex';
}
document.getElementById('login-pass').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});

// NAV
function showPage(name){
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  document.getElementById('page-'+name).classList.add('active');
  document.querySelector(`[data-page="${name}"]`).classList.add('active');
  if(name==='nodes')loadNodes();
  if(name==='configs'){loadGlobalConfigs();loadPerUserConfigs();}
  if(name==='settings')loadSettings();
}
function switchTab(id,el){
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  el.classList.add('active');
}

// DASHBOARD
async function loadDashboard(){
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
  document.getElementById('sys-stats').innerHTML=`
    <div class="stat-card"><div class="stat-label">Пользователи</div><div class="stat-value">${sys.total_user}</div><div class="stat-sub">${sys.users_active} активных · ${sys.users_expired} истекших</div></div>
    <div class="stat-card"><div class="stat-label">Онлайн</div><div class="stat-value" style="color:var(--green)">${sys.online_users}</div><div class="stat-sub">прямо сейчас</div></div>
    <div class="stat-card"><div class="stat-label">Входящий трафик</div><div class="stat-value">${fmt(sys.incoming_bandwidth)}</div><div class="stat-sub">${fmt(sys.incoming_bandwidth_speed)}/с</div></div>
    <div class="stat-card"><div class="stat-label">Исходящий трафик</div><div class="stat-value">${fmt(sys.outgoing_bandwidth)}</div><div class="stat-sub">${fmt(sys.outgoing_bandwidth_speed)}/с</div></div>
    <div class="stat-card"><div class="stat-label">CPU</div><div class="stat-value">${sys.cpu_usage.toFixed(1)}%</div><div class="stat-sub">${sys.cpu_cores} ядр</div></div>
    <div class="stat-card"><div class="stat-label">RAM</div><div class="stat-value">${mem}%</div><div class="stat-sub">${fmt(sys.mem_used)} / ${fmt(sys.mem_total)}</div></div>
  `;
  document.getElementById('dash-nodes').innerHTML=nodes.map(n=>`
    <div style="display:flex;align-items:center;gap:8px;padding:8px 0;border-bottom:0.5px solid var(--border)">
      <span class="dot ${n.status==='connected'?'dot-green':'dot-red'}"></span>
      <div style="flex:1">
        <div style="font-size:13px">${n.name}</div>
        <div style="font-size:11px;color:var(--text2)">${n.address} · xray ${n.xray_version||'?'}</div>
      </div>
      <span class="badge ${n.status==='connected'?'badge-green':'badge-red'}">${n.status==='connected'?'ок':'офф'}</span>
    </div>
  `).join('');
  const totalTraffic=usages.reduce((s,u)=>s+u.uplink+u.downlink,0);
  document.getElementById('dash-node-traffic').innerHTML=usages.length?usages.map(u=>{
    const total=u.uplink+u.downlink;
    const pct=totalTraffic>0?Math.round(total/totalTraffic*100):0;
    return`<div class="clickable" style="padding:8px 0;border-bottom:0.5px solid var(--border)" onclick="openNodeTraffic(${u.node_id===null?'null':u.node_id})">
      <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
        <span style="color:var(--text2)">${u.node_name}</span>
        <span>${fmt(total)}</span>
      </div>
      <div class="traffic-bar"><div class="traffic-fill" style="width:${pct}%"></div></div>
    </div>`;
  }).join(''):'<p style="color:var(--text3);font-size:13px;padding:1rem 0">Нет трафика за период</p>';
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
  sel.innerHTML=allUsers.map(u=>`<option value="${u.username}">${u.username}</option>`).join('');
}
function filterUsers(){
  const q=document.getElementById('user-search').value.toLowerCase();
  renderUsers(allUsers.filter(u=>u.username.toLowerCase().includes(q)||(u.note||'').toLowerCase().includes(q)));
}
function renderUsers(users){
  document.getElementById('users-tbody').innerHTML=users.map(u=>{
    const f=nodeFilters[u.username];
    const hasFilter=f&&f.all===false&&(f.allowed_configs||[]).length>0;
    return`
    <tr class="clickable" onclick="openUser('${u.username}')">
      <td>
        <div style="font-weight:500">${u.username}${hasFilter?` <span style="font-size:10px;color:var(--amber);border:0.5px solid var(--amber2);border-radius:3px;padding:1px 5px;vertical-align:middle">фильтр</span>`:''}</div>
        ${u.note?`<div style="font-size:11px;color:var(--text2)">${u.note}</div>`:''}
      </td>
      <td>${statusBadge(u.status)}</td>
      <td>${fmt(u.used_traffic)}${u.data_limit?` / ${fmt(u.data_limit)}`:'  / ∞'}</td>
      <td>${fmtRelDate(u.expire)}</td>
      <td style="font-size:11px;color:var(--text2);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${(u.sub_last_user_agent||'—').split('/')[0]}</td>
      <td>${userDeviceCounts[u.username]??0}</td>
      <td>${fmtOnline(u.online_at)}</td>
      <td><button onclick="event.stopPropagation();openUser('${u.username}')">···</button></td>
    </tr>
  `;
  }).join('');
}

// USER DETAIL MODAL
async function openUser(username){
  const modal=document.getElementById('user-modal');
  const body=document.getElementById('user-modal-body');
  const footer=document.getElementById('user-modal-footer');
  document.getElementById('user-modal-title').textContent=username;
  body.innerHTML='<div class="loading"><span class="spinner"></span>Загрузка...</div>';
  footer.innerHTML='';
  modal.classList.add('open');

  const [userR,usageR,devR]=await Promise.all([
    api('/user/'+username),
    api('/user/'+username+'/usage'),
    proxyApi('/admin/user-devices/'+encodeURIComponent(username)),
  ]);
  const u=await userR.json();
  const usage=await usageR.json();
  const devData=devR.ok?await devR.json():null;

  const usageByNode=usage.usages||[];
  const totalUsage=usageByNode.reduce((s,n)=>s+n.used_traffic,0);

  body.innerHTML=`
    <div class="user-detail-grid">
      <div class="detail-item"><div class="detail-label">Статус</div><div class="detail-value">${statusBadge(u.status)}</div></div>
      <div class="detail-item"><div class="detail-label">Использовано</div><div class="detail-value">${fmt(u.used_traffic)}</div></div>
      <div class="detail-item"><div class="detail-label">Лимит</div><div class="detail-value">${u.data_limit?fmt(u.data_limit):'∞'}</div></div>
      <div class="detail-item"><div class="detail-label">Истекает</div><div class="detail-value">${fmtDate(u.expire)}</div></div>
      <div class="detail-item"><div class="detail-label">Создан</div><div class="detail-value">${fmtDate(u.created_at)}</div></div>
      <div class="detail-item"><div class="detail-label">Онлайн</div><div class="detail-value">${fmtOnline(u.online_at)}</div></div>
    </div>
    ${u.note?`<div style="background:var(--bg4);border-radius:8px;padding:10px 12px;margin-bottom:1rem;font-size:13px"><span style="color:var(--text2)">Заметка: </span>${u.note}</div>`:''}
    <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Трафик по нодам</div>
    <div class="usage-node-list">
      ${usageByNode.map(n=>{
        const pct=totalUsage>0?Math.round(n.used_traffic/totalUsage*100):0;
        return`<div class="usage-node-item">
          <div style="display:flex;justify-content:space-between">
            <span class="usage-node-name">${n.node_name}</span>
            <span class="usage-node-val">${fmt(n.used_traffic)} <span style="color:var(--text3);font-size:11px">${pct}%</span></span>
          </div>
          <div class="traffic-bar" style="margin-top:6px"><div class="traffic-fill" style="width:${pct}%"></div></div>
        </div>`;
      }).join('')}
    </div>
    <div style="margin-top:1rem">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
        <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em">Устройства (HWID)</div>
        <button style="font-size:11px;padding:3px 10px" onclick="changeDeviceLimit('${username}',${devData?devData.limit:3})">Изменить лимит</button>
      </div>
      ${devData?`
        <div style="font-size:12px;color:var(--text2);margin-bottom:8px">
          Активных: <b>${devData.active_count}</b> / <b>${devData.limit===0?'∞':devData.limit}</b>
        </div>
        ${devData.devices.length?devData.devices.map(d=>`
          <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:0.5px solid var(--border);font-size:12px">
            <div>
              <span style="${d.is_active?'color:var(--green)':'color:var(--text3)'}">${d.is_active?'●':'○'}</span>
              <span style="margin-left:6px;font-weight:500">${esc(d.display_name||d.device_name||d.client_name||'Устройство')}</span>
              <span style="color:var(--text2);margin-left:6px">${esc(d.platform||'')}${d.client_name?' · '+esc(d.client_name):''}</span>
            </div>
            <div style="display:flex;align-items:center;gap:8px">
              <span style="color:var(--text3)">${fmtOnline(new Date(d.last_seen*1000).toISOString())}</span>
              <button style="font-size:11px;padding:2px 8px;color:var(--red)" onclick="adminRemoveDevice(${d.id},'${username}')">✕</button>
            </div>
          </div>`).join(''):'<div style="color:var(--text3);font-size:12px">Нет зарегистрированных устройств</div>'}
      `:'<div style="color:var(--text3);font-size:12px">Нет данных</div>'}
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
  `;

  footer.innerHTML=`
    <button class="danger" onclick="deleteUser('${username}')">Удалить</button>
    <button onclick="${u.status==='active'?`disableUser('${username}')`:`enableUser('${username}')`}">${u.status==='active'?'Отключить':'Включить'}</button>
    <button onclick="resetTraffic('${username}')">Сбросить трафик</button>
    <button class="primary" onclick="saveUser('${username}')">Сохранить</button>
  `;

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
    sec.innerHTML=`
      <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Фильтр конфигов</div>
      <p style="font-size:12px;color:var(--text3)">Нет ссылок в подписке</p>`;
    return;
  }

  const groupRows=hostOrder.map(ip=>{
    const nodeName=nodeByAddr[ip]||null;
    const configs=hostConfigs[ip];
    const safeIp=ip.replace(/\./g,'-');

    const configChecks=configs.map(cfg=>{
      const checked=isAll||allowedSet.has(cfg);
      return`<label style="display:flex;align-items:center;gap:6px;padding:3px 0;cursor:pointer">
        <input type="checkbox" class="nf-cfg" data-cfg="${esc(cfg)}" ${checked?'checked':''} style="width:auto" onchange="onNfCfgToggle()" />
        <span style="font-size:12px">${esc(cfg)}</span>
      </label>`;
    }).join('');

    const allGroupChecked=isAll||configs.every(c=>allowedSet.has(c));

    return`<div style="margin:8px 0;border:1px solid var(--border);border-radius:6px;overflow:hidden">
      <div style="display:flex;align-items:center;gap:8px;padding:7px 10px;background:var(--bg3);cursor:pointer" onclick="toggleNfGroup('nfg-${safeIp}')">
        <input type="checkbox" class="nf-group-all" data-ip="${esc(ip)}" ${allGroupChecked?'checked':''} style="width:auto" onclick="event.stopPropagation();onNfGroupAllToggle('${esc(ip)}')" />
        <span style="font-size:13px;flex:1">
          ${nodeName?`<span style="color:var(--text)">${esc(nodeName)}</span> `:''}
          <span style="font-family:monospace;font-size:11px;color:var(--text3)">${esc(ip)}</span>
        </span>
        <span style="font-size:11px;color:var(--text3)">${configs.length} конф.</span>
      </div>
      <div id="nfg-${safeIp}" style="padding:6px 10px 6px 28px">${configChecks}</div>
    </div>`;
  }).join('');

  sec.innerHTML=`
    <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:8px">Фильтр конфигов</div>
    <label style="display:flex;align-items:center;gap:8px;margin-bottom:10px;cursor:pointer">
      <input type="checkbox" id="nf-all" ${isAll?'checked':''} onchange="onNfAllToggle()" style="width:auto" />
      <span style="font-size:13px">Все конфиги (без фильтра)</span>
    </label>
    <div id="nf-list" style="${isAll?'opacity:0.45;pointer-events:none':''}">${groupRows}</div>`;
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

function onNfGroupAllToggle(ip){
  const groupCb=document.querySelector(`.nf-group-all[data-ip="${ip}"]`);
  document.querySelectorAll(`.nf-cfg`).forEach(cb=>{
    // only touch configs belonging to this group (those inside the same group container)
  });
  // find the group div by ip
  const safeIp=ip.replace(/\./g,'-');
  const groupDiv=document.getElementById('nfg-'+safeIp);
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
    const ip=groupCb.dataset.ip;
    const safeIp=ip.replace(/\./g,'-');
    const groupDiv=document.getElementById('nfg-'+safeIp);
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

  const r=await api('/user/'+username,{method:'PUT',body:JSON.stringify(body)});
  if(r.ok){toast('Сохранено');closeModal('user-modal');loadUsers();}
  else toast('Ошибка','err');
}
async function deleteUser(username){
  if(!confirm('Удалить '+username+'?'))return;
  const r=await api('/user/'+username,{method:'DELETE'});
  if(r.ok){toast('Удалён');closeModal('user-modal');loadUsers();}
  else toast('Ошибка','err');
}
async function disableUser(username){
  const r=await api('/user/'+username,{method:'PUT',body:JSON.stringify({status:'disabled'})});
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
  const r=await api('/user/'+username,{method:'PUT',body:JSON.stringify({status:'active'})});
  if(r.ok){toast('Включён');openUser(username);loadUsers();}
}
async function resetTraffic(username){
  if(!confirm('Сбросить трафик '+username+'?'))return;
  const r=await api('/user/'+username+'/reset',{method:'POST'});
  if(r.ok){toast('Трафик сброшен');openUser(username);loadUsers();}
}

// CREATE USER
async function openCreateUser(){
  document.getElementById('create-modal').classList.add('open');
  const el=document.getElementById('new-inbounds');
  if(!allInbounds||!Object.keys(allInbounds).length){
    try{const r=await api('/inbounds');allInbounds=await r.json();}catch{}
  }
  el.innerHTML=Object.entries(allInbounds).map(([proto,items])=>`
    <div style="margin-bottom:8px">
      <div style="font-size:12px;color:var(--text2);margin-bottom:4px;text-transform:uppercase">${esc(proto)}</div>
      <div style="display:flex;flex-wrap:wrap;gap:6px 12px;margin-left:4px">
        ${items.map(it=>`<label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:12px">
          <input type="checkbox" class="nu-ib" data-proto="${esc(proto)}" data-tag="${esc(it.tag)}" checked style="width:auto" />
          <span>${esc(it.tag)}</span>
        </label>`).join('')}
      </div>
    </div>
  `).join('')||'<p style="color:var(--text3);font-size:12px">Нет inbounds</p>';
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

  document.getElementById('nodes-grid').innerHTML=nodes.map(n=>{
    const u=usageMap[n.id]||{uplink:0,downlink:0};
    const total=(u.uplink||0)+(u.downlink||0);
    const s=getNodeSetting(n.id);
    return`<div class="node-card clickable" onclick="openNode(${n.id})">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
        <span class="dot ${n.status==='connected'?'dot-green':'dot-red'}"></span>
        <div class="node-name" style="flex:1">${esc(n.name)}</div>
        <button onclick="event.stopPropagation();reconnectNode(${n.id})" style="padding:2px 8px;font-size:11px">⟳</button>
      </div>
      <div class="node-addr">${esc(n.address)}:${n.port}</div>
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
        ${(s.provider||s.location)?`<div style="margin-top:6px;color:var(--text3)">${esc([s.provider,s.location].filter(Boolean).join(' · '))}</div>`:''}
        ${total&&s.traffic_price_per_tb?`<div style="margin-top:4px;color:var(--text3)">${trafficCostLabel(s,total)}</div>`:''}
      </div>
      <button onclick="event.stopPropagation();openNodeSettings(${n.id})" style="width:100%;margin-top:10px">Настроить</button>
    </div>`;
  }).join('');

  const tbody=document.getElementById('node-traffic-tbody');
  tbody.innerHTML=(usage.usages||[]).map(u=>{
    const total=u.uplink+u.downlink;
    const s=getNodeSetting(u.node_id);
    return`<tr class="clickable" onclick="openNodeTraffic(${u.node_id===null?'null':u.node_id})">
      <td>
        <div style="font-weight:500">${esc(u.node_name)}</div>
        <div style="font-size:11px;color:var(--text3)">${esc([s.provider,s.location].filter(Boolean).join(' · ')||importanceLabel(s.importance))}</div>
      </td>
      <td>${fmt(u.uplink)}</td>
      <td>${fmt(u.downlink)}</td>
      <td style="font-weight:500">${fmt(total)}</td>
      <td>${fmtMoney(s.monthly_cost,s.currency)}</td>
      <td>${trafficCostLabel(s,total)}</td>
      <td><button onclick="event.stopPropagation();openNodeSettings(${u.node_id===null?'null':u.node_id})" style="padding:4px 10px;font-size:12px">Настроить</button></td>
    </tr>`;
  }).join('');
}

async function reconnectNode(id){
  const r=await api('/node/'+id+'/reconnect',{method:'POST'});
  if(r.ok){toast('Reconnect послан');setTimeout(loadNodes,1000);}
  else toast('Ошибка','err');
}

function option(value,current,label){
  return `<option value="${esc(value)}" ${value===current?'selected':''}>${esc(label)}</option>`;
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
  body.innerHTML=`
    <div style="font-size:13px;color:var(--text2);margin-bottom:1rem">
      Эти параметры хранятся только в MGBoost Panel и не меняют Marzban-ноду. Они нужны для аналитики, рекомендаций и будущего LLM-ассистента.
    </div>
    <div class="form-row">
      <div>
        <label>Провайдер</label>
        <input type="text" id="node-provider" maxlength="64" placeholder="Hetzner, Aeza..." value="${esc(s.provider||'')}" />
      </div>
      <div>
        <label>Локация</label>
        <input type="text" id="node-location" maxlength="64" placeholder="DE, NL, Estonia..." value="${esc(s.location||'')}" />
      </div>
    </div>
    <div class="form-row">
      <div>
        <label>Стоимость VPS / месяц</label>
        <input type="number" id="node-monthly-cost" min="0" step="0.01" placeholder="например: 6.5" value="${s.monthly_cost??''}" />
      </div>
      <div>
        <label>Валюта</label>
        <input type="text" id="node-currency" maxlength="8" placeholder="USD" value="${esc(s.currency||'USD')}" />
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
          <option value="true" ${s.can_remove?'selected':''}>Можно убрать, если метрики слабые</option>
          <option value="false" ${!s.can_remove?'selected':''}>Не трогать без ручного решения</option>
        </select>
      </div>
    </div>
    <label>Заметка</label>
    <textarea id="node-note" maxlength="512" rows="4" placeholder="Например: дешёвая, плохой провайдер, оставить как резерв...">${esc(s.note||'')}</textarea>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:1rem">
      <div class="detail-item"><div class="detail-label">Marzban ID</div><div class="detail-value">${node?node.id:'—'}</div></div>
      <div class="detail-item"><div class="detail-label">Адрес</div><div class="detail-value">${node?esc(node.address):esc(s.node_address||'—')}</div></div>
      <div class="detail-item"><div class="detail-label">Статус</div><div class="detail-value">${node?esc(node.status):'—'}</div></div>
    </div>
    <div class="modal-footer">
      <button onclick="closeModal('node-modal')">Отмена</button>
      <button class="primary" onclick="saveNodeSettings(${id===null?'null':id})">Сохранить</button>
    </div>
  `;
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
    node_name:node?node.name:(getNodeSetting(id).node_name||''),
    node_address:node?node.address:(getNodeSetting(id).node_address||''),
    provider:document.getElementById('node-provider').value.trim(),
    location:document.getElementById('node-location').value.trim(),
    monthly_cost:monthlyCost,
    currency:(document.getElementById('node-currency').value.trim()||'USD').toUpperCase(),
    traffic_included_gb:trafficIncluded,
    traffic_price_per_tb:trafficPrice,
    importance:document.getElementById('node-importance').value,
    can_remove:document.getElementById('node-can-remove').value==='true',
    note:document.getElementById('node-note').value.trim(),
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
  body.innerHTML='<div class="loading"><span class="spinner"></span>Собираю трафик по клиентам...</div>';
  document.getElementById('node-modal').classList.add('open');

  const results=await loadUsersUsageForNode(id,period);
  const sorted=results.filter(r=>r.traffic>0).sort((a,b)=>b.traffic-a.traffic);
  if(!sorted.length){body.innerHTML='<p style="color:var(--text3);padding:1rem 0">Нет трафика через эту ноду за выбранный период</p>';return}
  body.innerHTML=`<div class="table-wrap"><table>
    <thead><tr><th>Пользователь</th><th style="text-align:right">Трафик</th></tr></thead>
    <tbody>${sorted.map(r=>`<tr class="clickable" onclick="closeModal('node-modal');openUser('${r.username}')"><td>${esc(r.username)}</td><td style="text-align:right">${fmt(r.traffic)}</td></tr>`).join('')}</tbody>
  </table></div>`;
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
  if(!configs.length){list.innerHTML='<p style="color:var(--text3);font-size:13px;padding:1rem 0">Нет конфигов</p>';return}
  list.innerHTML=configs.map((c,i)=>`
    <div class="config-row" draggable="true" id="cfg-${i}"
      ondragstart="dragStart(${i})" ondragover="dragOver(event,${i})" ondrop="drop(${i})" ondragend="dragEnd()">
      <span class="drag-handle">⠿</span>
      <div class="config-info">
        <div class="config-name-text">${esc(c.name)}</div>
        <div class="config-uri-text">${esc(c.uri)}</div>
      </div>
      <span class="badge ${c.enabled?'badge-green':'badge-red'}" style="cursor:pointer" onclick="toggleConfig(${i})">${c.enabled?'вкл':'выкл'}</span>
      <button class="danger" style="padding:4px 10px;font-size:12px" onclick="deleteConfig(${i})">×</button>
    </div>
  `).join('');
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
function dragOver(e,i){e.preventDefault()}
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
  if(!configs.length){el.innerHTML='<p style="font-size:13px;color:var(--text3);padding:0.5rem 0">Нет индивидуальных конфигов</p>';return}
  el.innerHTML=configs.map((c,i)=>`
    <div class="per-user-config">
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;font-weight:500">${esc(c.name)}</div>
        <div style="font-size:11px;color:var(--text3);font-family:monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(c.uri)}</div>
      </div>
      <button class="danger" style="padding:4px 10px;font-size:12px" onclick="deletePerUserConfig('${username}',${i})">×</button>
    </div>
  `).join('');
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

function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;')}
function closeModal(id){document.getElementById(id).classList.remove('open')}
document.querySelectorAll('.modal-overlay').forEach(m=>m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('open')}));

let allInbounds={};
async function bootstrap(){
  loadDashboard();
  loadUsers();
  try{
    const [nR,fR,iR]=await Promise.all([api('/nodes'),proxyApi('/admin/node-filters'),api('/inbounds')]);
    allNodes=await nR.json();
    nodeFilters=fR.ok?await fR.json():{};
    allInbounds=await iR.json();
  }catch(e){console.warn('bootstrap',e)}
}

// SETTINGS
async function loadSettings(){
  const status=document.getElementById('settings-status');
  status.textContent='Загрузка...';
  try{
    const r=await proxyApi('/admin/settings');
    const data=await r.json();
    document.getElementById('set-sub-interval').value=data.sub_update_interval!=null?data.sub_update_interval:'';
    document.getElementById('set-block-contact').value=data.block_contact||'';
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
  status.textContent='Сохранение...';
  try{
    await proxyApi('/admin/settings',{method:'POST',body:JSON.stringify({sub_update_interval:val,block_contact:contact||null})});
    status.style.color='#6f6';
    status.textContent='Сохранено';
    setTimeout(()=>{status.textContent='';status.style.color='';},2000);
  }catch(e){
    status.style.color='';
    status.textContent='Ошибка сохранения';
  }
}

// INIT
if(TOKEN){
  document.getElementById('login-page').style.display='none';
  document.getElementById('app').style.display='flex';
  bootstrap();
}
