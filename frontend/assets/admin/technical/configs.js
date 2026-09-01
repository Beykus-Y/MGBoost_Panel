// PH7-16 Wave 4 -- legacy Extra Configs screen, ported out of admin.js
// verbatim (owner-corrected placement: System/Technical, not account-domain
// Operations -- this is raw Marzban subscription-link config plumbing with
// no canonical equivalent, not fleet-infra monitoring like Nodes).
//
// `allInbounds` is shared with the legacy Users screen's "Create user"
// dialog (admin/technical/marzban_users.js, since Wave 5) and with
// router.js's own bootstrap() initial fetch -- threaded in via
// getAllInbounds/setAllInbounds accessors, same single-shared-cache
// discipline as allNodes/allUsers in admin/operations/nodes.js, not a
// second independently-cached copy.
export function createConfigsUi({html,renderHtml,toast,api,proxyApi,getAllInbounds,setAllInbounds}){
  let cfgs=[];
  let perUserConfigs={};
  let inboundClientExtras={};
  let dragIdx=null;

  // --- global configs -------------------------------------------------
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
    cfgs=configs;
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
     const cfg=cfgs[idx];
     if(!cfg||!cfg.id){
         toast('Ошибка: конфиг не найден','err');
         return;
     }
     await proxyApi('/admin/configs/'+cfg.id,{method:'DELETE'});
     toast('Удалён');loadGlobalConfigs();
   }
  async function toggleConfig(idx){
    cfgs[idx].enabled=!cfgs[idx].enabled;
    await proxyApi('/admin/configs/reorder',{method:'POST',body:JSON.stringify(cfgs)});
    loadGlobalConfigs();
  }
  function dragStart(i){dragIdx=i;document.getElementById('cfg-'+i).style.opacity='0.4'}
  function dragOver(e){e.preventDefault()}
  function drop(i){
    if(dragIdx===null||dragIdx===i)return;
    const moved=cfgs.splice(dragIdx,1)[0];
    cfgs.splice(i,0,moved);
    proxyApi('/admin/configs/reorder',{method:'POST',body:JSON.stringify(cfgs)}).then(()=>loadGlobalConfigs());
  }
  function dragEnd(){dragIdx=null;document.querySelectorAll('.config-row').forEach(r=>r.style.opacity='')}

  // --- per-user configs -------------------------------------------------
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

  // --- inbound extras -------------------------------------------------
  async function loadInboundExtras(){
    let inbounds=getAllInbounds();
    if(!inbounds||!Object.keys(inbounds).length){
      try{const r=await api('/inbounds');inbounds=await r.json();setAllInbounds(inbounds);}catch{}
    }
    const dl=document.getElementById('ie-inbounds-list');
    const tags=[];
    Object.values(inbounds||{}).forEach(items=>items.forEach(i=>tags.push(i.tag)));
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

  function loadConfigsPage(){
    loadGlobalConfigs();
    loadPerUserConfigs();
    loadInboundExtras();
  }

  document.addEventListener('dragstart',event=>{
    const row=event.target.closest('.config-row[data-config-index]');
    if(row)dragStart(Number(row.dataset.configIndex));
  });
  document.addEventListener('dragover',event=>{
    if(event.target.closest('.config-row[data-config-index]'))dragOver(event);
  });
  document.addEventListener('drop',event=>{
    const row=event.target.closest('.config-row[data-config-index]');
    if(row){event.preventDefault();drop(Number(row.dataset.configIndex));}
  });
  document.addEventListener('dragend',dragEnd);

  return {loadConfigsPage,addGlobalConfig,toggleConfig,deleteConfig,
    addPerUserConfig,deletePerUserConfig,addInboundExtra,deleteInboundExtra,
    formatInboundExtraJson,updateInboundExtra};
}
