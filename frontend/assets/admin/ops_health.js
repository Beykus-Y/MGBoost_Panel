// PH7-16 Wave 2 -- Operations / Health: the first-ever UI consumer of
// GET /admin/ops/health (PH8-04 Step 2, production, read-only). Renders
// exactly what the endpoint composes -- fail-open per signal, `sources`
// telling us which ones actually resolved -- and nothing invented: no
// alert thresholds (PH8-04 sets none by design), no fabricated "all
// green" state. A signal whose `sources[name]` is `"UNKNOWN"` renders as
// "не удалось получить" so an operator never mistakes absence of data for
// a healthy value. This is fleet-level health, not per-account state --
// deliberately not folded into the Account workspace's Subscription tab
// (see PH7-16 plan §6/§14 Gate D).
export function createOpsHealth({html,renderHtml,toast,getJson,formatTimestamp,formatDuration}){
  function statusBadge(ok){return html`<span class="badge ${ok?'badge-green':'badge-red'}">${ok?'OK':'DEGRADED'}</span>`;}
  function unknownNotice(stub){return html`<div class="notice notice-amber">Сигнал недоступен (${stub?.error_class||'не инструментировано'}) — данные не показываются, чтобы не выдать отсутствие информации за здоровое состояние.</div>`;}

  function collectorFreshnessBlock(sources,data){
    if(sources.wl_reconciliation_backlog!=='OK')return unknownNotice(data);
    const f=data;
    return html`<dl class="ops-dl">
      <dt>Свежесть</dt><dd>${f.fresh?html`<span class="badge badge-green">свежо</span>`:html`<span class="badge badge-red">устарело</span>`} · возраст ${f.age_seconds===null?'—':formatDuration(f.age_seconds)} (порог ${formatDuration(f.max_age_seconds)})</dd>
      <dt>Последний успешный запуск</dt><dd>${formatTimestamp(f.last_ok_run_at)}</dd>
      <dt>Исход последнего запуска</dt><dd>${f.last_run_outcome||'—'}${f.last_run_error_class?` · ${f.last_run_error_class}`:''}</dd>
    </dl>`;
  }

  function outboxBlock(sources,data){
    if(sources.wl_reconciliation_backlog!=='OK')return unknownNotice(data);
    const counts=data.op_counts||{};
    const entries=Object.entries(counts);
    return html`<dl class="ops-dl">
      <dt>Очередь по состояниям</dt><dd>${entries.length?entries.map(([state,n])=>`${state}: ${n}`).join(' · '):'пусто'}</dd>
      <dt>Возраст старейшей записи</dt><dd>${data.oldest_backlog_age_seconds===null?'—':formatDuration(data.oldest_backlog_age_seconds)}</dd>
    </dl>`;
  }

  function driftBlock(sources,data){
    if(sources.wl_reconciliation_backlog!=='OK')return unknownNotice(data);
    return html`<dl class="ops-dl">
      <dt>Обнаружено расхождений</dt><dd>${data.detected}</dd>
      <dt>Запланирован ремонт</dt><dd>${data.repaired}</dd>
      <dt>Помечено для ручной проверки</dt><dd>${data.flagged}</dd>
    </dl>`;
  }

  function workerHealthBlock(sources,data){
    if(sources.wl_reconciliation_backlog!=='OK')return unknownNotice(data);
    return html`<dl class="ops-dl">
      <dt>Планировщик видел цикл</dt><dd>${data.scheduler_seen?html`<span class="badge badge-green">да</span>`:html`<span class="badge badge-red">нет</span>`}</dd>
      <dt>Последний цикл завершён</dt><dd>${formatTimestamp(data.last_cycle_finished_at)}</dd>
      <dt>Исход</dt><dd>${data.last_cycle_outcome||'—'}</dd>
    </dl>`;
  }

  function cycleBlock(label,sources,data){
    if(sources.wl_reconciliation_backlog!=='OK')return html`<div class="detail-line"><span>${label}</span>${unknownNotice(data)}</div>`;
    if(!data)return html`<div class="detail-line"><span>${label}</span><strong>ещё не запускался</strong></div>`;
    return html`<div class="detail-line"><span>${label}</span><strong>#${data.cycle_id} · ${data.outcome}</strong><div class="cell-sub">${data.trigger||'—'} · начат ${formatTimestamp(data.started_at)}${data.finished_at?` · завершён ${formatTimestamp(data.finished_at)}`:' · выполняется'}</div></div>`;
  }

  function monotonicityCard(sources,data){
    const body=sources.monotonicity!=='OK'?unknownNotice(data):html`<dl class="ops-dl">
      <dt>Окно наблюдения</dt><dd>${formatDuration(data.lookback_seconds)}</dd>
      <dt>Обнаруженных сбросов счётчика</dt><dd>${data.reset_events}</dd>
      <dt>Затронуто курсоров</dt><dd>${data.distinct_cursors_affected}</dd>
    </dl>`;
    return html`<div class="card"><div class="card-title">Монотонность WL-счётчиков</div><div class="cell-sub">Считает сбросы (Marzban traffic reset), а не устаревание — это отдельный сигнал от свежести коллектора.</div>${body}</div>`;
  }

  function backlogQueueCard(title,hint,sources,sourceName,data){
    const body=sources[sourceName]!=='OK'?unknownNotice(data):html`<dl class="ops-dl">
      <dt>В очереди сейчас</dt><dd>${data.count_in_state}</dd>
      <dt>Возраст самой старой записи</dt><dd>${data.oldest_age_seconds===null?'—':formatDuration(data.oldest_age_seconds)}</dd>
      <dt>Повторных пометок</dt><dd>${data.reconcile_stale_recurrences_total??data.manual_review_retries_total}</dd>
    </dl>`;
    return html`<div class="card"><div class="card-title">${title}</div><div class="cell-sub">${hint}</div>${body}</div>`;
  }

  async function loadOpsHealth(){
    const box=document.getElementById('ops-health-box');
    if(!box)return;
    renderHtml(box,html`<div class="loading"><span class="spinner"></span>Загрузка...</div>`);
    let data;
    try{data=await getJson('/admin/ops/health');}
    catch(error){renderHtml(box,html`<div class="notice notice-amber">Не удалось загрузить состояние операций</div>`);throw error;}
    const sources=data.sources||{};
    renderHtml(box,html`
      <div class="card">
        <div class="card-title">Общий статус ${statusBadge(data.status==='OK')}</div>
        <div class="cell-sub">Обновлено ${formatTimestamp(data.generated_at)}</div>
      </div>
      <div class="card">
        <div class="card-title">WL Reconciliation</div>
        <div class="cell-sub">Свежесть коллектора usage-телеметрии, очередь enforcement-операций, обнаруженные расхождения desired/observed, здоровье воркера.</div>
        <div class="detail-grid">
          <div><strong>Свежесть коллектора</strong>${collectorFreshnessBlock(sources,data.collector_freshness)}</div>
          <div><strong>Очередь enforcement</strong>${outboxBlock(sources,data.outbox)}</div>
          <div><strong>Расхождения (drift)</strong>${driftBlock(sources,data.drift)}</div>
          <div><strong>Воркер</strong>${workerHealthBlock(sources,data.worker_health)}</div>
        </div>
        ${cycleBlock('Последний цикл',sources,data.last_reconciliation_cycle)}
        ${cycleBlock('Последний успешный цикл',sources,data.last_successful_reconciliation_cycle)}
      </div>
      ${monotonicityCard(sources,data.monotonicity)}
      ${backlogQueueCard('Миграция: ошибки сверки (ERROR_RECONCILE)','Биндинги, застрявшие в ERROR_RECONCILE прямо сейчас.',sources,'error_reconcile',data.error_reconcile)}
      ${backlogQueueCard('P0: ручная проверка легаси-переходов (MANUAL_REVIEW)','Legacy→commercial переходы, ожидающие ручного решения.',sources,'legacy_transition_review',data.legacy_transition_review)}
      <div class="cell-sub">Пороги алертов не заданы намеренно (PH8-04) — это read-only снимок состояния, не система оповещений.</div>
    `);
  }

  return {loadOpsHealth};
}
