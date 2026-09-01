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

  return {renderTimeline};
}
