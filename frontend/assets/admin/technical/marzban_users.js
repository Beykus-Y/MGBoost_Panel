// PH7-16 Wave 5 -- legacy Marzban raw-user screen, ported out of admin.js
// verbatim (System/Technical: Marzban-username-centric, no canonical
// account concept -- exactly what DL-050 already called for).
//
// PH7-16 Wave H hardened the underlying PUT/DELETE/POST-reset routes on
// the raw Marzban proxy (src/routes/admin_proxy.py) to require primary-
// admin capability + a mandatory reason (query param) -- this is the
// mandatory companion fix the owner required before this move: without
// it, save/delete/disable/enable/reset-traffic here would 400 for every
// admin, including primary. `promptReason` is injected from router.js so
// the exact same helper backs the equivalent fix in
// admin/payments/stars_legacy.js, not two independently-written copies.
//
// allUsers/allNodes/allInbounds are shared with Dashboard/nodes.js/
// configs.js -- threaded via the same getAllX/setAllX accessors already
// established in Waves 2/4, not a second independently-cached copy.
// nodeFilters is shared only with router.js's own bootstrap() (initial
// fetch); userDeviceCounts is private to this screen.
export function createMarzbanUsersUi({html,renderHtml,toast,closeModal,api,proxyApi,promptReason,
  getAllUsers,setAllUsers,getAllNodes,getAllInbounds,setAllInbounds,getNodeFilters,setNodeFilters,marzbanStatusBadgeClass}){
  let userDeviceCounts={};

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
    if(days<0)return html`<span class="expiry-overdue">просрочен</span>`;
    if(days===0)return html`<span class="expiry-soon">сегодня</span>`;
    if(days<=3)return html`<span class="expiry-soon">${days}д</span>`;
    return`${days}д`;
  }
  function fmtOnline(dt){
    if(!dt)return'—';
    const d=parseUTC(dt);
    const diff=(Date.now()-d)/1000;
    if(diff<120)return html`<span class="online-now">сейчас</span>`;
    if(diff<3600)return`${Math.floor(diff/60)}м назад`;
    if(diff<86400)return`${Math.floor(diff/3600)}ч назад`;
    return`${Math.floor(diff/86400)}д назад`;
  }
  function statusBadge(s){
    const l={active:'активен',disabled:'выкл',expired:'истёк',limited:'лимит',on_hold:'на паузе'};
    return html`<span class="badge ${marzbanStatusBadgeClass(s)}">${l[s]||s}</span>`;
  }

  async function loadUsers(){
    const r=await api('/users?limit=500');
    const d=await r.json();
    const users=d.users||[];
    setAllUsers(users);
    userDeviceCounts={};
    try{
      const countsR=await proxyApi('/admin/user-devices-counts',{method:'POST',body:JSON.stringify({usernames:users.map(u=>u.username)})});
      if(countsR.ok)userDeviceCounts=await countsR.json();
    }catch(e){console.warn('device counts',e)}
    document.getElementById('users-count').textContent='('+users.length+')';
    renderUsers(users);
    // populate per-user select
    const sel=document.getElementById('pu-user');
    renderHtml(sel,html`${users.map(u=>html`<option value="${u.username}">${u.username}</option>`)}`);
  }
  function filterUsers(){
    const q=document.getElementById('user-search').value.toLowerCase();
    renderUsers(getAllUsers().filter(u=>u.username.toLowerCase().includes(q)||(u.note||'').toLowerCase().includes(q)));
  }
  function renderUsers(users){
    const nodeFilters=getNodeFilters();
    renderHtml(document.getElementById('users-tbody'),html`${users.map(u=>{
      const f=nodeFilters[u.username];
      const hasFilter=f&&f.all===false&&(f.allowed_configs||[]).length>0;
      return html`
      <tr class="clickable" data-action="open-user" data-username="${u.username}">
        <td>
          <div class="cell-strong">${u.username}${hasFilter?html` <span class="filter-tag">фильтр</span>`:''}</div>
          ${u.note?html`<div class="user-note">${u.note}</div>`:''}
        </td>
        <td>${statusBadge(u.status)}</td>
        <td>${fmt(u.used_traffic)}${u.data_limit?` / ${fmt(u.data_limit)}`:'  / ∞'}</td>
        <td>${fmtRelDate(u.expire)}</td>
        <td class="ua-cell">${(u.sub_last_user_agent||'—').split('/')[0]}</td>
        <td>${userDeviceCounts[u.username]??0}</td>
        <td>${fmtOnline(u.online_at)}</td>
        <td><button data-action="open-user" data-username="${u.username}">···</button></td>
      </tr>
    `;
    })}`);
  }
  // Local byte formatter matches the original admin.js `fmt()` exactly
  // (binary units, English suffixes) -- kept separate from
  // admin/core.js's decimal formatGb() on purpose, same as before Wave 4:
  // this is Marzban-native traffic display, not account-domain WL bytes.
  function fmt(bytes){
    if(bytes===null||bytes===undefined)return'—';
    if(bytes<1024)return bytes+'B';
    if(bytes<1048576)return(bytes/1024).toFixed(1)+'KB';
    if(bytes<1073741824)return(bytes/1048576).toFixed(1)+'MB';
    return(bytes/1073741824).toFixed(2)+'GB';
  }

  // --- user detail modal -------------------------------------------------
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
      ${u.note?html`<div class="user-note-block"><span class="muted2">Заметка: </span>${u.note}</div>`:''}
      <div class="section-label">Трафик по нодам</div>
      <div class="usage-node-list">
        ${usageByNode.map(n=>{
          const pct=totalUsage>0?Math.round(n.used_traffic/totalUsage*100):0;
          return html`<div class="usage-node-item">
            <div class="node-usage-row">
              <span class="usage-node-name">${n.node_name}</span>
              <span class="usage-node-val">${fmt(n.used_traffic)} <span class="node-usage-pct">${pct}%</span></span>
            </div>
            <div class="traffic-bar"><div class="traffic-fill" style="width:${pct}%"></div></div>
          </div>`;
        })}
      </div>
      <div class="section-block">
        <div class="devices-header">
          <div class="section-label section-label--tight">Устройства (HWID)</div>
          <button class="small-btn" data-action="change-device-limit" data-username="${username}" data-limit="${devData?devData.limit:3}">Изменить лимит</button>
        </div>
        ${devData?html`
          <div class="devices-summary">
            Активных: <b>${devData.active_count}</b> / <b>${devData.limit===0?'∞':devData.limit}</b>
          </div>
          ${devData.devices.length?devData.devices.map(d=>html`
            <div class="device-row">
              <div>
                <span class="${d.is_active?'device-dot--active':'device-dot--inactive'}">${d.is_active?'●':'○'}</span>
                <span class="device-name">${d.display_name||d.device_name||d.client_name||'Устройство'}</span>
                <span class="device-meta">${d.platform||''}${d.client_name?` · ${d.client_name}`:''}</span>
              </div>
              <div class="device-actions-row">
                <span class="muted">${fmtOnline(new Date(d.last_seen*1000).toISOString())}</span>
                <button class="device-remove-btn" data-action="admin-remove-device" data-device-id="${d.id}" data-username="${username}">✕</button>
              </div>
            </div>`):html`<div class="nf-empty">Нет зарегистрированных устройств</div>`}
        `:html`<div class="nf-empty">Нет данных</div>`}
      </div>
      <div class="section-block" id="nf-section">
        <div class="section-label">Фильтр конфигов</div>
        <div class="loading"><span class="spinner"></span></div>
      </div>
      <div class="section-block">
        <div class="section-label">Изменить</div>
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
    getAllNodes().forEach(n=>nodeByAddr[n.address]=n.name);

    const nodeFilters=getNodeFilters();
    const f=nodeFilters[username]||{all:true};
    // legacy formats → treat as all
    const isAll=f.all!==false||('hosts' in f)||('allowed_ips' in f);
    const allowedSet=new Set(f.allowed_configs||[]);

    if(!hostOrder.length){
      renderHtml(sec,html`
        <div class="section-label">Фильтр конфигов</div>
        <p class="nf-empty">Нет ссылок в подписке</p>`);
      return;
    }

    const groupRows=hostOrder.map((ip,groupIndex)=>{
      const nodeName=nodeByAddr[ip]||null;
      const configs=hostConfigs[ip];
      const groupId=`nfg-${groupIndex}`;

      const configChecks=configs.map(cfg=>{
        const checked=isAll||allowedSet.has(cfg);
        return html`<label class="nf-cfg-row">
          <input type="checkbox" class="nf-cfg checkbox-auto" data-cfg="${cfg}" ${checked?html`checked`:''} data-change-action="nf-cfg-toggle" />
          <span class="nf-cfg-label">${cfg}</span>
        </label>`;
      });

      const allGroupChecked=isAll||configs.every(c=>allowedSet.has(c));

      return html`<div class="nf-group">
        <div class="nf-group-header" data-action="toggle-nf-group" data-target="${groupId}">
          <input type="checkbox" class="nf-group-all checkbox-auto" data-group-id="${groupId}" ${allGroupChecked?html`checked`:''} data-change-action="nf-group-all-toggle" />
          <span class="nf-group-title">
            ${nodeName?html`<span class="nf-group-node-name">${nodeName}</span> `:''}
            <span class="nf-group-host">${ip}</span>
          </span>
          <span class="nf-group-count">${configs.length} конф.</span>
        </div>
        <div id="${groupId}" class="nf-group-body">${configChecks}</div>
      </div>`;
    });

    renderHtml(sec,html`
      <div class="section-label">Фильтр конфигов</div>
      <label class="nf-all-row">
        <input type="checkbox" id="nf-all" ${isAll?html`checked`:''} data-change-action="nf-all-toggle" class="checkbox-auto" />
        <span class="nf-all-label">Все конфиги (без фильтра)</span>
      </label>
      <div id="nf-list" class="${isAll?'nf-list--disabled':''}">${groupRows}</div>`);
  }

  function toggleNfGroup(id){
    const el=document.getElementById(id);
    if(el)el.classList.toggle('nf-group-body--collapsed');
  }

  function onNfAllToggle(){
    const all=document.getElementById('nf-all').checked;
    document.getElementById('nf-list').classList.toggle('nf-list--disabled',all);
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

  // --- mutations (PH7-16 Wave H: primary-admin capability + mandatory
  // reason, query-param, on the underlying admin_proxy.py routes) --------
  async function saveUser(username){
    const reason=promptReason('Причина изменения пользователя (3–300 символов, попадёт в audit)');
    if(reason===null)return;
    const expire=document.getElementById('edit-expire').value;
    const limitGB=parseFloat(document.getElementById('edit-limit').value)||0;
    const note=document.getElementById('edit-note').value;
    const body={note,data_limit:limitGB?Math.round(limitGB*1073741824):null};
    if(expire)body.expire=Math.floor(new Date(expire).getTime()/1000);
    else body.expire=null;

    // node filter
    const nfAllEl=document.getElementById('nf-all');
    if(nfAllEl){
      const nodeFilters=getNodeFilters();
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
      setNodeFilters(nodeFilters);
      await proxyApi('/admin/node-filters',{method:'POST',body:JSON.stringify(nodeFilters)});
    }

    const r=await api('/user/'+encodeURIComponent(username)+'?reason='+encodeURIComponent(reason),{method:'PUT',body:JSON.stringify(body)});
    if(r.ok){toast('Сохранено');closeModal('user-modal');loadUsers();}
    else toast('Ошибка','err');
  }
  async function deleteUser(username){
    if(!confirm('Удалить '+username+'?'))return;
    const reason=promptReason('Причина удаления пользователя (3–300 символов)');
    if(reason===null)return;
    const r=await api('/user/'+encodeURIComponent(username)+'?reason='+encodeURIComponent(reason),{method:'DELETE'});
    if(r.ok){toast('Удалён');closeModal('user-modal');loadUsers();}
    else toast('Ошибка','err');
  }
  async function disableUser(username){
    const reason=promptReason('Причина отключения (3–300 символов)');
    if(reason===null)return;
    const r=await api('/user/'+encodeURIComponent(username)+'?reason='+encodeURIComponent(reason),{method:'PUT',body:JSON.stringify({status:'disabled'})});
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
    const reason=promptReason('Причина включения (3–300 символов)');
    if(reason===null)return;
    const r=await api('/user/'+encodeURIComponent(username)+'?reason='+encodeURIComponent(reason),{method:'PUT',body:JSON.stringify({status:'active'})});
    if(r.ok){toast('Включён');openUser(username);loadUsers();}
  }
  async function resetTraffic(username){
    if(!confirm('Сбросить трафик '+username+'?'))return;
    const reason=promptReason('Причина сброса трафика (3–300 символов)');
    if(reason===null)return;
    const r=await api('/user/'+encodeURIComponent(username)+'/reset?reason='+encodeURIComponent(reason),{method:'POST'});
    if(r.ok){toast('Трафик сброшен');openUser(username);loadUsers();}
  }

  // --- create user --------------------------------------------------
  async function openCreateUser(){
    document.getElementById('create-modal').classList.add('open');
    const el=document.getElementById('new-inbounds');
    let inbounds=getAllInbounds();
    if(!inbounds||!Object.keys(inbounds).length){
      try{const r=await api('/inbounds');inbounds=await r.json();setAllInbounds(inbounds);}catch{}
    }
    renderHtml(el,Object.keys(inbounds||{}).length?html`${Object.entries(inbounds).map(([proto,items])=>html`
      <div class="create-inbound-group">
        <div class="create-inbound-proto">${proto}</div>
        <div class="create-inbound-items">
          ${items.map(it=>html`<label class="create-inbound-item">
            <input type="checkbox" class="nu-ib checkbox-auto" data-proto="${proto}" data-tag="${it.tag}" checked />
            <span>${it.tag}</span>
          </label>`)}
        </div>
      </div>
    `)}`:html`<p class="nf-empty">Нет inbounds</p>`);
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

  return {loadUsers,filterUsers,renderUsers,openUser,toggleNfGroup,onNfAllToggle,onNfGroupAllToggle,onNfCfgToggle,
    saveUser,deleteUser,disableUser,changeDeviceLimit,adminRemoveDevice,enableUser,resetTraffic,
    openCreateUser,createUser};
}
