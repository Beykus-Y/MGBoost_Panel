// PH7-01 admin expiry operations UI over the durable SubscriptionAdminOpsStore
// backend. The frontend never computes an expiry: every action first asks the
// server preview endpoint (which projects the exact formulas apply enforces),
// shows those numbers in the consequence dialog, requires a mandatory reason
// plus explicit confirmation, and mints ONE durable idempotency key per opened
// dialog so a double submit converges instead of duplicating an adjustment.

const PRESET_DAYS=[7,30,60];
const KIND_TITLES={
  EXTEND_DAYS:'Продлить срок',
  REDUCE_DAYS:'Сократить срок',
  SET_EXACT:'Установить точную дату',
  END_NOW:'Завершить доступ сейчас',
};

function shortValue(kind){
  return kind==='END_NOW'?'«завершить сейчас»':kind==='EXTEND_DAYS'?'продление'
    :kind==='REDUCE_DAYS'?'сокращение':'точная дата';
}

export function createExpiryOps({html,renderHtml,toast,confirmFlow,formatTimestamp,
                                 badgeClass,humanLabel}){
  const badge=value=>html`<span class="badge ${badgeClass(value)}">${humanLabel(value)}</span>`;

  async function fetchPreview(ctx,payload){
    const response=await ctx.adminFetch(`/admin/accounts/${ctx.accountId}/expiry/preview`,
      {method:'POST',body:JSON.stringify(payload)});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'preview failed');
    return data;
  }

  function previewMarkup(p){
    return html`<div class="card ops-preview"><h4>Предпросмотр (сервер)</h4>
      <dl class="ops-dl">
        <dt>Текущий срок</dt><dd>${p.current_expiry?formatTimestamp(p.current_expiry):'—'}${p.currently_expired?' · уже истёк':''}</dd>
        <dt>Новый срок</dt><dd><strong>${formatTimestamp(p.new_expiry)}</strong>${p.becomes_expired_now&&p.adjustment_kind!=='EXTEND_DAYS'?' · дети будут отключены синхронизацией':''}</dd>
        ${p.adjustment_kind==='EXTEND_DAYS'&&p.currently_expired?html`<dt>Якорь продления</dt><dd>истёкшая подписка возобновляется от текущего момента (DL-044)</dd>`:''}
        <dt>WL-периоды</dt><dd>операцией не затрагиваются</dd>
      </dl>
      <div class="notice notice-amber">Изменение попадёт в immutable audit (actor/reason/before-after); всем детям уйдёт ревизия состояния.</div>
    </div>`;
  }

  async function openExpiryDialog(ctx,{kind,value}){
    let preview;
    try{preview=await fetchPreview(ctx,{adjustment_kind:kind,value});}
    catch(error){toast(error.message,'err');return;}
    // One durable key per dialog run: a retry/double-click converges on the
    // SAME adjustment; a freshly-opened dialog starts a genuinely new one.
    const idempotencyKey=`adm-expiry-${crypto.randomUUID()}`;
    confirmFlow({
      title:`${KIND_TITLES[kind]} (${shortValue(kind)}) · аккаунт #${ctx.accountId}`,
      body:html`${previewMarkup(preview)}
        <div class="ops-form"><label>Причина (обязательно, попадёт в immutable audit)
          <input type="text" id="ops-reason" maxlength="300"/></label></div>`,
      confirmLabel:'Подтвердить изменение срока',
      busyLabel:'Применяю…',
      onConfirm:async m=>{
        const reason=m.el.querySelector('#ops-reason').value.trim();
        if(reason.length<3){toast('Причина минимум 3 символа','err');return false;}
        const response=await ctx.adminFetch(`/admin/accounts/${ctx.accountId}/expiry/adjust`,
          {method:'POST',body:JSON.stringify({adjustment_kind:kind,value,
            reason,idempotency_key:idempotencyKey})});
        const data=await response.json().catch(()=>({}));
        if(!response.ok)throw new Error(data.error||'adjustment failed');
        const pending=data.aggregate_state&&data.aggregate_state!=='IN_SYNC'
          &&data.aggregate_state!=='REPLAYED';
        const footer=m.el.querySelector('.modal-footer');footer.innerHTML='';
        const ok=document.createElement('button');ok.className='primary';ok.textContent='Готово';
        ok.addEventListener('click',()=>{m.close();ctx.reload&&ctx.reload();});
        footer.appendChild(ok);
        m.setBody(html`${previewMarkup(preview)}
          ${data.already_applied?html`<div class="notice notice-amber">Эта операция уже применялась ранее (повтор по idempotency key) — новое изменение не выполнено.</div>`:''}
          <dl class="ops-dl">
            <dt>Старый срок</dt><dd>${data.previous_expiry?formatTimestamp(data.previous_expiry):'—'}</dd>
            <dt>Новый срок</dt><dd>${data.new_expiry?formatTimestamp(data.new_expiry):'—'}</dd>
            <dt>Entitlement</dt><dd>${data.entitlement_summary?`${humanLabel(data.entitlement_summary.effective_status)} · до ${formatTimestamp(data.entitlement_summary.effective_expiry)}`:'—'}</dd>
            <dt>Child-sync</dt><dd>${badge(data.aggregate_state||'?')}</dd>
          </dl>
          ${pending?html`<div class="notice notice-amber">Дети ещё не сошлись (PENDING): состояние сохранено durably и сойдётся при повторе «Sync» на устройстве.</div>`:''}`);
        ctx.reload&&ctx.reload();
      }});
  }

  function expiryCard(detail){
    const sub=detail.subscription;
    const blocked=!sub||sub.status==='UNLIMITED';
    const reason=sub&&sub.status==='UNLIMITED'
      ?'UNLIMITED-подписка выдана админом без конечного срока; операции со сроком к ней неприменимы.'
      :'Нет подписки — операции со сроком недоступны.';
    return html`<div class="card spaced-card"><div class="card-title">Операции со сроком подписки (PH7-01 · только primary admin)</div>
      <span class="cell-sub">Авторитетен только сервер: продление активной подписки идёт от её текущего срока (DL-044), истёкшей — от текущего момента. WL-периоды не сбрасываются.</span>
      ${blocked?html`<div class="notice notice-amber">${reason}</div>`:html`
      <div class="ops-form grid-two">
        <div class="wide ops-actions">
          ${PRESET_DAYS.map(days=>html`<button class="primary" data-expiry-op="EXTEND_DAYS" data-value="${days}" data-account-id="${detail.account.id}">+${days} дн.</button>`)}
          <button class="danger" data-expiry-op="END_NOW" data-account-id="${detail.account.id}">Завершить сейчас…</button>
        </div>
        <div class="wide ops-actions">
          <input type="number" id="ops-exp-days" min="1" max="3650" step="1" placeholder="дней" style="max-width:110px"/>
          <button data-expiry-op="EXTEND_DAYS" data-custom="1" data-account-id="${detail.account.id}">Продлить на N…</button>
          <button class="danger" data-expiry-op="REDUCE_DAYS" data-custom="1" data-account-id="${detail.account.id}">Сократить на N…</button>
        </div>
        <label class="wide">Точная дата окончания (локальное время)
          <input type="datetime-local" id="ops-exp-exact"/>
          <button data-expiry-op="SET_EXACT" data-account-id="${detail.account.id}">Установить…</button>
        </label>
      </div>`}
    </div>`;
  }

  async function handleExpiryClick(element,ctx){
    const kind=element.dataset.expiryOp;
    if(kind==='SET_EXACT'){
      const raw=document.getElementById('ops-exp-exact').value;
      if(!raw){toast('Выберите дату','err');return;}
      const seconds=Math.floor(new Date(raw).getTime()/1000);
      if(!Number.isFinite(seconds)||seconds<=0){toast('Некорректная дата','err');return;}
      return openExpiryDialog(ctx,{kind,value:seconds});
    }
    if(element.dataset.custom){
      const days=Number(document.getElementById('ops-exp-days').value);
      if(!Number.isInteger(days)||days<1||days>3650){toast('Дни: целое число 1..3650','err');return;}
      return openExpiryDialog(ctx,{kind,value:days});
    }
    return openExpiryDialog(ctx,{kind,value:Number(element.dataset.value)});
  }

  return {expiryCard,handleExpiryClick};
}
