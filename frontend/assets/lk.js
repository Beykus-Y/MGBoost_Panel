const app = document.getElementById('app');

function getToken() {
  return new URLSearchParams(location.search).get('token') || '';
}

function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return bytes.toFixed(i >= 2 ? 2 : 0) + ' ' + units[i];
}

function formatExpire(ts) {
  if (!ts) return 'Бессрочно';
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diff = d - now;
  const days = Math.ceil(diff / 86400000);
  const dateStr = d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
  if (diff <= 0) return `Истёк ${dateStr}`;
  return `${days} дн. (${dateStr})`;
}

function formatRelTime(ts) {
  const diff = Math.floor((Date.now() / 1000) - ts);
  if (diff < 60) return 'только что';
  if (diff < 3600) return `${Math.floor(diff / 60)} мин назад`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} ч назад`;
  if (diff < 172800) return 'вчера';
  const d = new Date(ts * 1000);
  return d.toLocaleDateString('ru-RU') + ' ' + d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
}

function statusLabel(status) {
  const map = {
    active: ['Активен', 'badge-active'],
    expired: ['Истёк', 'badge-expired'],
    disabled: ['Отключён', 'badge-disabled'],
    limited: ['Лимит', 'badge-expired'],
    on_hold: ['На паузе', 'badge-disabled'],
  };
  return map[status] || ['Неизвестно', 'badge-unknown'];
}

function skeleton(n = 3) {
  return Array.from({ length: n }, () => '<div class="skeleton"></div>').join('');
}

async function apiFetch(path) {
  const token = getToken();
  const res = await fetch(`/lk/api/${path}?token=${encodeURIComponent(token)}`);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

async function apiDelete(path) {
  const token = getToken();
  const res = await fetch(`/lk/api/${path}?token=${encodeURIComponent(token)}`, { method: 'DELETE' });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

async function apiPatch(path, data) {
  const token = getToken();
  const res = await fetch(`/lk/api/${path}?token=${encodeURIComponent(token)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || `HTTP ${res.status}`);
  }
  return res.json();
}

function renderTokenForm() {
  app.innerHTML = `
    <div class="card">
      <div class="card-title">Вход</div>
      <div class="token-form">
        <p>Введите ваш токен подписки, чтобы открыть личный кабинет.</p>
        <input class="token-input" id="tokenInput" type="text" placeholder="Вставьте токен из ссылки подписки" autocomplete="off">
        <button class="btn btn-primary" onclick="openWithToken()">Открыть кабинет</button>
      </div>
    </div>`;
}

function openWithToken() {
  const val = document.getElementById('tokenInput')?.value.trim();
  if (!val) return;
  // Handle full URL or bare token
  let token = val;
  try {
    const u = new URL(val);
    token = u.searchParams.get('token') || val;
  } catch {}
  location.href = `/lk/?token=${encodeURIComponent(token)}`;
}

async function renderDashboard(token) {
  // Render shell with loading states
  app.innerHTML = `
    <div class="card" id="statusCard">
      <div class="card-title">Статус аккаунта</div>
      ${skeleton(4)}
    </div>
    <div class="card" id="usageCard">
      <div class="card-title">Трафик по нодам</div>
      ${skeleton(3)}
    </div>
    <div class="card" id="subCard">
      <div class="card-title">Подписка</div>
      <div class="btn-group">
        <button class="btn btn-primary" id="copyBtn" onclick="copySubLink()">📋 Скопировать ссылку подписки</button>
        <div class="collapsible-header" onclick="toggleInstructions()">
          <h3>❓ Как подключиться?</h3>
          <span class="chevron" id="chevron">▼</span>
        </div>
        <div class="collapsible-body" id="instructions">
          <div class="app-list">
            <div class="app-item">
              <div class="app-name">Hiddify (Android / iOS / Windows / Mac)</div>
              <div class="app-steps">1. Установите Hiddify<br>2. Нажмите «+» → «Добавить из буфера»<br>3. Вставьте скопированную ссылку подписки</div>
            </div>
            <div class="app-item">
              <div class="app-name">Streisand (iOS)</div>
              <div class="app-steps">1. Установите Streisand<br>2. Нажмите «+» → «Импорт из URL»<br>3. Вставьте ссылку подписки</div>
            </div>
            <div class="app-item">
              <div class="app-name">v2rayNG (Android)</div>
              <div class="app-steps">1. Установите v2rayNG<br>2. Меню → «Подписки» → «Группы»<br>3. Добавьте ссылку подписки</div>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div class="card" id="devicesCard">
      <div class="card-title">Мои устройства</div>
      ${skeleton(3)}
    </div>`;

  // Load in parallel
  const [infoResult, usageResult, devicesResult] = await Promise.allSettled([
    apiFetch('info'),
    apiFetch('usage'),
    apiFetch('devices'),
  ]);

  renderStatusCard(infoResult, token);
  renderUsageCard(usageResult);
  renderDevicesCard(devicesResult);
}

function renderStatusCard(result, token) {
  const card = document.getElementById('statusCard');
  if (!card) return;

  if (result.status === 'rejected') {
    card.innerHTML = `<div class="card-title">Статус аккаунта</div><div class="empty-state">${result.reason.message}</div>`;
    return;
  }

  const d = result.value;
  const [label, cls] = statusLabel(d.status);
  const isExpiredOrDisabled = d.status === 'expired' || d.status === 'disabled' || d.status === 'limited';
  const usedPct = d.data_limit ? Math.min(100, Math.round(d.used_traffic / d.data_limit * 100)) : 0;
  const trafficLabel = d.data_limit
    ? `${formatBytes(d.used_traffic)} / ${formatBytes(d.data_limit)}`
    : `${formatBytes(d.used_traffic)} / ∞`;

  card.innerHTML = `
    <div class="card-title">Статус аккаунта</div>
    ${isExpiredOrDisabled ? `<div class="alert" style="margin-bottom:14px">⚠️ Ваша подписка истекла или отключена. Обратитесь к администратору.</div>` : ''}
    <div class="user-row">
      <span class="username">👤 ${escHtml(d.username)}</span>
      <span class="badge ${cls}">${label}</span>
    </div>
    <div class="info-row">
      <span class="info-label">📅 Истекает через</span>
      <span class="info-value">${formatExpire(d.expire)}</span>
    </div>
    <div class="info-row" style="border-bottom:none">
      <span class="info-label">📦 Трафик</span>
      <span class="info-value">${trafficLabel}</span>
    </div>
    ${d.data_limit ? `<div class="progress-wrap">
      <div class="progress-bar"><div class="progress-fill" style="width:${usedPct}%"></div></div>
    </div>` : ''}`;

  // Store subscription URL for copy button
  window._subUrl = d.subscription_url;
}

function renderUsageCard(result) {
  const card = document.getElementById('usageCard');
  if (!card) return;

  if (result.status === 'rejected') {
    card.innerHTML = `<div class="card-title">Трафик по нодам</div><div class="empty-state">${result.reason.message}</div>`;
    return;
  }

  const usages = result.value.usages || [];
  if (!usages.length) {
    card.innerHTML = `<div class="card-title">Трафик по нодам</div><div class="empty-state">Нет данных</div>`;
    return;
  }

  const rows = usages.map(u => `
    <div class="node-row">
      <div class="node-top">
        <span class="node-name">${escHtml(u.node_name)}</span>
        <span class="node-traffic">${formatBytes(u.used_traffic)} &nbsp;${u.percent}%</span>
      </div>
      <div class="node-bar"><div class="node-bar-fill" style="width:${u.percent}%"></div></div>
    </div>`).join('');

  card.innerHTML = `<div class="card-title">Трафик по нодам</div>${rows}`;
}

function deviceLabel(d) {
  const name = d.display_name || d.device_name || d.client_name || 'Устройство';
  const parts = [];
  if (d.platform) parts.push(d.platform);
  if (d.client_name && d.client_name !== name) parts.push(d.client_name);
  return { name, meta: parts.join(' · ') };
}

function renderDevicesCard(result) {
  const card = document.getElementById('devicesCard');
  if (!card) return;

  if (result.status === 'rejected') {
    card.innerHTML = `<div class="card-title">Мои устройства</div><div class="empty-state">${result.reason.message}</div>`;
    return;
  }

  const devices = result.value.devices || [];
  const limit = result.value.limit || 3;
  const activeCount = result.value.active_count || 0;
  const pct = Math.min(100, Math.round(activeCount / limit * 100));

  const counter = `
    <div class="device-counter">
      <span class="device-counter-label">Активные устройства</span>
      <span class="device-counter-val">${activeCount} / ${limit === 0 ? '∞' : limit}</span>
    </div>
    <div class="device-slots-bar"><div class="device-slots-fill" style="width:${pct}%"></div></div>`;

  if (!devices.length) {
    card.innerHTML = `<div class="card-title">Мои устройства</div>${counter}<div class="empty-state">Нет зарегистрированных устройств</div>`;
    return;
  }

  const rows = devices.map(d => {
    const { name, meta } = deviceLabel(d);
    const active = d.is_active;
    const badge = active
      ? `<span class="badge-device-active">● Активно</span>`
      : `<span class="badge-device-inactive">○ Откл.</span>`;
    const actions = active ? `
      <div class="device-item-actions">
        <button class="btn-icon" title="Переименовать" onclick="renameDevice(${d.id}, '${escHtml(name)}')">✏️</button>
        <button class="btn-icon btn-icon-danger" title="Отключить" onclick="deleteDevice(${d.id})">✕</button>
      </div>` : '';
    return `
      <div class="device-item" id="dev-${d.id}">
        <div class="device-item-top">
          <span class="device-item-name${active ? '' : ' inactive'}">${escHtml(name)}</span>
          ${badge}
        </div>
        <div class="device-item-meta">
          <span>${escHtml(meta || '—')} · ${formatRelTime(d.last_seen)}</span>
          ${actions}
        </div>
      </div>`;
  }).join('');

  card.innerHTML = `<div class="card-title">Мои устройства</div>${counter}${rows}`;
}

async function deleteDevice(id) {
  if (!confirm('Отключить это устройство?')) return;
  try {
    await apiDelete(`devices/${id}`);
    const el = document.getElementById(`dev-${id}`);
    if (el) {
      el.querySelector('.badge-device-active').outerHTML = '<span class="badge-device-inactive">○ Откл.</span>';
      const actions = el.querySelector('.device-item-actions');
      if (actions) actions.remove();
      el.querySelector('.device-item-name')?.classList.add('inactive');
    }
    // Refresh counter
    apiFetch('devices').then(data => {
      const counter = document.querySelector('.device-counter-val');
      if (counter) counter.textContent = `${data.active_count} / ${data.limit}`;
      const fill = document.querySelector('.device-slots-fill');
      if (fill) fill.style.width = `${Math.min(100, Math.round(data.active_count / data.limit * 100))}%`;
    }).catch(() => {});
  } catch(e) {
    alert('Ошибка: ' + e.message);
  }
}

async function renameDevice(id, currentName) {
  const newName = prompt('Новое название устройства:', currentName);
  if (!newName || newName.trim() === currentName) return;
  try {
    await apiPatch(`devices/${id}`, { name: newName.trim() });
    const el = document.getElementById(`dev-${id}`);
    if (el) {
      const nameEl = el.querySelector('.device-item-name');
      if (nameEl) nameEl.textContent = newName.trim();
    }
  } catch(e) {
    alert('Ошибка: ' + e.message);
  }
}

function escHtml(s) {
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function copySubLink() {
  const btn = document.getElementById('copyBtn');
  const url = window._subUrl;
  if (!url || !btn) return;
  try {
    await navigator.clipboard.writeText(url);
    btn.textContent = '✅ Скопировано!';
    btn.classList.add('btn-copied');
    setTimeout(() => {
      btn.textContent = '📋 Скопировать ссылку подписки';
      btn.classList.remove('btn-copied');
    }, 2000);
  } catch {
    // Fallback
    const ta = document.createElement('textarea');
    ta.value = url;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    btn.textContent = '✅ Скопировано!';
    setTimeout(() => { btn.textContent = '📋 Скопировать ссылку подписки'; }, 2000);
  }
}

function toggleInstructions() {
  const body = document.getElementById('instructions');
  const chev = document.getElementById('chevron');
  if (!body) return;
  body.classList.toggle('open');
  chev?.classList.toggle('open');
}

// Init
(function init() {
  const token = getToken();
  if (!token) {
    renderTokenForm();
  } else {
    renderDashboard(token);
  }
})();
