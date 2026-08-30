// PH5-13 admin promo UI over the existing PromoStore backend routes
// (/admin/promo/*). A single manager modal: definitions list (code, effect,
// per-user limit, status) with create/disable flows, plus the latest
// redemptions for support visibility (including CANCELLED reservations).
// Same patterns as admin_grant_ops.js: confirmFlow for consequential actions,
// crypto.randomUUID() idempotency keys, server-side validation only.

export function createPromoOps({html,renderHtml,toast,openModal,confirmFlow,
                                formatTimestamp,humanLabel,adminFetch}){

  function effectTitle(definition){
    if(definition.effect_kind==='TRIAL_GRANT')return `Триал · класс ${definition.trial_class}`;
    if(definition.effect_kind==='PURCHASE_DISCOUNT')return 'Скидка на покупку';
    return 'Продление подписки';
  }

  function effectDetails(definition){
    const params=definition.effect_params||{};
    if(params.days)return html`<small>${params.days} дн.</small>`;
    if(params.discount_percent)return html`<small>−${params.discount_percent}%</small>`;
    if(params.discount_minor)return html`<small>−${params.discount_minor} minor</small>`;
    return html``;
  }

  async function openManager(){
    let modal=openModal({title:'Промокоды (PH5-13 · только primary admin)'});
    let definitions=[],redemptions=[];

    async function reloadLists(){
      const [defsResponse,redemptionsResponse]=await Promise.all([
        adminFetch('/admin/promo/definitions'),
        adminFetch('/admin/promo/redemptions'),
      ]);
      const defsData=await defsResponse.json().catch(()=>({}));
      if(!defsResponse.ok)throw new Error(defsData.error||'definitions failed');
      const redemptionsData=await redemptionsResponse.json().catch(()=>({}));
      if(!redemptionsResponse.ok)throw new Error(redemptionsData.error||'redemptions failed');
      definitions=defsData.definitions||[];
      redemptions=redemptionsData.redemptions||[];
      renderManager();
    }

    function renderManager(){
      renderHtml(modal.el.querySelector('.modal-body'),html`
        <div class="ops-actions">
          <button class="primary" data-promo-act="create">+ Создать промокод</button>
          <button data-promo-act="refresh">Обновить</button>
        </div>
        <h4>Определения</h4>
        ${definitions.length?html`<table class="list"><thead><tr>
            <th>Код</th><th>Эффект</th><th>На польз.</th><th>Статус</th><th></th></tr></thead>
          <tbody>${definitions.map(d=>html`<tr>
            <td><strong>${d.code}</strong></td>
            <td>${effectTitle(d)} ${effectDetails(d)}</td>
            <td>${d.per_user_limit}</td>
            <td><span class="badge ${d.status==='ACTIVE'?'ok':'muted'}">${d.status}</span></td>
            <td>${d.status==='ACTIVE'?html`<button data-promo-act="disable" data-code="${d.code}">Отключить</button>`:html``}</td>
          </tr>`)}</tbody></table>`
          :html`<div class="empty-state">Промокодов ещё нет</div>`}
        <h4>Последние применения</h4>
        ${redemptions.length?html`<table class="list"><thead><tr>
            <th>Код</th><th>Аккаунт</th><th>Статус</th><th>Кем</th><th>Когда</th></tr></thead>
          <tbody>${redemptions.map(r=>html`<tr>
            <td><strong>${r.promo_code}</strong>${r.trial_class?html`<div class="cell-sub">${r.trial_class}</div>`:html``}</td>
            <td>#${r.account_id??'—'}</td>
            <td><span class="badge ${r.status==='REDEEMED'?'ok':(r.status==='CANCELLED'?'muted':'warn')}">${r.status}</span></td>
            <td><div class="cell-sub">${r.actor_type}</div></td>
            <td>${formatTimestamp(r.created_at)}</td>
          </tr>`)}</tbody></table>`
          :html`<div class="empty-state">Применений ещё нет</div>`}
        <div id="promo-error"></div>`);
      modal.el.querySelectorAll('[data-promo-act="create"]')
        .forEach(btn=>btn.addEventListener('click',()=>openCreateDefinitionDialog().then(reloadLists).catch(error=>toast(error.message||'Ошибка','err'))));
      modal.el.querySelectorAll('[data-promo-act="refresh"]')
        .forEach(btn=>btn.addEventListener('click',()=>reloadLists().catch(error=>toast(error.message||'Ошибка','err'))));
      modal.el.querySelectorAll('[data-promo-act="disable"]')
        .forEach(btn=>btn.addEventListener('click',()=>disableDefinition(btn.dataset.code).then(reloadLists).catch(error=>toast(error.message||'Ошибка','err'))));
    }

    reloadLists().catch(error=>{
      renderHtml(modal.el.querySelector('.modal-body'),
        html`<div class="notice notice-danger">${error.message||'Не удалось загрузить промокоды'}</div>`);
    });
  }

  async function openCreateDefinitionDialog(){
    return new Promise(resolve=>{
      confirmFlow({
        title:'Создать промокод',
        body:html`<div class="ops-form">
          <label>Код (A-Z, 0-9, _, 3..64)
            <input type="text" id="promo-new-code" maxlength="64" style="text-transform:uppercase"/></label>
          <label>Эффект
            <select id="promo-new-kind">
              <option value="EXTEND_SUBSCRIPTION">Продление подписки (+N дней)</option>
              <option value="TRIAL_GRANT">Триал (WL_TRIAL, N дней)</option>
            </select></label>
          <label>Дней (effect_params.days)
            <input type="number" id="promo-new-days" min="1" max="3650" step="1" value="7"/></label>
          <label>trial_class (только для триала)
            <input type="text" id="promo-new-trial-class" value="WL_TRIAL" maxlength="64"/></label>
          <label>Лимит на пользователя (1 = одноразовый)
            <input type="number" id="promo-new-per-user" min="1" max="1000" step="1" value="1"/></label>
          <label>Причина (обязательно, попадёт в immutable audit)
            <input type="text" id="promo-new-reason" maxlength="1000"/></label>
          <div class="notice notice-amber">Эффект применяется мгновенно при вводе кода пользователем. Отключение кода (disable) не отменяет уже применённые эффекты.</div>
        </div>`,
        confirmLabel:'Создать',
        busyLabel:'Создаю…',
        onConfirm:async m=>{
          const code=m.el.querySelector('#promo-new-code').value.trim().toUpperCase();
          const effectKind=m.el.querySelector('#promo-new-kind').value;
          const days=Number(m.el.querySelector('#promo-new-days').value);
          const trialClass=m.el.querySelector('#promo-new-trial-class').value.trim();
          const perUserLimit=Number(m.el.querySelector('#promo-new-per-user').value);
          const reason=m.el.querySelector('#promo-new-reason').value.trim();
          if(!/^[A-Z0-9_]{3,64}$/.test(code)){toast('Код: A-Z, 0-9, _, 3..64','err');return false;}
          if(!Number.isInteger(days)||days<1||days>3650){toast('Дней: целое 1..3650','err');return false;}
          if(effectKind==='TRIAL_GRANT'&&trialClass.length<1){toast('Укажите trial_class','err');return false;}
          if(reason.length<8){toast('Причина минимум 8 символов','err');return false;}
          const effectParams={days};
          const response=await adminFetch('/admin/promo/definitions',
            {method:'POST',body:JSON.stringify({code,effect_kind:effectKind,
              trial_class:effectKind==='TRIAL_GRANT'?trialClass:null,
              effect_params:effectParams,per_user_limit:perUserLimit,reason,
              idempotency_key:`promo-def-${crypto.randomUUID()}`})});
          const data=await response.json().catch(()=>({}));
          if(!response.ok)throw new Error(data.error||'create failed');
          toast(`Промокод ${data.code} создан`);
          resolve();
        }});
    });
  }

  async function disableDefinition(code){
    return new Promise((resolve,reject)=>{
      confirmFlow({
        title:`Отключить промокод ${code}?`,
        body:html`<div class="ops-form">
          <div class="notice notice-amber">Код перестанет приниматься; уже применённые эффекты не отменяются.</div>
          <label>Причина (обязательно)
            <input type="text" id="promo-disable-reason" maxlength="1000"/></label>
        </div>`,
        confirmLabel:'Отключить',
        busyLabel:'Отключаю…',
        onConfirm:async m=>{
          const reason=m.el.querySelector('#promo-disable-reason').value.trim();
          if(reason.length<8){toast('Причина минимум 8 символов','err');return false;}
          const response=await adminFetch(`/admin/promo/definitions/${code}/disable`,
            {method:'POST',body:JSON.stringify({reason,idempotency_key:`promo-disable-${crypto.randomUUID()}`})});
          const data=await response.json().catch(()=>({}));
          if(!response.ok)throw new Error(data.error||'disable failed');
          toast(`Промокод ${code} отключён`);
          resolve();
        }});
    });
  }

  return {openManager};
}
