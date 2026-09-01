// PH7-16 Wave 3 -- Operations / Legacy Transitions: cross-account queue of
// every P0 legacy->commercial transition still in flight, over the
// existing GET /admin/legacy-transitions (Wave 3 addition, read-only) and
// GET /admin/legacy-transitions/{id} routes. This is deliberately NOT a
// second transition UI -- clicking a row opens the exact same modal
// (openLegacyTransitionById, from admin/payments.js) the account's
// Payments tab already uses for the single-account entry point; "same
// components, two list contexts," not a duplicate implementation.
export function createLegacyTransitionsQueue({html,renderHtml,toast,adminFetch,getJson,formatTimestamp,humanLabel,openLegacyTransitionById,openAccount}){
  function stateNotice(state,reviewReason){
    if(state==='MANUAL_REVIEW')return html`<span class="cell-sub">${reviewReason||'Требуется ручная проверка'}</span>`;
    if(state==='PENDING_PAYMENT')return html`<span class="cell-sub">Ожидает подтверждения оплаты</span>`;
    if(state==='SELECTION_REQUIRED')return html`<span class="cell-sub">Ожидает выбора устройств для превышения лимита</span>`;
    return '';
  }

  function row(transition){
    return html`<div class="list-row clickable" data-action="open-legacy-transition" data-transition-id="${transition.id}" data-account-id="${transition.account_id}">
      <div>
        <strong>${transition.label}</strong>
        <div class="cell-sub">${humanLabel(transition.source_plan_code)} → ${humanLabel(transition.target_plan_code)} · обновлено ${formatTimestamp(transition.updated_at)}</div>
        ${stateNotice(transition.state,transition.review_reason)}
      </div>
      <span class="badge ${transition.state==='MANUAL_REVIEW'?'badge-amber':'badge-gray'}">${humanLabel(transition.state)}</span>
    </div>`;
  }

  async function loadQueue(){
    const box=document.getElementById('legacy-transitions-box');
    if(!box)return;
    renderHtml(box,html`<div class="loading"><span class="spinner"></span>Загрузка...</div>`);
    let data;
    try{data=await getJson('/admin/legacy-transitions');}
    catch(error){renderHtml(box,html`<div class="notice notice-amber">Не удалось загрузить очередь переходов</div>`);throw error;}
    const transitions=data.transitions||[];
    if(!transitions.length){renderHtml(box,html`<div class="empty-state">Открытых переходов нет</div>`);return;}
    renderHtml(box,html`<div class="card flush-card">${transitions.map(row)}</div>`);
  }

  function handleQueueClick(element){
    if(element.dataset.action!=='open-legacy-transition')return;
    const transitionId=Number(element.dataset.transitionId);
    const accountId=Number(element.dataset.accountId);
    openLegacyTransitionById(transitionId,{account:{id:accountId},adminFetch,reload:loadQueue})
      .catch(error=>{console.error(error);toast(error.message||'Не удалось открыть переход','err');});
  }

  return {loadQueue,handleQueueClick};
}
