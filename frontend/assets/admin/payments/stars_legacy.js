// PH7-16 Wave 5 -- Telegram Stars screen, ported out of admin.js verbatim.
// Owner-corrected placement: under top-level Payments (real revenue
// channel, kept legacy-implemented), not Technical.
//
// PH7-16 Wave H hardened the refund/reconcile-refund handlers in
// src/routes/admin.py to require primary-admin capability + a mandatory
// `reason` in the JSON body -- this is the mandatory companion fix the
// owner required before this move: without it, refund/reconcile-refund
// here would 400 for every admin, including primary. `promptReason` is
// injected from admin.js, the same helper backing the equivalent fix in
// admin/technical/marzban_users.js.
//
// Fully self-contained otherwise: no state shared with any other screen.
export function createStarsLegacyUi({html,renderHtml,promptReason,proxyApi}){
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
    let body;
    if(action==='refund'||action==='reconcile-refund'){
      if(action==='refund'&&!confirm('Выполнить возврат Stars за этот платёж?'))return;
      const reason=promptReason(action==='refund'?'Причина возврата (3–300 символов, попадёт в audit)':'Причина сверки возврата (3–300 символов)');
      if(reason===null)return;
      body=JSON.stringify({reason});
    }
    const r=await proxyApi(`/admin/stars-payments/${id}/${action}`,{method:'POST',body});
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
    let body;
    if(action==='refund'||action==='reconcile-refund'){
      if(action==='refund'&&!confirm('Выполнить возврат Stars за эту непривязанную оплату?'))return;
      const reason=promptReason(action==='refund'?'Причина возврата (3–300 символов, попадёт в audit)':'Причина сверки возврата (3–300 символов)');
      if(reason===null)return;
      body=JSON.stringify({reason});
    }
    const r=await proxyApi(`/admin/stars-orphan-payments/${id}/${action}`,{method:'POST',body});
    const data=await r.json().catch(()=>({}));
    if(!r.ok){alert(data.error||'Ошибка');return;}
    if(data.message)alert(data.message);
    loadStarsOrphans();
  }

  return {loadStarsSettings,saveStarsSettings,loadStarsTariffs,addStarsTariff,toggleStarsTariff,
    deleteStarsTariff,loadStarsPayments,starsPaymentAction,loadStarsOrphans,starsOrphanAction};
}
