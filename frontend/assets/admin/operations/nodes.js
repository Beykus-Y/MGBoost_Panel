// PH7-16 Wave 2 -- legacy Nodes screen, ported out of admin.js verbatim
// (owner-corrected placement: Operations, not System/Technical -- this is
// fleet-infra monitoring, not account-domain-adjacent debt).
//
// Nodes shares real state with two screens NOT touched by this move:
// Dashboard (node-traffic widget, `open-node-traffic` clicks reachable
// before this screen was ever visited) and the legacy Users node-filter
// feature (needs the same node list to resolve host->name). Rather than
// duplicate that shared state into a second, independently-cached copy
// here (the exact class of drift risk PH7-16 Wave 1 closed for getJson),
// admin.js keeps owning `allNodes`/`allUsers` and passes accessor
// functions in -- one shared cache, explicit access, no hidden global.
export function createNodesUi({html,renderHtml,toast,closeModal,api,proxyApi,fmt,fmtMoney,getTrafficPeriod,getAllNodes,setAllNodes,getAllUsers,setAllUsers,nodeImportanceBadgeClass}){
  let nodeSettings={};

  function nodeKey(id){return id===null||id===undefined||id===''?'null':String(id)}
  function sameNodeId(a,b){return (a===null&&b===null)||String(a)===String(b);}
  function getNodeSetting(id){return nodeSettings[nodeKey(id)]||{currency:'USD',importance:'normal',can_remove:true}}
  function importanceLabel(v){return{normal:'обычная',core:'важная',backup:'backup',test:'test',deprecated:'к выводу'}[v]||'обычная'}
  const importanceClass=nodeImportanceBadgeClass;
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
  function option(value,current,label){
    return html`<option value="${value}" ${value===current?html`selected`:''}>${label}</option>`;
  }
  function emptyToNumber(id){
    const raw=document.getElementById(id).value.trim().replace(',','.');
    if(raw==='')return null;
    const n=Number(raw);
    return Number.isFinite(n)&&n>=0?n:null;
  }

  async function loadNodes(){
    const period=getTrafficPeriod();
    const [nodesR,usageR,settingsR]=await Promise.all([api('/nodes'),api('/nodes/usage'+period.query),proxyApi('/admin/node-settings')]);
    const nodes=await nodesR.json();
    setAllNodes(nodes);
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
        <div class="node-card-header">
          <span class="dot ${n.status==='connected'?'dot-green':'dot-red'}"></span>
          <div class="node-name">${n.name}</div>
          <button data-action="reconnect-node" data-node-id="${n.id}" class="node-icon-btn">⟳</button>
        </div>
        <div class="node-addr">${n.address}:${n.port}</div>
        <div class="node-badges">
          <span class="badge ${importanceClass(s.importance)}">${importanceLabel(s.importance)}</span>
          <span class="badge ${s.can_remove?'badge-green':'badge-red'}">${s.can_remove?'можно убрать':'не трогать'}</span>
        </div>
        <div class="node-stats">
          <span>↑${fmt(u.uplink)}</span>
          <span>↓${fmt(u.downlink)}</span>
          <span class="muted">${n.xray_version||'?'}</span>
        </div>
        <div class="node-billing-block">
          <div class="node-billing-row"><span>VPS / мес</span><b>${fmtMoney(s.monthly_cost,s.currency)}</b></div>
          <div class="node-billing-row"><span>Трафик</span><span>${s.traffic_price_per_tb?fmtMoney(s.traffic_price_per_tb,s.currency)+'/TB':'—'}</span></div>
          ${(s.provider||s.location)?html`<div class="node-billing-note">${[s.provider,s.location].filter(Boolean).join(' · ')}</div>`:''}
          ${s.billing_group?html`<div class="node-billing-note">группа: ${s.billing_group}</div>`:''}
          ${total&&s.traffic_price_per_tb?html`<div class="node-billing-note">${groupTrafficCostLabel(n.id,total,billingGroups)}</div>`:''}
        </div>
        <button class="node-card-footer-btn" data-action="open-node-settings" data-node-id="${n.id}">Настроить</button>
      </div>`;
    })}`);

    const tbody=document.getElementById('node-traffic-tbody');
    renderHtml(tbody,html`${(usage.usages||[]).map(u=>{
      const total=u.uplink+u.downlink;
      const s=getNodeSetting(u.node_id);
      return html`<tr class="clickable" data-action="open-node-traffic" data-node-id="${u.node_id===null?'null':u.node_id}">
        <td>
          <div class="node-traffic-name">${u.node_name}</div>
          <div class="node-traffic-sub">${[s.provider,s.location].filter(Boolean).join(' · ')||importanceLabel(s.importance)}</div>
        </td>
        <td>${fmt(u.uplink)}</td>
        <td>${fmt(u.downlink)}</td>
        <td class="node-traffic-name">${fmt(total)}</td>
        <td>${fmtMoney(s.monthly_cost,s.currency)}</td>
        <td>${groupTrafficCostLabel(u.node_id,total,billingGroups)}</td>
        <td><button class="small" data-action="open-node-settings" data-node-id="${u.node_id===null?'null':u.node_id}">Настроить</button></td>
      </tr>`;
    })}`);
  }

  async function reconnectNode(id){
    const r=await api('/node/'+id+'/reconnect',{method:'POST'});
    if(r.ok){toast('Reconnect послан');setTimeout(loadNodes,1000);}
    else toast('Ошибка','err');
  }

  function renderQuietHours(list){
    if(!list||!list.length)return html`<div class="cell-sub">Не заданы</div>`;
    return html`${list.map((w,i)=>html`
      <div class="quiet-hour-row">
        <input type="time" value="${w.from}" class="quiet-hour-input" data-qh-from="${i}" />
        <span class="quiet-hour-sep">—</span>
        <input type="time" value="${w.to}" class="quiet-hour-input" data-qh-to="${i}" />
        <button class="node-icon-btn" data-action="remove-quiet-hour" data-quiet-index="${i}">✕</button>
      </div>`)}`;
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

  function openNodeSettings(id){
    const node=getAllNodes().find(n=>sameNodeId(n.id,id));
    const s={currency:'USD',importance:'normal',can_remove:true,...getNodeSetting(id)};
    document.getElementById('node-modal-title').textContent=node?`Настройки ноды · ${node.name}`:'Настройки ноды';
    const body=document.getElementById('node-modal-body');
    renderHtml(body,html`
      <div class="node-modal-intro">
        Эти параметры хранятся только в MGBoost Panel и не меняют Marzban-ноду.
      </div>
      <label>Отображаемое имя (в боте)</label>
      <input type="text" id="node-display-name" maxlength="128" placeholder="${node?node.name:''}" value="${s.node_name||''}" class="node-input-spaced" />
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
      <div class="node-field-hint">
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
      <div class="node-id-grid">
        <div class="detail-item"><div class="detail-label">Marzban ID</div><div class="detail-value">${node?node.id:'—'}</div></div>
        <div class="detail-item"><div class="detail-label">Адрес</div><div class="detail-value">${node?node.address:s.node_address||'—'}</div></div>
        <div class="detail-item"><div class="detail-label">Статус</div><div class="detail-value">${node?node.status:'—'}</div></div>
      </div>
      <div class="node-quiet-hours-section">
        <div class="node-section-label">Тихие часы мониторинга (UTC)</div>
        <div class="node-section-hint">Во время тихих часов алерты в Telegram не отправляются (для прерываемых ВМ).</div>
        <div id="node-quiet-hours-list">${renderQuietHours(s.monitor_quiet_hours||[])}</div>
        <button class="small" data-action="add-quiet-hour">+ Добавить окно</button>
      </div>
      <div class="modal-footer">
        <button data-action="close-modal" data-target="node-modal">Отмена</button>
        <button class="primary" data-action="save-node-settings" data-node-id="${id===null?'null':id}">Сохранить</button>
      </div>
    `);
    document.getElementById('node-modal').classList.add('open');
  }

  async function saveNodeSettings(id){
    const node=getAllNodes().find(n=>sameNodeId(n.id,id));
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

    let users=getAllUsers();
    if(!users.length){const r=await api('/users?limit=500');users=(await r.json()).users||[];setAllUsers(users);}
    return Promise.all(users.map(u=>
      api('/user/'+encodeURIComponent(u.username)+'/usage'+period.query).then(r=>r.json()).then(d=>{
        const rec=(d.usages||[]).find(x=>sameNodeId(x.node_id,id));
        return{username:u.username,traffic:rec?rec.used_traffic:0};
      }).catch(()=>({username:u.username,traffic:0}))
    ));
  }

  async function openNodeTraffic(id){
    const period=getTrafficPeriod();
    const node=getAllNodes().find(n=>sameNodeId(n.id,id));
    const usage=(window._dashNodeUsages||[]).find(u=>sameNodeId(u.node_id,id));
    const title=node?`${node.name} · ${node.address}`:(usage?usage.node_name:'Нода');
    document.getElementById('node-modal-title').textContent=`${title} · ${period.label}`;
    const body=document.getElementById('node-modal-body');
    renderHtml(body,html`<div class="loading"><span class="spinner"></span>Собираю трафик по клиентам...</div>`);
    document.getElementById('node-modal').classList.add('open');

    const results=await loadUsersUsageForNode(id,period);
    const sorted=results.filter(r=>r.traffic>0).sort((a,b)=>b.traffic-a.traffic);
    if(!sorted.length){renderHtml(body,html`<p class="empty-state">Нет трафика через эту ноду за выбранный период</p>`);return}
    renderHtml(body,html`<div class="table-wrap"><table>
      <thead><tr><th>Пользователь</th><th class="text-right">Трафик</th></tr></thead>
      <tbody>${sorted.map(r=>html`<tr class="clickable" data-action="open-user-from-node" data-username="${r.username}"><td>${r.username}</td><td class="text-right">${fmt(r.traffic)}</td></tr>`)}</tbody>
    </table></div>`);
  }

  async function openNode(id){
    return openNodeTraffic(id);
  }

  return {loadNodes,reconnectNode,openNodeSettings,saveNodeSettings,openNodeTraffic,openNode,addQuietHour,removeQuietHour};
}
