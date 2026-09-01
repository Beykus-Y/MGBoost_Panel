// PH5-12 admin delivery-routing UI: live host inventory + STANDARD
// membership management. The frontend is never the authority: WL
// classification comes from the server's exact PH0-05 check, WL and
// wl-shaped hosts render a permanently disabled action with the server's
// own reason, and every accepted mutation requires a mandatory reason plus
// one idempotency key per opened dialog, guarded server-side by the
// profile's CAS row_version.

const _V = `./core.js${new URL(import.meta.url).search}`;
const { formatTimestamp } = await import(_V);
const { createModals } = await import(`./modals.js${new URL(import.meta.url).search}`);

const CLASS_LABELS = {
  WL_EXACT: 'WL (exact PH0-05)',
  WL_SUSPECT: 'wl-like · не верифицирован',
  STANDARD: 'обычный хост',
};

export function createRoutingUi({ adminFetch, getJson, html, renderHtml, toast, hostClassBadgeClass }) {
  let state = null;
  const { confirmFlow } = createModals({ html, renderHtml });

  function classBadge(cls) {
    return html`<span class="badge ${hostClassBadgeClass(cls)}">${CLASS_LABELS[cls] || cls}</span>`;
  }

  function wlReason(host) {
    return host.classification === 'WL_EXACT'
      ? 'Доказанный WL-хост: попадание в STANDARD запрещено на бэкенде (exact PH0-05 allowlist).'
      : 'wl-подобное имя отсутствует в exact-топологии — переклассификация fail-closed.';
  }

  function memberChip(host) {
    return html`<div class="pool-chip pool-chip--member">
      <span class="pool-chip-tag">${host.inbound_tag}</span>
      <button class="pool-chip-action" data-action="routing-host-op" data-routing-op="REMOVE" data-tag="${host.inbound_tag}" title="Убрать из STANDARD">−</button>
    </div>`;
  }

  function availableChip(host) {
    return html`<div class="pool-chip pool-chip--available" data-action="routing-host-op" data-routing-op="ADD" data-tag="${host.inbound_tag}">
      <span class="pool-chip-tag">${host.inbound_tag}</span>
      <span class="pool-chip-action pool-chip-action--add" title="Добавить в STANDARD">+</span>
    </div>`;
  }

  function lockedChip(host) {
    return html`<div class="pool-chip pool-chip--locked" title="${wlReason(host)}">
      <span class="pool-chip-lock">⛔</span>
      <span class="pool-chip-tag">${host.inbound_tag}</span>
      ${classBadge(host.classification)}
    </div>`;
  }

  function render() {
    const box = document.getElementById('routing-box');
    if (!state) { renderHtml(box, html`<p class="muted">Загрузка…</p>`); return; }
    const topo = state.topology || {};
    const hosts = state.hosts || [];
    const members = hosts.filter(h => h.classification === 'STANDARD' && h.in_standard);
    const available = hosts.filter(h => h.classification === 'STANDARD' && !h.in_standard);
    const excluded = hosts.filter(h => h.classification !== 'STANDARD');
    const events = (state.events || []).slice(0, 12).map(ev => html`<li>
      <strong>${ev.event_type}</strong> ${ev.inbound_tag ? html`· ${ev.inbound_tag}` : ''}
      <span class="cell-sub">${ev.actor_ref || ev.actor_type} · ${formatTimestamp(ev.created_at)} · ${ev.reason}</span>
    </li>`);
    renderHtml(box, html`
      ${topo.ok === false ? html`<div class="notice notice-amber">Топология сейчас нездорова (${topo.error_class || 'mismatch'}) — мутации роутинга заблокированы fail-closed. Обновите страницу позже.</div>` : ''}
      <div class="routing-meta cell-sub">profile row_version: ${state.profile_row_version ?? '—'} · топология проверена: ${topo.checked_at ? formatTimestamp(topo.checked_at) : '—'}</div>
      <div class="routing-layout">
        <div class="routing-main">
          <div class="pool-board">
            <section class="pool-zone pool-zone--standard">
              <div class="pool-zone-head"><div><span class="eyebrow">Delivery profile</span><h2>STANDARD pool</h2></div><span class="pool-count">${members.length} хост${members.length===1?'':'ов'}</span></div>
              <div class="pool-chips">${members.length?members.map(memberChip):html`<div class="pool-empty">Пул пуст</div>`}</div>
              ${available.length?html`<div class="pool-zone-sub">Доступны для добавления</div><div class="pool-chips">${available.map(availableChip)}</div>`:''}
            </section>
            <section class="pool-zone pool-zone--excluded">
              <div class="pool-zone-head"><div><span class="eyebrow">Fail-closed</span><h2>Excluded · WL</h2></div><span class="pool-count">${excluded.length} хост${excluded.length===1?'':'ов'}</span></div>
              <div class="pool-chips">${excluded.length?excluded.map(lockedChip):html`<div class="pool-empty">Нет WL-хостов в топологии</div>`}</div>
              ${!topo.ok?html`<p class="cell-sub">Состав hosts показан по последним данным; для мутаций требуется свежая успешная проверка топологии.</p>`:''}
            </section>
          </div>
        </div>
        <aside class="routing-rail">
          <div class="card rail-card">
            <div class="card-title">Планы → профиль доставки</div>
            <p class="cell-sub">Operational routing, не тариф: замена хостов не требует новой версии тарифа и не требует перепокупки.</p>
            <ul class="plain-list">${Object.entries(state.plan_delivery || {}).map(([plan, profile]) => html`<li><strong>${plan}</strong> → ${profile}</li>`)}</ul>
          </div>
          <div class="card rail-card">
            <div class="card-title">Последние события (immutable audit)</div>
            <ul class="routing-events">${events}</ul>
          </div>
        </aside>
      </div>`);
  }

  function openMutationDialog(op, tag) {
    const idempotencyKey = `adm-routing-${crypto.randomUUID()}`;
    const label = op === 'ADD' ? 'Добавить' : 'Убрать';
    confirmFlow({
      title: `${label} «${tag}» ${op === 'ADD' ? 'в STANDARD' : 'из STANDARD'}`,
      body: html`<div class="ops-form"><label>Причина (обязательно, попадёт в immutable audit)
        <input type="text" id="routing-reason" maxlength="300"/></label></div>
        <div class="notice notice-amber">Изменение влияет на provisioning новых шаблонов/устройств; существующие child'ы продолжают работать с зафиксированным membership. CAS: при параллельном изменении вы получите 409 и обновите страницу.</div>`,
      confirmLabel: `${label} хост`,
      busyLabel: 'Применяю…',
      onConfirm: async m => {
        const reason = m.el.querySelector('#routing-reason').value.trim();
        if (reason.length < 3) { toast('Причина минимум 3 символа', 'err'); return false; }
        const response = await adminFetch(`/admin/routing/hosts/${op === 'ADD' ? 'add' : 'remove'}`, {
          method: 'POST',
          body: JSON.stringify({
            inbound_tag: tag,
            reason, idempotency_key: idempotencyKey,
            expected_row_version: state.profile_row_version,
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || 'routing change failed');
        m.close();
        toast(op === 'ADD' ? 'Хост добавлен в STANDARD' : 'Хост убран из STANDARD');
        await loadRouting();
      },
    });
  }

  async function loadRouting() {
    try {
      state = await getJson('/admin/routing/hosts');
    } catch (error) {
      toast(error.message, 'err');
      return;
    }
    render();
  }

  function handleRoutingClick(element) {
    const op = element.dataset.routingOp;
    const tag = element.dataset.tag;
    if (!op || !tag || element.disabled) return;
    openMutationDialog(op, tag);
  }

  return { loadRouting, handleRoutingClick };
}
