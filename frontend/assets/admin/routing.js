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

  function hostRow(host) {
    const blocked = host.classification !== 'STANDARD';
    const reason = host.classification === 'WL_EXACT'
      ? 'Доказанный WL-хост: попадание в STANDARD запрещено на бэкенде (exact PH0-05 allowlist).'
      : host.classification === 'WL_SUSPECT'
        ? 'wl-подобное имя отсутствует в exact-топологии — переклассификация fail-closed.'
        : '';
    return html`<tr>
      <td><strong>${host.inbound_tag}</strong></td>
      <td>${classBadge(host.classification)}</td>
      <td>${host.in_standard ? html`<span class="badge badge-green">в STANDARD</span>` : html`<span class="badge badge-gray">нет</span>`}</td>
      <td class="routing-action-cell">
        ${blocked ? html`<button disabled title="${reason}">Недоступно</button>
          <div class="cell-sub">${reason}</div>`
          : host.in_standard
            ? html`<button class="quiet small" data-action="routing-host-op" data-routing-op="REMOVE" data-tag="${host.inbound_tag}">Убрать из STANDARD</button>`
            : html`<button class="primary" data-action="routing-host-op" data-routing-op="ADD" data-tag="${host.inbound_tag}">Добавить в STANDARD</button>`}
      </td>
    </tr>`;
  }

  function render() {
    const box = document.getElementById('routing-box');
    if (!state) { renderHtml(box, html`<p class="muted">Загрузка…</p>`); return; }
    const topo = state.topology || {};
    const rows = (state.hosts || []).map(hostRow);
    const events = (state.events || []).slice(0, 12).map(ev => html`<li>
      <strong>${ev.event_type}</strong> ${ev.inbound_tag ? html`· ${ev.inbound_tag}` : ''}
      <span class="cell-sub">${ev.actor_ref || ev.actor_type} · ${formatTimestamp(ev.created_at)} · ${ev.reason}</span>
    </li>`);
    renderHtml(box, html`
      ${topo.ok === false ? html`<div class="notice notice-amber">Топология сейчас нездорова (${topo.error_class || 'mismatch'}) — мутации роутинга заблокированы fail-closed. Обновите страницу позже.</div>` : ''}
      <div class="routing-layout">
        <div class="card routing-main">
          <div class="card-title">Live hosts → STANDARD membership
            <span class="cell-sub"> profile row_version: ${state.profile_row_version ?? '—'} · топология проверена: ${topo.checked_at ? formatTimestamp(topo.checked_at) : '—'}</span>
          </div>
          <div class="table-wrap"><table>
            <thead><tr><th>Inbound tag</th><th>Классификация</th><th>STANDARD</th><th>Действие</th></tr></thead>
            <tbody>${rows}</tbody>
          </table></div>
          ${!topo.ok ? html`<p class="cell-sub">Состав hosts показан по последним данным; для мутаций требуется свежая успешная проверка топологии.</p>` : ''}
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
