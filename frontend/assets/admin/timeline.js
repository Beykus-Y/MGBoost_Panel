// Unified read-only audit timeline renderer + dashboard operator queues.
// Every entry is display-only: the backend aggregator already excluded raw
// identifiers; technical IDs stay in the Technical tab.

const SOURCE_BADGES={
  ENTITLEMENT_MUTATION:'tl-src-entitlement',PAYMENT_RECORD:'tl-src-payment',
  MANUAL_PAYMENT:'tl-src-payment',MANUAL_PAYMENT_EDIT:'tl-src-payment',
  DEVICE_LIFECYCLE:'tl-src-device',MIGRATION_BINDING:'tl-src-migration',
  LEGACY_GRACE:'tl-src-migration',SUBSCRIPTION_CREDENTIAL:'tl-src-credential',
  OWNERSHIP_REBIND:'tl-src-ownership',
};

export function createTimeline({html,formatTimestamp,humanLabel}){
  function renderTimeline(timeline){
    if(!timeline||!Array.isArray(timeline.entries))return html`<div class="empty-state">История недоступна</div>`;
    if(!timeline.entries.length)return html`<div class="empty-state">Существенных событий ещё не было</div>`;
    const byDay=new Map();
    for(const entry of timeline.entries){
      const day=new Date(entry.ts*1000).toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric'});
      if(!byDay.has(day))byDay.set(day,[]);
      byDay.get(day).push(entry);
    }
    return html`<div class="timeline-list">${[...byDay.entries()].map(([day,entries])=>html`
      <div class="timeline-day"><div class="timeline-day-title">${day}</div>
      ${entries.map(entry=>{const detailKeys=Object.keys(entry.detail||{}).sort();
        return html`<div class="timeline-item">
          <span class="timeline-time">${new Date(entry.ts*1000).toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})}</span>
          <span class="badge ${SOURCE_BADGES[entry.source]||'badge-gray'}">${humanLabel(entry.source)}</span>
          <div class="timeline-body"><strong>${entry.label}</strong>${detailKeys.length?html`
            <details class="timeline-details"><summary>Детали</summary><dl class="ops-dl">${detailKeys.map(key=>html`
              <dt>${key}</dt><dd>${String(entry.detail[key])}</dd>`)}</dl></details>`:''}
          </div>
        </div>`;})}
      </div>`)}
    </div>${timeline.truncated?html`<div class="cell-sub">Показаны последние события; полная история доступна в каждой доменной таблице.</div>`:html``}`;
  }

  function paymentQueueItem(item){
    const product=item.plan_code
      ?`${humanLabel(item.plan_code)} · ${item.duration_days} дн.`
      :`${humanLabel(item.package_sku)}`;
    return html`<button class="compact-row queue-row" data-action="open-account" data-account-id="${item.account_id}" data-open-tab="payments">
      <span><strong>${item.label}</strong><small>${product} · ${item.amount_minor} ₽ · ${item.public_id||''}${item.sync_state&&item.sync_state!=='SYNCED'?html` · sync: ${humanLabel(item.sync_state)}`:''}</small></span>
      <strong>${item.amount_minor} ₽</strong></button>`;
  }

  function queuesSection(queues){
    if(!queues)return html``;
    const pending=(queues.pending||[]);
    const review=(queues.manual_review||[]);
    const syncPending=(queues.sync_pending||[]);
    const stars=queues.stars_manual_review||{count:0,items:[]};
    return html`<section class="dashboard-section"><div class="section-heading"><div>
      <span class="eyebrow">Операционные очереди</span><h2>Ручные платежи и сбои</h2></div>
      <button data-action="show-page" data-page="accounts">К аккаунтам</button></div>
      ${(!pending.length&&!review.length&&!syncPending.length&&!stars.count)?html`<div class="empty-state">Очереди пусты — платёжных и sync-исключений нет</div>`:
      html`<div class="queue-grid">
        ${pending.length?html`<div class="queue-block"><h4>Pending-платежи (${pending.length})</h4><div class="compact-list">${pending.map(paymentQueueItem)}</div></div>`:''}
        ${review.length?html`<div class="queue-block"><h4>Ручная проверка (${review.length})</h4><div class="compact-list">${review.map(paymentQueueItem)}</div></div>`:''}
        ${syncPending.length?html`<div class="queue-block"><h4>Child-sync не завершён (${syncPending.length})</h4><div class="compact-list">${syncPending.map(item=>html`
          <button class="compact-row queue-row" data-action="open-account" data-account-id="${item.account_id}" data-open-tab="payments">
            <span><strong>${item.label}</strong><small>платёж #${item.payment_record_id} · ${humanLabel(item.state)}</small></span>
            <span class="badge ${item.state==='MANUAL_REVIEW'?'badge-red':'badge-amber'}">${humanLabel(item.state)}</span></button>`)}</div></div>`:''}
        ${stars.count?html`<div class="queue-block"><h4>Stars manual-review (legacy экран Stars)</h4><p class="cell-sub">К оплатам Stars применяется отдельный существующий reconciliation на экране «Stars»; всего в статусе: ${stars.count}</p></div>`:''}
      </div>}`}
    </section>`;
  }

  return {renderTimeline,queuesSection};
}
