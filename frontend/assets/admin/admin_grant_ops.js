// PH7-14 admin ADMIN_GRANT UI over the existing AdminGrantStore backend
// (production since HEAD c1ae3d4/no-payment WL canary session). Two flows:
// create a canonical DIRECT account for a Telegram id (no grant, no money),
// and grant an exact plan/duration to an existing account for free. The
// product list (plan_code + duration_days) is the SAME server RUB catalog
// `payments.js` already uses -- price is simply never sent/charged here,
// never a separately-typed "arbitrary duration": duration_days is validated
// server-side against the same catalog as every other purchase path.

export function createAdminGrantOps({html,renderHtml,toast,openModal,confirmFlow,
                                     formatTimestamp,humanLabel,adminFetch}){
  let catalogPromise=null;

  function ensureCatalog(){
    if(!catalogPromise){
      catalogPromise=adminFetch('/admin/manual-payment-catalog')
        .then(response=>response.json().then(data=>{if(!response.ok)throw new Error(data.error||'catalog failed');return data;}))
        .catch(error=>{catalogPromise=null;throw error;});
    }
    return catalogPromise;
  }

  function productTitle(plan){
    return `${humanLabel(plan.plan_code)} · ${plan.duration_days} дн.`;
  }

  async function openCreateAccountDialog(ctx){
    const idempotencyKey=`adm-grant-create-${crypto.randomUUID()}`;
    confirmFlow({
      title:'Создать аккаунт',
      body:html`<div class="ops-form">
        <label>Telegram ID (обязательно)
          <input type="number" id="ops-create-tg" min="1" step="1"/></label>
        <label>Причина (обязательно, попадёт в immutable audit)
          <input type="text" id="ops-create-reason" maxlength="1000"/></label>
        <div class="notice notice-amber">Создаётся только аккаунт и привязка Telegram — план/оплата не выдаются здесь. Дальше можно выдать план бесплатно (ADMIN_GRANT) или оформить ручную оплату (MANUAL_RUB) на карточке аккаунта.</div>
      </div>`,
      confirmLabel:'Создать',
      busyLabel:'Создаю…',
      onConfirm:async m=>{
        const telegramId=Number(m.el.querySelector('#ops-create-tg').value);
        const reason=m.el.querySelector('#ops-create-reason').value.trim();
        if(!Number.isInteger(telegramId)||telegramId<1){toast('Укажите корректный Telegram ID','err');return false;}
        if(reason.length<8){toast('Причина минимум 8 символов','err');return false;}
        const response=await adminFetch('/admin/accounts',
          {method:'POST',body:JSON.stringify({telegram_id:telegramId,reason,idempotency_key:idempotencyKey})});
        const data=await response.json().catch(()=>({}));
        if(!response.ok)throw new Error(data.error||'create failed');
        const footer=m.el.querySelector('.modal-footer');footer.innerHTML='';
        const ok=document.createElement('button');ok.className='primary';
        ok.textContent=data.reused?'Открыть существующий аккаунт':'Открыть новый аккаунт';
        ok.addEventListener('click',()=>{m.close();ctx.openAccount(data.account_id);});
        footer.appendChild(ok);
        m.setBody(html`<div class="notice ${data.reused?'notice-amber':'notice-success'}">
          ${data.reused?`У этого Telegram ID уже был аккаунт #${data.account_id} — новый не создан.`
                       :`Аккаунт #${data.account_id} создан.`}</div>`);
        ctx.reload&&ctx.reload();
      }});
  }

  async function openGrantDialog(ctx){
    let catalog;
    try{catalog=await ensureCatalog();}
    catch(error){toast('Не удалось загрузить каталог тарифов','err');return;}
    const plans=catalog.plans;
    let modal=openModal({title:`Выдать тариф бесплатно (ADMIN_GRANT) · аккаунт #${ctx.accountId}`});
    let selected=null;
    const idempotencyKey=`adm-grant-apply-${crypto.randomUUID()}`;

    function renderPicker(){
      renderHtml(modal.el.querySelector('.modal-body'),html`
        <div class="notice notice-amber">Без движения денег: план и срок из того же серверного каталога, что и у ручных платежей, но сумма не проверяется и не взимается.</div>
        <div class="product-grid">${plans.map((plan,index)=>html`
          <button class="product-card ${selected===index?'active':''}" data-ops-grant-product="${index}">
            <strong>${productTitle(plan)}</strong>
            ${plan.wl_quota_gb?html`<small>${humanLabel(plan.wl_mode)} · ${plan.wl_quota_gb} ГБ/${plan.period_days||30}дн.</small>`:html`<small>${humanLabel(plan.wl_mode)}</small>`}
          </button>`)}
        </div>
        <label>Причина (обязательно, попадёт в immutable audit)
          <input type="text" id="ops-grant-reason" maxlength="1000"/></label>
        <div id="ops-grant-error"></div>`);
      modal.el.querySelectorAll('[data-ops-grant-product]').forEach(btn=>{
        btn.addEventListener('click',()=>{selected=Number(btn.dataset.opsGrantProduct);renderPicker();});
      });
    }
    renderPicker();

    async function submit(){
      if(selected==null){toast('Выберите тариф','err');return;}
      const plan=plans[selected];
      const reason=modal.el.querySelector('#ops-grant-reason').value.trim();
      if(reason.length<8){toast('Причина минимум 8 символов','err');return;}
      const response=await ctx.adminFetch(`/admin/accounts/${ctx.accountId}/admin-grant`,
        {method:'POST',body:JSON.stringify({plan_code:plan.plan_code,duration_days:plan.duration_days,
          reason,idempotency_key:idempotencyKey})});
      const data=await response.json().catch(()=>({}));
      if(!response.ok){renderHtml(modal.el.querySelector('#ops-grant-error'),
        html`<div class="notice notice-danger">${data.error||'Выдача не выполнена'}</div>`);return;}
      renderHtml(modal.el.querySelector('.modal-body'),html`<div class="notice notice-success">
        ${data.already_applied?'Эта выдача уже применялась ранее (повтор по idempotency key).':
          `Тариф ${humanLabel(plan.plan_code)} выдан до ${formatTimestamp(data.new_expiry)}.`}</div>`);
      const footerEl=modal.el.querySelector('.modal-footer');
      footerEl.innerHTML='';
      const ok=document.createElement('button');ok.className='primary';ok.textContent='Готово';
      ok.addEventListener('click',()=>{modal.close();ctx.reload&&ctx.reload();});
      footerEl.appendChild(ok);
      ctx.reload&&ctx.reload();
    }
    const footer=modal.el.querySelector('.modal-footer');
    if(footer){
      const confirmBtn=document.createElement('button');confirmBtn.className='primary';
      confirmBtn.textContent='Выдать тариф';
      confirmBtn.addEventListener('click',()=>submit().catch(error=>toast(error.message||'Ошибка','err')));
      footer.appendChild(confirmBtn);
    }
  }

  function adminGrantCard(detail){
    return html`<div class="card spaced-card"><div class="card-title">Бесплатная выдача тарифа (ADMIN_GRANT · только primary admin)</div>
      <span class="cell-sub">Не revenue: деньги не движутся, сумма не проверяется. Для реальной ручной продажи используйте MANUAL_RUB (карточка «Платежи»).</span>
      <div class="ops-actions">
        <button class="primary" data-action="open-admin-grant" data-account-id="${detail.account.id}">Выдать тариф…</button>
      </div>
    </div>`;
  }

  return {openCreateAccountDialog,openGrantDialog,adminGrantCard};
}
