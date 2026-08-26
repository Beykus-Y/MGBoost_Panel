import {badgeClass,formatDuration,formatTimestamp,maskTelegram} from './core.js';

export function createAccountUi({adminFetch,html,renderHtml,showPage,toast}){
  let accounts=[];
  let detail=null;

  const badge=value=>html`<span class="badge ${badgeClass(value)}">${value||'—'}</span>`;

  async function getJson(path,opts){
    const response=await adminFetch(path,opts);
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'request failed');
    return data;
  }

  function renderAccounts(rows=accounts){
    const tbody=document.getElementById('accounts-tbody');
    document.getElementById('accounts-count').textContent=`(${rows.length})`;
    if(!rows.length){
      renderHtml(tbody,html`<tr><td colspan="8" class="empty-state">Аккаунты не найдены</td></tr>`);
      return;
    }
    renderHtml(tbody,html`${rows.map(row=>html`<tr class="clickable" data-action="open-account" data-account-id="${row.id}">
      <td><strong>${row.primary_alias||`Account #${row.id}`}</strong><div class="cell-sub">#${row.id} · ${row.account_source} · aliases ${row.alias_count}</div></td>
      <td>${badge(row.status)}</td>
      <td>${row.subscription?badge(row.subscription.status):badge('NO_SUBSCRIPTION')}<div class="cell-sub">${row.subscription?.display_name||'Нет entitlement'}</div></td>
      <td>${badge(row.telegram_status)}</td>
      <td><strong>${row.active_devices}</strong><div class="cell-sub">активных slot</div></td>
      <td><strong>${row.migrated_devices}</strong><div class="cell-sub">реальных lineage</div></td>
      <td>${row.parent_ready?badge('PARENT_READY'):badge('NOT_READY')}</td>
      <td>${badge(row.migration_action)}</td>
    </tr>`)}`);
  }

  async function loadAccounts(){
    const tbody=document.getElementById('accounts-tbody');
    try{
      const data=await getJson('/admin/accounts');
      accounts=data.accounts||[];
      renderAccounts();
    }catch(error){
      renderHtml(tbody,html`<tr><td colspan="8" class="error-state">Не удалось загрузить аккаунты</td></tr>`);
      throw error;
    }
  }

  function filterAccounts(){
    const query=(document.getElementById('account-search')?.value||'').trim().toLowerCase();
    renderAccounts(accounts.filter(row=>!query||String(row.id).includes(query)||(row.primary_alias||'').toLowerCase().includes(query)));
  }

  function overviewTab(){
    const account=detail.account;
    return html`<div class="detail-grid">
      <div class="detail-item"><div class="detail-label">Account</div><div class="detail-value">#${account.id}</div></div>
      <div class="detail-item"><div class="detail-label">Статус</div><div class="detail-value">${badge(account.status)}</div></div>
      <div class="detail-item"><div class="detail-label">Источник</div><div class="detail-value">${account.account_source}</div></div>
      <div class="detail-item"><div class="detail-label">Создан</div><div class="detail-value">${formatTimestamp(account.created_at)}</div></div>
    </div>
    <div class="card"><div class="card-title">Legacy aliases</div>${detail.aliases.map(alias=>html`<div class="list-row"><div><strong>${alias.legacy_username}</strong><div class="cell-sub">${alias.alias_role} · ${alias.ownership_provenance}</div></div>${badge(alias.legacy_status)}</div>`)}</div>`;
  }

  function subscriptionTab(){
    const sub=detail.subscription;
    const credential=detail.credential;
    return html`<div class="detail-grid">
      <div class="detail-item"><div class="detail-label">Subscription</div><div class="detail-value">${sub?badge(sub.status):'Нет'}</div></div>
      <div class="detail-item"><div class="detail-label">Plan</div><div class="detail-value">${sub?.display_name||'—'}</div></div>
      <div class="detail-item"><div class="detail-label">Expiry</div><div class="detail-value">${sub?.current_expiry?formatTimestamp(sub.current_expiry):'∞'}</div></div>
      <div class="detail-item"><div class="detail-label">Devices</div><div class="detail-value">${sub?.effective?.device_limit_mode==='UNLIMITED'?'UNLIMITED':sub?.effective?.device_limit??'—'}</div></div>
    </div>
    <div class="card"><div class="card-title">Opaque subscription credential</div>
      <div class="list-row"><div><strong>${credential?.status||'Не выпущен'}</strong><div class="cell-sub">${credential?`generation ${credential.generation} · last used ${formatTimestamp(credential.last_used_at)}`:'Токен показывается только один раз после выпуска'}</div></div>
      <button class="primary" data-action="issue-account-credential" data-account-id="${detail.account.id}">${credential?.status==='ACTIVE'?'Перевыпустить':'Выпустить'}</button></div>
      <div id="credential-delivery"></div>
    </div>`;
  }

  function devicesTab(){
    if(!detail.devices.length)return html`<div class="card empty-state">Slots ещё не созданы</div>`;
    return html`<div class="device-grid">${detail.devices.map(device=>html`<div class="card device-card">
      <div class="list-row"><strong>Slot ${device.slot_number}</strong>${badge(device.desired_state)}</div>
      <div class="cell-sub">${device.slot_kind} · observed ${device.observed_state}</div>
      <div class="detail-line"><span>HWID</span><strong>${device.hwid_masked||'—'}</strong></div>
      <div class="detail-line"><span>Child</span><strong>${device.child_observed_state||'не создан'}</strong></div>
      <div class="detail-line"><span>Migration</span>${badge(device.migration_state||'NO_LINEAGE')}</div>
      ${device.child_observed_state==='ACTIVE'&&!device.real_migration_lineage?html`<div class="notice notice-amber">У active child нет real migration lineage. Его нельзя считать migrated customer device; в grace cohort это может быть genesis placeholder.</div>`:''}
    </div>`)}</div>`;
  }

  function telegramTab(){
    return html`<div class="card"><div class="list-row"><div><div class="card-title">Ownership state</div>${badge(detail.telegram.status)}</div></div>
      ${detail.telegram.identities.length?detail.telegram.identities.map(identity=>html`<div class="list-row"><div><strong>Telegram ${maskTelegram(identity.telegram_id)}</strong><div class="cell-sub">${identity.role} · ${identity.provenance} · linked ${formatTimestamp(identity.linked_at)}</div></div>${identity.revoked_at?badge('REVOKED'):badge('ACTIVE')}</div>`):html`<div class="empty-state">Активный OWNER ещё не связан</div>`}
    </div>`;
  }

  function migrationTab(){
    const row=detail.migration_grace;
    const grace=row.grace;
    return html`<div class="detail-grid">
      <div class="detail-item"><div class="detail-label">Action</div><div class="detail-value">${badge(row.action)}</div></div>
      <div class="detail-item"><div class="detail-label">Parent ready</div><div class="detail-value">${row.bridge_enabled&&row.active_devices>0?'Да':'Нет'}</div></div>
      <div class="detail-item"><div class="detail-label">Active slots</div><div class="detail-value">${row.active_devices}</div></div>
      <div class="detail-item"><div class="detail-label">Real migrated lineages</div><div class="detail-value">${row.migrated_devices}</div></div>
    </div>
    <div class="card"><div class="card-title">Grace</div>${grace?html`<div class="list-row"><div><strong>День ${grace.day_of_14}/14</strong><div class="cell-sub">До ${formatTimestamp(grace.current_end_at)} · осталось ${formatDuration(grace.seconds_remaining)}</div></div>${badge(grace.active?'ACTIVE':'ENDED')}</div>`:html`<div class="empty-state">Grace period не запускался</div>`}</div>`;
  }

  function technicalTab(){
    return html`<div class="notice">Технические идентификаторы доступны только на этой вкладке.</div>
      <div class="card"><div class="card-title">Account public ID</div><code class="code-wrap">${detail.technical.account_public_id}</code></div>
      <div class="card"><div class="card-title">Device lineage</div>${detail.technical.device_lineage.length?detail.technical.device_lineage.map(row=>html`<div class="technical-row"><strong>Slot ${row.slot_number} / generation ${row.generation}</strong><code>slot_generation_id=${row.slot_generation_id||'—'}</code><code>child_intent_id=${row.child_intent_id||'—'}</code><code>${row.child_username||'child не создан'}</code><code>${row.hwid_verifier||'HWID verifier отсутствует'}</code><code>${row.uuid_verifier||'UUID verifier отсутствует'}</code><code>outbox_id=${row.outbox_id||'—'} · ${row.operation_id||'—'}</code></div>`):html`<div class="empty-state">Lineage отсутствует</div>`}</div>`;
  }

  const tabRenderers={overview:overviewTab,subscription:subscriptionTab,devices:devicesTab,telegram:telegramTab,migration:migrationTab,technical:technicalTab};

  function showAccountTab(name){
    document.querySelectorAll('#account-tabs .tab').forEach(tab=>tab.classList.toggle('active',tab.dataset.accountTab===name));
    renderHtml(document.getElementById('account-tab-content'),tabRenderers[name]?.()||overviewTab());
  }

  async function openAccount(accountId){
    detail=await getJson(`/admin/accounts/${accountId}`);
    document.getElementById('account-detail-title').textContent=detail.aliases[0]?.legacy_username||`Account #${accountId}`;
    showPage('account-detail');
    showAccountTab('overview');
  }

  async function issueCredential(accountId){
    const reason=prompt('Причина выпуска/перевыпуска credential (3–300 символов):');
    if(reason===null)return;
    if(reason.trim().length<3){toast('Укажите причину не короче 3 символов','err');return;}
    const rotating=detail?.credential?.status==='ACTIVE';
    if(rotating&&!confirm('Текущий opaque credential будет немедленно отозван. Продолжить?'))return;
    const response=await adminFetch(`/admin/accounts/${accountId}/subscription-credential/issue`,{method:'POST',body:JSON.stringify({reason:reason.trim(),confirm:rotating})});
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'issue failed');
    const delivery=document.getElementById('credential-delivery');
    renderHtml(delivery,html`<div class="notice notice-success"><strong>Сохраните URL сейчас — повторно он не показывается.</strong><div class="credential-row"><input id="issued-credential-url" readonly value="${data.canonical_url}"/><button data-action="copy-issued-credential">Копировать</button></div></div>`);
    detail.credential=data.credential;
    toast('Credential выпущен');
  }

  async function loadMigration(){
    const tbody=document.getElementById('migration-tbody');
    const data=await getJson('/admin/migration-grace');
    renderHtml(tbody,html`${data.accounts.map(row=>html`<tr class="clickable" data-action="open-account" data-account-id="${row.account_id}">
      <td><strong>${row.primary_alias||`Account #${row.account_id}`}</strong><div class="cell-sub">#${row.account_id}</div></td>
      <td>${badge(row.action)}</td><td>${badge(row.telegram_status)}</td>
      <td>${row.bridge_enabled&&row.active_devices>0?'Да':'Нет'}</td><td>${row.active_devices}</td><td>${row.migrated_devices}</td>
      <td>${row.grace?`День ${row.grace.day_of_14}/14 · ${formatDuration(row.grace.seconds_remaining)}`:'—'}</td>
      <td>${row.legacy_requests_72h}</td>
    </tr>`)}`);
  }

  async function loadDashboard(){
    const target=document.getElementById('account-dashboard');
    try{
      const data=await getJson('/admin/dashboard');
      const campaign=data.grace_campaign;
      renderHtml(target,html`
        ${campaign?html`<section class="dashboard-section"><div class="section-heading"><div><span class="eyebrow">Grace campaign</span><h2>День ${campaign.day_of_14}/14</h2></div><button data-action="show-page" data-page="migration">Открыть Migration / Grace</button></div>
          <div class="stats-grid"><div class="stat-card"><div class="stat-label">Аккаунты</div><div class="stat-value">${campaign.accounts_total}</div><div class="stat-sub">parent-ready ${campaign.parent_ready}</div></div><div class="stat-card"><div class="stat-label">Telegram BOUND</div><div class="stat-value">${campaign.telegram_bound}</div><div class="stat-sub">waiting ${campaign.waiting_for_registration}</div></div><div class="stat-card"><div class="stat-label">Real device lineages</div><div class="stat-value">${campaign.real_devices_child_backed}/${campaign.real_device_lineages}</div><div class="stat-sub">child-backed / всего; active slots ${campaign.active_slots}</div></div><div class="stat-card"><div class="stat-label">Осталось</div><div class="stat-value small-value">${formatDuration(campaign.seconds_remaining)}</div><div class="stat-sub">до ${formatTimestamp(campaign.ends_at)}</div></div></div>
          ${(campaign.reconcile_blockers||campaign.compatibility_blockers)?html`<div class="notice notice-amber">Blockers: reconcile ${campaign.reconcile_blockers}, compatibility ${campaign.compatibility_blockers}</div>`:''}</section>`:''}
        <section class="dashboard-section"><div class="section-heading"><div><span class="eyebrow">Operational health</span><h2>${data.health.error_reconcile||data.health.slot_state_mismatches||data.health.child_state_mismatches?'Требует внимания':'Локальное состояние согласовано'}</h2></div></div><div class="stats-grid"><div class="stat-card"><div class="stat-label">ERROR_RECONCILE</div><div class="stat-value">${data.health.error_reconcile}</div></div><div class="stat-card"><div class="stat-label">Resolver errors 72h</div><div class="stat-value">${data.health.resolver_errors_72h}</div></div><div class="stat-card"><div class="stat-label">Desired / observed</div><div class="stat-value">${data.health.slot_state_mismatches+data.health.child_state_mismatches}</div><div class="stat-sub">slot + child mismatches</div></div><div class="stat-card clickable" data-action="show-page" data-page="tickets"><div class="stat-label">Тикеты</div><div class="stat-value">${data.tickets.open}</div><div class="stat-sub">без ответа ${data.tickets.unanswered}</div></div></div></section>
        <section class="dashboard-section"><div class="section-heading"><div><span class="eyebrow">Expiring soon</span><h2>Ближайшие подписки</h2></div></div><div class="expiry-buckets"><span>Сегодня ${data.expiring.buckets.today}</span><span>≤3 дней ${data.expiring.buckets.three_days}</span><span>≤7 дней ${data.expiring.buckets.seven_days}</span><span>≤30 дней ${data.expiring.buckets.thirty_days}</span></div>${data.expiring.accounts.length?html`<div class="compact-list">${data.expiring.accounts.slice(0,6).map(row=>html`<button class="compact-row" data-action="open-account" data-account-id="${row.id}"><span>${row.label}</span><strong>${formatDuration(row.seconds_remaining)}</strong></button>`)}</div>`:html`<div class="empty-state">Нет ближайших истечений</div>`}</section>`);
    }catch(error){renderHtml(target,html`<div class="notice notice-amber">Account dashboard временно недоступен</div>`);throw error;}
  }

  document.addEventListener('click',event=>{
    const element=event.target.closest('[data-action]');
    if(!element)return;
    let work;
    if(element.dataset.action==='open-account')work=openAccount(Number(element.dataset.accountId));
    if(element.dataset.action==='account-tab')showAccountTab(element.dataset.accountTab);
    if(element.dataset.action==='issue-account-credential')work=issueCredential(Number(element.dataset.accountId));
    if(element.dataset.action==='copy-issued-credential')work=navigator.clipboard.writeText(document.getElementById('issued-credential-url').value).then(()=>toast('Скопировано'));
    if(work)Promise.resolve(work).catch(error=>{console.error(error);toast('Операция не выполнена','err');});
  });
  document.addEventListener('input',event=>{if(event.target.id==='account-search')filterAccounts();});

  return {loadAccounts,loadDashboard,loadMigration,openAccount};
}
