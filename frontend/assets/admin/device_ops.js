// Wave B device lifecycle actions (PH7-05): Revoke / Free / Rebind as four
// distinct operations per DL-049, plus the reversible pause (Disable/Enable)
// over the DeviceSlotAdminStore primitive — the pause dialog exists because
// owner instruction supersedes ADMIN-UX-02's lighter no-reason UX: every
// administrative mutation here keeps preview + mandatory reason + explicit
// confirmation + immutable audit server-side.

export function createDeviceOps({html,toast,confirmFlow,formatTimestamp}){
  const CONSEQUENCES={
    disable:{
      title:'Пауза устройства (Disable)',
      confirm:'Приостановить устройство',
      consequences:['Обратимая пауза текущей generation: слот остаётся занят, capacity не освобождается.',
        'Устройству уйдёт состояние «disabled» через ревизию PH3-08; UUID/история/HWID не затрагиваются.',
        'Продление/смена срока подписки НЕ вернёт это устройство само — только явный Enable.'],
    },
    enable:{
      title:'Возобновление устройства (Enable)',
      confirm:'Возобновить устройство',
      consequences:['Возвращает ТОТ ЖЕ child/generation/UUID в сервис с сохранением истории.',
        'Финальное состояние определяется подпиской аккаунта: истёкшая подписка держит устройство отключённым независимо от паузы.',
        'Не смешивается с Revoke/Free/Rebind: терминальные операции остаются отдельными.'],
      light:true,
    },
    revoke:{
      title:'Отзыв устройства (Revoke)',
      confirm:'Отозвать устройство',
      consequences:['Удалённый child текущего поколения будет отозван через типовой broker.',
        'Старый UUID/credential никогда не воскресает; операция терминальна для этой generation.',
        'Slot остаётся занятым до отдельной операции «Освободить».'],
    },
    free:{
      title:'Освобождение слота (Free)',
      confirm:'Освободить слот',
      consequences:['Возможно только ПОСЛЕ применённого Revoke (сервер проверит порядок жёстко).',
        'После освобождения новый клиентский сможет занять этот slot органически.'],
    },
    rebind:{
      title:'Перепривязка (Rebind: замена/компрометация)',
      confirm:'Перепривязать на новое устройство',
      consequences:['Текущая generation отзывается ТЕРМИНАЛЬНО; её UUID не воскресает.',
        'На этом же slot создаётся новая generation под указанный HWID нового устройства; provisioning ребёнка пойдёт по существующему PH3-03 конвейеру; successor стартует активным (пауза не наследуется).',
        'Самая опасная из операций — проверьте HWID у клиента дважды.'],
      needsHwid:true,
    },
  };

  function openDeviceAction({kind,slot,label},ctx){
    const spec=CONSEQUENCES[kind];
    if(!spec)return;
    const hwidField=spec.needsHwid?html`<label>HWID нового устройства
      <input type="text" id="ops-hwid" maxlength="128" placeholder="как его сообщает клиент"/>
      <span class="cell-sub">Именно он станет владельцем новой generation.</span></label>`:'';
    const modal=confirmFlow({
      title:`${spec.title} · Слот ${slot}${label?` (${label})`:''}`,
      body:html`<ul class="ops-consequences">${spec.consequences.map(item=>html`<li>${item}</li>`)}
        </ul><div class="ops-form">${hwidField}
        <label>Причина (обязательно, попадёт в immutable audit)
          <input type="text" id="ops-reason" maxlength="300"/></label>
        ${kind==='rebind'?html`<label class="ops-checkline"><input type="checkbox" id="ops-compromise-check"/>
          <span>Подтверждаю: suspected compromise / реальная замена устройства</span></label>`:''}
      </div>`,
      confirmLabel:spec.confirm,
      onConfirm:async m=>{
        const reason=m.el.querySelector('#ops-reason').value.trim();
        if(reason.length<3){toast('Причина минимум 3 символа','err');return false;}
        const body={reason,confirm:true};
        if(spec.needsHwid){
          const hwid=m.el.querySelector('#ops-hwid').value.trim();
          if(hwid.length<6){toast('HWID минимум 6 символов','err');return false;}
          body.new_device_hwid=hwid;
          if(!m.el.querySelector('#ops-compromise-check').checked){toast('Подтвердите характер замены','err');return false;}
        }
        const response=await ctx.adminFetch(`/admin/accounts/${ctx.accountId}/devices/${slot}/${kind}`,
          {method:'POST',body:JSON.stringify(body)});
        let data={};
        try{data=await response.json();}catch(_){}
        if(response.status===202){
          m.setBody(html`<div class="notice notice-amber">Удалённый шаг не завершился сразу; операция сохранена durably и будет повторена. Текущее состояние: <strong>${data.state||data.aggregate_state||'PENDING'}</strong>${kind==='disable'||kind==='enable'?html`<div class="cell-sub">Повторная сходимость доступна кнопкой «Sync» на этом устройстве.</div>`:''}</div>`);
          ctx.reload&&ctx.reload();
          return;
        }
        if(!response.ok)throw new Error(data.error||'operation failed');
        toast(kind==='revoke'?'Устройство отозвано':kind==='free'?'Слот освобожден'
          :kind==='rebind'?'Rebind выполнен':kind==='disable'?'Устройство приостановлено':'Устройство возобновлено');
        m.close();
        ctx.reload&&ctx.reload();
      }});
    // Consequence dialogs must require BOTH reason and the checked ack; the
    // shared confirmFlow already gates the button on the checkbox.
    return modal;
  }

  async function openDeviceSync(slot,ctx){
    const response=await ctx.adminFetch(`/admin/accounts/${ctx.accountId}/devices/${slot}/sync`,
      {method:'POST',body:'{}'});
    const data=await response.json().catch(()=>({}));
    if(!response.ok){toast(data.error||'sync failed','err');return;}
    if(data.aligned)toast('Дети сошлись (IN_SYNC)');
    else toast(`Состояние синхронизации: ${data.aggregate_state||'?'}`,'warn');
    ctx.reload&&ctx.reload();
  }

  function deviceActionButtons(device,ctx){
    const actions=device.actions||{};
    const buttons=[];
    if(actions.disable==='available')buttons.push(html`<button class="small" data-device-op="disable" data-slot="${device.slot_number}">Приостановить…</button>`);
    else if(actions.disable==='done')buttons.push(html`<span class="badge badge-red">На паузе</span>`);
    if(actions.enable==='available')buttons.push(html`<button class="primary small" data-device-op="enable" data-slot="${device.slot_number}">Возобновить…</button>`);
    if(actions.revoke==='available')buttons.push(html`<button class="danger small" data-device-op="revoke" data-slot="${device.slot_number}">Отозвать…</button>`);
    else if(actions.revoke==='done')buttons.push(html`<span class="badge badge-red">Отозван</span>`);
    if(actions.free==='available')buttons.push(html`<button class="small" data-device-op="free" data-slot="${device.slot_number}">Освободить…</button>`);
    else if(String(actions.free||'').startsWith('PENDING'))buttons.push(html`<span class="badge badge-amber">Free: ${actions.free.slice(8)}</span>`);
    if(actions.rebind==='available')buttons.push(html`<button class="small" data-device-op="rebind" data-slot="${device.slot_number}">Rebind…</button>`);
    else if(actions.rebind==='done')buttons.push(html`<span class="badge badge-gray">Rebind применён</span>`);
    else if(String(actions.rebind||'').startsWith('PENDING'))buttons.push(html`<span class="badge badge-amber">Rebind: ${actions.rebind.slice(8)}</span>`);
    const mismatch=device.desired_state&&device.observed_state&&device.desired_state!==device.observed_state;
    if(mismatch&&ctx)buttons.push(html`<button class="small" data-device-sync="${device.slot_number}" title="Повторить детскую сходимость по durable-операциям">Sync…</button>`);
    if(!buttons.length)return html``;
    return html`<div class="device-actions">${buttons}</div>
      ${actions.last_error_class?html`<div class="cell-sub">Последняя ошибка операции: <code>${actions.last_error_class}</code></div>`:''}`;
  }

  function ownershipTab(detail,ctx){
    const identities=detail.telegram.identities||[];
    const activeOwner=identities.find(identity=>identity.role==='OWNER'&&identity.revoked_at===null&&identity.status!=='REVOKED')
      ||identities.find(identity=>!identity.revoked_at);
    return html`
    <div class="card"><div class="list-row"><div><div class="card-title">Владелец Telegram</div>
      <div class="cell-sub">Ownership rebind — отдельная операция от device rebind. HWID/URL не доказывают владение (OPD-39/DL-041).</div></div></div>
      ${identities.length?identities.map(identity=>html`<div class="list-row">
        <div><strong>ID ${identity.telegram_id}</strong>
        <div class="cell-sub">привязан ${formatTimestamp(identity.linked_at)}${identity.revoked_at?` · отозван ${formatTimestamp(identity.revoked_at)}`:''}</div></div>
        ${identity.revoked_at?html`<span class="badge badge-gray">Отозван</span>`:html`<span class="badge badge-green">Активен</span>`}</div>`)
        :html`<div class="empty-state">Владелец ещё не привязан</div>`}
    </div>
    <div class="card spaced-card" id="ownership-rebind-card">
      <div class="card-title">Rebind владельца Telegram (только primary admin)</div>
      <div class="ops-form grid-two">
        <label>Текущий owner ID<input type="number" id="ops-tg-old" value="${activeOwner?activeOwner.telegram_id:''}" readonly/></label>
        <label>Новый Telegram ID<input type="number" id="ops-tg-new" placeholder="положительное число"/></label>
        <label class="wide">Режим
          <select id="ops-tg-mode">
            <option value="ORDINARY">Обычный rebind (token/UUID не меняются)</option>
            <option value="COMPROMISE">Компрометация (opaque credential ротируется немедленно)</option>
          </select></label>
        <label class="wide">Причина (3–300)<input type="text" id="ops-tg-reason" maxlength="300"/></label>
      </div>
      <div class="notice notice-amber hidden-when-ordinary" data-show-for="COMPROMISE">При COMPROMISE старый subscription URL перестаёт работать сразу; новый выдаётся через «Подписка → Выпустить» после завершения.</div>
      <div class="ops-actions"><button class="primary" data-action="tg-rebind" data-account-id="${detail.account.id}">Начать rebind…</button></div>
    </div>`;
  }

  return {openDeviceAction,openDeviceSync,deviceActionButtons,ownershipTab};
}
