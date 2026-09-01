// PH7-16 Wave 4 -- Settings screen (subscription-template/block-message
// misc settings + Telegram monitoring bot + AI support-bot config), ported
// out of admin.js verbatim. Owner-corrected placement: System, not
// Technical -- this is product/operational configuration, not Marzban
// internals. Fully self-contained: no state shared with any other screen.
export function createSettingsUi({html,renderHtml,toast,proxyApi}){
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

  function loadSettingsPage(){
    loadSettings();
    loadBotSettings();
    loadSupportSettings();
  }

  return {loadSettingsPage,saveSettings,toggleBotProxy,saveBotSettings,restartBot,saveSupportSettings};
}
