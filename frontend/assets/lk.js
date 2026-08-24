const app = document.getElementById('app');

// Accept old ?token= bookmarks once, then remove the bearer from the visible
// URL before any API request or subresource load. New manual entries use a
// fragment, which is never sent to nginx. The token remains memory-only.
const initialUrl = new URL(location.href);
const fragmentParams = new URLSearchParams(
  initialUrl.hash.startsWith('#') ? initialUrl.hash.slice(1) : initialUrl.hash
);
const legacySubscriptionToken =
  initialUrl.searchParams.get('token') || fragmentParams.get('token') || '';
if (legacySubscriptionToken) {
  initialUrl.searchParams.delete('token');
  fragmentParams.delete('token');
  const remainingFragment = fragmentParams.toString();
  const cleanTarget = initialUrl.pathname +
    (initialUrl.searchParams.toString() ? `?${initialUrl.searchParams}` : '') +
    (remainingFragment ? `#${remainingFragment}` : '');
  history.replaceState(null, '', cleanTarget);
}

function getToken() {
  return legacySubscriptionToken;
}

function subscriptionHeaders(extra = {}) {
  const token = getToken();
  return token ? { ...extra, 'X-MGBoost-Subscription': token } : extra;
}

function formatBytes(bytes) {
  bytes = Number(bytes);
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) { bytes /= 1024; i++; }
  return bytes.toFixed(i >= 2 ? 2 : 0) + ' ' + units[i];
}

function formatExpire(ts) {
  ts = Number(ts);
  if (!Number.isFinite(ts) || ts <= 0) return 'Бессрочно';
  const d = new Date(ts * 1000);
  const now = Date.now();
  const diff = d - now;
  const days = Math.ceil(diff / 86400000);
  const dateStr = d.toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
  if (diff <= 0) return `Истёк ${dateStr}`;
  return `${days} дн. (${dateStr})`;
}

function formatRelTime(ts) {
  ts = Number(ts);
  if (!Number.isFinite(ts) || ts <= 0) return '—';
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

function createNode(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  if (options.id) element.id = options.id;
  if (options.className) element.className = options.className;
  if (Object.prototype.hasOwnProperty.call(options, 'text')) {
    element.textContent = String(options.text ?? '');
  }
  if (options.type) element.type = options.type;
  if (options.title) element.title = options.title;
  for (const child of children) {
    if (child) element.appendChild(child);
  }
  return element;
}

function replaceContent(parent, ...children) {
  parent.replaceChildren(...children.filter(Boolean));
}

function cardTitle(text) {
  return createNode('div', { className: 'card-title', text });
}

function skeletons(count = 3) {
  return Array.from({ length: count }, () => createNode('div', { className: 'skeleton' }));
}

function safeNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function boundedPercent(value) {
  return Math.max(0, Math.min(100, safeNumber(value)));
}

function safeErrorMessage(reason) {
  return typeof reason?.message === 'string' && reason.message
    ? reason.message
    : 'Не удалось загрузить данные';
}

function emptyStateCard(card, title, message) {
  replaceContent(
    card,
    cardTitle(title),
    createNode('div', { className: 'empty-state', text: message }),
  );
}

// Reasons the backend returns for a missing/invalid management session —
// these mutating calls no longer work with just the subscription token.
const MGMT_SESSION_REASONS = new Set([
  'management_session_required',
  'insufficient_scope',
  'session_username_mismatch',
]);
const MGMT_SESSION_MESSAGE =
  'Нужна ссылка для управления устройствами. Откройте бота и нажмите «🔧 Управление устройствами», ' +
  'чтобы получить новую (действует 15 минут).';

class ApiError extends Error {
  constructor(message, reason) {
    super(message);
    this.reason = reason;
  }
}

async function _throwApiError(res) {
  const err = await res.json().catch(() => ({}));
  if (err.reason && MGMT_SESSION_REASONS.has(err.reason)) {
    throw new ApiError(MGMT_SESSION_MESSAGE, err.reason);
  }
  throw new ApiError(err.error || `HTTP ${res.status}`, err.reason);
}

async function apiFetch(path) {
  // credentials: 'include' so that when there's no token (pure
  // management-session flow, reached via the bot's mgmt deep link) the
  // HttpOnly management-session cookie rides along and the backend can
  // resolve the username from the session instead. Harmless when a token
  // is present too — the backend always prefers the token in that case.
  const res = await fetch(`/lk/api/${path}`, {
    credentials: 'include',
    headers: subscriptionHeaders(),
  });
  if (!res.ok) await _throwApiError(res);
  return res.json();
}

async function apiDelete(path) {
  // credentials: 'include' so the HttpOnly management-session cookie
  // (set by exchangeMgmtCode()) rides along — mutating actions are
  // authorized by that cookie server-side, not by the token in the URL.
  // When there's no token at all, the cookie is also how the backend
  // resolves which username this request is for.
  const res = await fetch(`/lk/api/${path}`, {
    method: 'DELETE',
    credentials: 'include',
    headers: subscriptionHeaders(),
  });
  if (!res.ok) await _throwApiError(res);
  return res.json();
}

async function apiPatch(path, data) {
  const res = await fetch(`/lk/api/${path}`, {
    method: 'PATCH',
    headers: subscriptionHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!res.ok) await _throwApiError(res);
  return res.json();
}

// Exchange a one-time `mgmt` code (from a bot-issued deep link) for a
// management session cookie. The code arrives in the URL *fragment*
// (`#mgmt=<code>`), never a query string: the browser never transmits the
// fragment part of a URL to the server, so it can't end up in reverse-proxy
// access logs, browser history sync, or a Referer header. We read it
// client-side, POST it to the exchange endpoint's body (never the URL),
// and immediately clear the fragment via history.replaceState — before any
// dashboard data is rendered — so a reload/share/back-navigation of the
// resulting URL can't re-expose or attempt to reuse a spent code.
async function exchangeMgmtCode() {
  const hash = location.hash.startsWith('#') ? location.hash.slice(1) : location.hash;
  const params = new URLSearchParams(hash);
  const code = params.get('mgmt');

  // Clear the fragment immediately, before doing anything else (including
  // the network request) — the one-time code must not linger in the
  // visible URL a moment longer than necessary.
  if (code) {
    history.replaceState(null, '', location.pathname + location.search);
  }

  if (!code) return;

  try {
    const res = await fetch('/lk/api/mgmt/exchange', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ code }),
    });
    if (res.ok) {
      window._hasMgmtSession = true;
    }
  } catch {
    // Network error — treated the same as "no session"; mutating actions
    // will surface the standard MGMT_SESSION_MESSAGE if attempted.
  }
}

function renderTokenForm() {
  const input = createNode('input', { id: 'tokenInput', className: 'token-input', type: 'text' });
  input.placeholder = 'Вставьте токен из ссылки подписки';
  input.autocomplete = 'off';

  const button = createNode('button', {
    className: 'btn btn-primary', type: 'button', text: 'Открыть кабинет',
  });
  button.addEventListener('click', openWithToken);
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') openWithToken();
  });

  const form = createNode('div', { className: 'token-form' }, [
    createNode('p', { text: 'Введите ваш токен подписки, чтобы открыть личный кабинет.' }),
    input,
    button,
  ]);
  replaceContent(app, createNode('div', { className: 'card' }, [cardTitle('Вход'), form]));
}

function openWithToken() {
  const val = document.getElementById('tokenInput')?.value.trim();
  if (!val) return;
  // Handle full URL or bare token
  let token = val;
  try {
    const u = new URL(val);
    token = u.searchParams.get('token') ||
      new URLSearchParams(u.hash.slice(1)).get('token') ||
      (u.pathname.includes('/sub/') ? u.pathname.split('/sub/')[1].split('/')[0] : val);
  } catch {}
  location.href = `/lk/#token=${encodeURIComponent(token)}`;
}

function instructionItem(name, steps) {
  const stepElement = createNode('div', { className: 'app-steps' });
  steps.forEach((step, index) => {
    if (index) stepElement.appendChild(document.createElement('br'));
    stepElement.appendChild(document.createTextNode(step));
  });
  return createNode('div', { className: 'app-item' }, [
    createNode('div', { className: 'app-name', text: name }),
    stepElement,
  ]);
}

function subscriptionCard() {
  const copyButton = createNode('button', {
    id: 'copyBtn', className: 'btn btn-primary', type: 'button',
    text: '📋 Скопировать ссылку подписки',
  });
  copyButton.addEventListener('click', copySubLink);

  const chevron = createNode('span', { id: 'chevron', className: 'chevron', text: '▼' });
  const header = createNode('div', { className: 'collapsible-header' }, [
    createNode('h3', { text: '❓ Как подключиться?' }),
    chevron,
  ]);
  header.tabIndex = 0;
  header.setAttribute('role', 'button');
  header.setAttribute('aria-expanded', 'false');
  header.setAttribute('aria-controls', 'instructions');
  header.addEventListener('click', toggleInstructions);
  header.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggleInstructions();
    }
  });

  const instructions = createNode('div', { id: 'instructions', className: 'collapsible-body' }, [
    createNode('div', { className: 'app-list' }, [
      instructionItem('Hiddify (Android / iOS / Windows / Mac)', [
        '1. Установите Hiddify',
        '2. Нажмите «+» → «Добавить из буфера»',
        '3. Вставьте скопированную ссылку подписки',
      ]),
      instructionItem('Streisand (iOS)', [
        '1. Установите Streisand',
        '2. Нажмите «+» → «Импорт из URL»',
        '3. Вставьте ссылку подписки',
      ]),
      instructionItem('v2rayNG (Android)', [
        '1. Установите v2rayNG',
        '2. Меню → «Подписки» → «Группы»',
        '3. Добавьте ссылку подписки',
      ]),
    ]),
  ]);

  return createNode('div', { id: 'subCard', className: 'card' }, [
    cardTitle('Подписка'),
    createNode('div', { className: 'btn-group' }, [copyButton, header, instructions]),
  ]);
}

async function renderDashboard(_token) {
  const statusCard = createNode('div', { id: 'statusCard', className: 'card' });
  const usageCard = createNode('div', { id: 'usageCard', className: 'card' });
  const devicesCard = createNode('div', { id: 'devicesCard', className: 'card' });
  replaceContent(statusCard, cardTitle('Статус аккаунта'), ...skeletons(4));
  replaceContent(usageCard, cardTitle('Трафик по нодам'), ...skeletons(3));
  replaceContent(devicesCard, cardTitle('Мои устройства'), ...skeletons(3));
  replaceContent(app, statusCard, usageCard, subscriptionCard(), devicesCard);

  // Load in parallel
  const [infoResult, usageResult, devicesResult] = await Promise.allSettled([
    apiFetch('info'),
    apiFetch('usage'),
    apiFetch('devices'),
  ]);

  renderStatusCard(infoResult);
  renderUsageCard(usageResult);
  renderDevicesCard(devicesResult);
}

function renderStatusCard(result) {
  const card = document.getElementById('statusCard');
  if (!card) return;

  if (result.status === 'rejected') {
    emptyStateCard(card, 'Статус аккаунта', safeErrorMessage(result.reason));
    return;
  }

  const d = result.value || {};
  const [label, cls] = statusLabel(d.status);
  const isExpiredOrDisabled = d.status === 'expired' || d.status === 'disabled' || d.status === 'limited';
  const dataLimit = Math.max(0, safeNumber(d.data_limit));
  const usedTraffic = Math.max(0, safeNumber(d.used_traffic));
  const usedPct = dataLimit ? boundedPercent(Math.round(usedTraffic / dataLimit * 100)) : 0;
  const trafficLabel = dataLimit
    ? `${formatBytes(d.used_traffic)} / ${formatBytes(d.data_limit)}`
    : `${formatBytes(d.used_traffic)} / ∞`;

  const children = [cardTitle('Статус аккаунта')];
  if (isExpiredOrDisabled) {
    const alert = createNode('div', {
      className: 'alert',
      text: '⚠️ Ваша подписка истекла или отключена. Обратитесь к администратору.',
    });
    alert.style.marginBottom = '14px';
    children.push(alert);
  }
  children.push(createNode('div', { className: 'user-row' }, [
    createNode('span', { className: 'username', text: `👤 ${String(d.username || '')}` }),
    createNode('span', { className: `badge ${cls}`, text: label }),
  ]));
  children.push(createNode('div', { className: 'info-row' }, [
    createNode('span', { className: 'info-label', text: '📅 Истекает через' }),
    createNode('span', { className: 'info-value', text: formatExpire(d.expire) }),
  ]));
  const trafficRow = createNode('div', { className: 'info-row' }, [
    createNode('span', { className: 'info-label', text: '📦 Трафик' }),
    createNode('span', { className: 'info-value', text: trafficLabel }),
  ]);
  trafficRow.style.borderBottom = 'none';
  children.push(trafficRow);
  if (dataLimit) {
    const fill = createNode('div', { className: 'progress-fill' });
    fill.style.width = `${usedPct}%`;
    children.push(createNode('div', { className: 'progress-wrap' }, [
      createNode('div', { className: 'progress-bar' }, [fill]),
    ]));
  }
  replaceContent(card, ...children);

  // Store subscription URL for copy button
  window._subUrl = d.subscription_url;
}

function renderUsageCard(result) {
  const card = document.getElementById('usageCard');
  if (!card) return;

  if (result.status === 'rejected') {
    emptyStateCard(card, 'Трафик по нодам', safeErrorMessage(result.reason));
    return;
  }

  const usages = Array.isArray(result.value?.usages) ? result.value.usages : [];
  if (!usages.length) {
    emptyStateCard(card, 'Трафик по нодам', 'Нет данных');
    return;
  }

  const rows = usages.map(u => {
    const percent = boundedPercent(u?.percent);
    const fill = createNode('div', { className: 'node-bar-fill' });
    fill.style.width = `${percent}%`;
    return createNode('div', { className: 'node-row' }, [
      createNode('div', { className: 'node-top' }, [
        createNode('span', { className: 'node-name', text: String(u?.node_name || '') }),
        createNode('span', {
          className: 'node-traffic',
          text: `${formatBytes(u?.used_traffic)}  ${percent}%`,
        }),
      ]),
      createNode('div', { className: 'node-bar' }, [fill]),
    ]);
  });
  replaceContent(card, cardTitle('Трафик по нодам'), ...rows);
}

function deviceLabel(d) {
  d = d || {};
  const name = String(d.display_name || d.device_name || d.client_name || 'Устройство');
  const parts = [];
  if (d.platform) parts.push(String(d.platform));
  if (d.client_name && d.client_name !== name) parts.push(String(d.client_name));
  return { name, meta: parts.join(' · ') };
}

function normalizeDeviceId(value) {
  const id = Number(value);
  return Number.isSafeInteger(id) && id > 0 ? String(id) : null;
}

function deviceCounter(activeCount, limit) {
  const fill = createNode('div', { className: 'device-slots-fill' });
  const percent = limit > 0 ? boundedPercent(Math.round(activeCount / limit * 100)) : 0;
  fill.style.width = `${percent}%`;
  return [
    createNode('div', { className: 'device-counter' }, [
      createNode('span', { className: 'device-counter-label', text: 'Активные устройства' }),
      createNode('span', {
        className: 'device-counter-val',
        text: `${activeCount} / ${limit === 0 ? '∞' : limit}`,
      }),
    ]),
    createNode('div', { className: 'device-slots-bar' }, [fill]),
  ];
}

function renderDevicesCard(result) {
  const card = document.getElementById('devicesCard');
  if (!card) return;

  if (result.status === 'rejected') {
    emptyStateCard(card, 'Мои устройства', safeErrorMessage(result.reason));
    return;
  }

  const devices = Array.isArray(result.value?.devices) ? result.value.devices : [];
  const limit = Math.max(0, Math.trunc(safeNumber(result.value?.limit, 3)));
  const activeCount = Math.max(0, Math.trunc(safeNumber(result.value?.active_count)));
  const counter = deviceCounter(activeCount, limit);

  if (!devices.length) {
    replaceContent(
      card,
      cardTitle('Мои устройства'),
      ...counter,
      createNode('div', { className: 'empty-state', text: 'Нет зарегистрированных устройств' }),
    );
    return;
  }

  const rows = devices.map(rawDevice => {
    const d = rawDevice || {};
    const { name, meta } = deviceLabel(d);
    const active = Boolean(d.is_active);
    const deviceId = normalizeDeviceId(d.id);
    const item = createNode('div', { className: 'device-item' });
    if (deviceId) {
      item.id = `dev-${deviceId}`;
      item.dataset.deviceId = deviceId;
    }
    const badge = createNode('span', {
      className: active ? 'badge-device-active' : 'badge-device-inactive',
      text: active ? '● Активно' : '○ Откл.',
    });
    const nameElement = createNode('span', {
      className: `device-item-name${active ? '' : ' inactive'}`,
      text: name,
    });
    item.appendChild(createNode('div', { className: 'device-item-top' }, [nameElement, badge]));

    const metaChildren = [
      createNode('span', { text: `${meta || '—'} · ${formatRelTime(d.last_seen)}` }),
    ];
    if (active && deviceId) {
      const renameButton = createNode('button', {
        className: 'btn-icon', type: 'button', title: 'Переименовать', text: '✏️',
      });
      renameButton.dataset.action = 'rename-device';
      renameButton.dataset.deviceId = deviceId;
      renameButton.addEventListener('click', event => {
        const id = normalizeDeviceId(event.currentTarget.dataset.deviceId);
        if (id) renameDevice(id, name);
      });

      const deleteButton = createNode('button', {
        className: 'btn-icon btn-icon-danger', type: 'button', title: 'Отключить', text: '✕',
      });
      deleteButton.dataset.action = 'delete-device';
      deleteButton.dataset.deviceId = deviceId;
      deleteButton.addEventListener('click', event => {
        const id = normalizeDeviceId(event.currentTarget.dataset.deviceId);
        if (id) deleteDevice(id);
      });
      metaChildren.push(createNode('div', { className: 'device-item-actions' }, [
        renameButton,
        deleteButton,
      ]));
    }
    item.appendChild(createNode('div', { className: 'device-item-meta' }, metaChildren));
    return item;
  });

  replaceContent(card, cardTitle('Мои устройства'), ...counter, ...rows);
}

async function deleteDevice(id) {
  const deviceId = normalizeDeviceId(id);
  if (!deviceId) return;
  if (!confirm('Отключить это устройство?')) return;
  try {
    await apiDelete(`devices/${deviceId}`);
    const el = document.getElementById(`dev-${deviceId}`);
    if (el) {
      const activeBadge = el.querySelector('.badge-device-active');
      if (activeBadge) {
        activeBadge.replaceWith(createNode('span', {
          className: 'badge-device-inactive', text: '○ Откл.',
        }));
      }
      const actions = el.querySelector('.device-item-actions');
      if (actions) actions.remove();
      el.querySelector('.device-item-name')?.classList.add('inactive');
    }
    // Refresh counter
    apiFetch('devices').then(data => {
      const counter = document.querySelector('.device-counter-val');
      const refreshedLimit = Math.max(0, Math.trunc(safeNumber(data.limit, 3)));
      const refreshedActive = Math.max(0, Math.trunc(safeNumber(data.active_count)));
      if (counter) counter.textContent = `${refreshedActive} / ${refreshedLimit === 0 ? '∞' : refreshedLimit}`;
      const fill = document.querySelector('.device-slots-fill');
      if (fill) {
        const percent = refreshedLimit > 0
          ? boundedPercent(Math.round(refreshedActive / refreshedLimit * 100))
          : 0;
        fill.style.width = `${percent}%`;
      }
    }).catch(() => {});
  } catch(e) {
    alert('Ошибка: ' + e.message);
  }
}

async function renameDevice(id, currentName) {
  const deviceId = normalizeDeviceId(id);
  if (!deviceId) return;
  const newName = prompt('Новое название устройства:', currentName);
  if (!newName || newName.trim() === currentName) return;
  try {
    await apiPatch(`devices/${deviceId}`, { name: newName.trim() });
    const el = document.getElementById(`dev-${deviceId}`);
    if (el) {
      const nameEl = el.querySelector('.device-item-name');
      if (nameEl) nameEl.textContent = newName.trim();
    }
  } catch(e) {
    alert('Ошибка: ' + e.message);
  }
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
  const header = body.previousElementSibling;
  header?.setAttribute('aria-expanded', body.classList.contains('open') ? 'true' : 'false');
}

// Init
(async function init() {
  await exchangeMgmtCode();
  const token = getToken();
  if (!token && !window._hasMgmtSession) {
    renderTokenForm();
  } else {
    // Either a normal ?token= link, or a management deep-link whose mgmt
    // code was just exchanged for a session cookie — renderDashboard's
    // apiFetch calls resolve the username from whichever is present
    // (token takes priority; the session cookie is the fallback).
    renderDashboard(token);
  }
})();
