// PH7-10 manual external-payment UI over the deployed PH5-09/10 backend.
// The frontend never sends an authoritative price/plan/duration/account: the
// product list comes from the server catalog endpoint, the preview comes from
// the server preview endpoint, and the create call re-validates everything
// server-side. Double submits converge through the store's durable
// idempotency key minted once per wizard run.

const METHOD_PRESETS=['bank_transfer','sbp','cash','card_transfer','other'];

export function createPayments({html,renderHtml,toast,openModal,confirmFlow,formatTimestamp,formatRub,humanLabel,badge,badgeClass}){
  let catalogPromise=null;

  function ensureCatalog(adminFetch){
    if(!catalogPromise){
      catalogPromise=adminFetch('/admin/manual-payment-catalog')
        .then(response=>response.json().then(data=>{if(!response.ok)throw new Error(data.error||'catalog failed');return data;}))
        .catch(error=>{catalogPromise=null;throw error;});
    }
    return catalogPromise;
  }

  const NOT_PURCHASABLE_TEXT={
    PLAN_SWITCH_REQUIRES_PH5_06:'Смена тарифа недоступна: upgrade/downgrade (PH5-06) ещё не реализован. Доступно только продление текущего тарифа.',
    ADMIN_GRANTED_UNLIMITED_NOT_EXTENDABLE:'Подписка выдана админом как UNLIMITED и не продлевается платежом.',
    CURRENT_PLAN_NOT_WL:'WL-пакеты доступны только аккаунтам на реальном WL-тарифе.',
    PACKAGE_INELIGIBLE:'Пакеты сейчас не могут быть применены к этому аккаунту.',
    NO_ACTIVE_SUBSCRIPTION:'Нет действующей подписки — продление недоступно (выдача подписки не входит в ручные платежи).',
  };

  function productTitle(product){
    return product.kind==='PLAN'
      ?`${humanLabel(product.plan_code)} · ${product.duration_days} дн.`
      :humanLabel(product.sku);
  }

  async function openNewPayment(ctx){
    const {account}=ctx;
    let catalog;
    try{catalog=await ensureCatalog(ctx.adminFetch);}
    catch(error){toast('Не удалось загрузить RUB-каталог','err');return;}
    const products=[
      ...catalog.plans.map(plan=>({...plan,kind:'PLAN',purchasable:true})),
      ...catalog.packages.map(sku=>({...sku,kind:'PACKAGE',purchasable:true})),
    ];
    let modal=openModal({title:`Новый внешний платёж · ${account.id}`});
    let selected=null,previewData=null,idempotencyKey=null;

    function renderProducts(){
      renderHtml(modal.el.querySelector('.modal-body'),html`
        <div class="cell-sub">Канал: RUB · версия каталога <code>${catalog.catalog_version}</code>. Цена и тариф фиксируются сервером.</div>
        <div class="product-grid">${products.map((product,index)=>html`
          <button class="product-card ${selected===index?'active':''}" data-ops-product="${index}">
            <strong>${productTitle(product)}</strong>
            <span>${formatRub(product.amount_minor)}</span>
            ${product.kind==='PLAN'?html`<small>${humanLabel(product.wl_mode)}${product.wl_quota_gb?` · ${product.wl_quota_gb} ГБ/${product.period_days||30}дн.`:''}</small>`
              :html`<small>+${Math.round(product.bytes/1e9)} ГБ к WL</small>`}
          </button>`)}
        </div>
        <div id="ops-preview-slot"></div>
        <div id="ops-create-slot"></div>`);
      modal.el.querySelectorAll('[data-ops-product]').forEach(btn=>{
        btn.addEventListener('click',()=>selectProduct(Number(btn.dataset.opsProduct)));
      });
    }

    async function selectProduct(index){
      selected=index;previewData=null;
      modal.el.querySelectorAll('.product-card').forEach(card=>card.classList.toggle('active',Number(card.dataset.opsProduct)===index));
      const slot=modal.el.querySelector('#ops-preview-slot');
      const createSlot=modal.el.querySelector('#ops-create-slot');
      renderHtml(slot,html`<div class="cell-sub">Загрузка предпросмотра…</div>`);
      renderHtml(createSlot,html``);
      const product=products[index];
      const payload=product.kind==='PLAN'
        ?{plan_code:product.plan_code,duration_days:product.duration_days}
        :{package_sku:product.sku};
      try{
        const response=await ctx.adminFetch(`/admin/accounts/${account.id}/manual-payments/preview`,{method:'POST',body:JSON.stringify(payload)});
        const data=await response.json();
        if(!response.ok)throw new Error(data.error||'preview failed');
        previewData=data;
        renderPreview(slot);
      }catch(error){renderHtml(slot,html`<div class="notice notice-amber">Предпросмотр недоступен: ${error.message}</div>`);}
    }

    function renderPreview(slot){
      const p=previewData;
      if(!p.purchasable){
        renderHtml(slot,html`<div class="notice notice-amber"><strong>Оплата этого продукта невозможна.</strong><div>${NOT_PURCHASABLE_TEXT[p.not_purchasable_reason]||'Сервер запретил этот продукт для аккаунта.'}</div></div>`);
        return;
      }
      const current=p.current_expiry?formatTimestamp(p.current_expiry):'—';
      renderHtml(slot,html`<div class="card ops-preview">
        <h4>Предпросмотр (сервер)</h4>
        <dl class="ops-dl">
          <dt>Аккаунт</dt><dd>#${account.id}</dd>
          <dt>Текущий тариф</dt><dd>${p.current_display_name||'—'} · до ${current}</dd>
          <dt>Продукт</dt><dd>${p.product_kind==='PLAN'?`${p.display_name} · ${p.duration_days} дн.`:p.display_name}</dd>
          <dt>Цена (сертифицировано каталогом)</dt><dd><strong>${formatRub(p.amount_minor)}</strong></dd>
          ${p.product_kind==='PLAN'&&p.expected_new_expiry?html`
            <dt>Ожидаемый новый срок</dt><dd>${formatTimestamp(p.expected_new_expiry)} <span class="cell-sub">(оценка по формуле DL-044)</span></dd>
            <dt>WL-эффект</dt><dd>${p.wl_effect.mode==='LIMITED'?`${p.wl_effect.quota_gb_per_period} ГБ / ${p.wl_effect.period_days} дн.`:'без изменений'}</dd>`:''}
          <dt>Способ оплаты</dt><dd>свободный ввод, попадёт в audit</dd>
          <dt>External reference</dt><dd>уникальный номер перевода, резервируется навсегда (DL-054)</dd>
        </dl>
        <div class="notice notice-amber">Применённый платёж иммутабелен: после apply исправления не существует (только будущая компенсация).</div>
      </div>`);
      renderCreateBar();
    }

    function renderCreateBar(){
      const createSlot=modal.el.querySelector('#ops-create-slot');
      const methodOptions=METHOD_PRESETS.map(m=>html`<option value="${m}">${m}</option>`);
      renderHtml(createSlot,html`<div class="card ops-form">
        <label>Записанная сумма, ₽ ( должна быть равна цене )<input type="number" id="ops-amount" min="1" step="1" value="${previewData.amount_minor}"/></label>
        <label>Способ оплаты<input list="ops-method-list" id="ops-method" maxlength="100"/><datalist id="ops-method-list">${methodOptions}</datalist></label>
        <label>External reference *<input type="text" id="ops-ref" maxlength="200"/></label>
        <label>Комментарий<textarea id="ops-comment" maxlength="500"></textarea></label>
        <div class="ops-actions"><button class="primary" id="ops-create">Создать pending-запись</button>
        <span class="cell-sub">После создания запись можно исправить или отменить до apply.</span></div>
      </div>`);
      createSlot.querySelector('#ops-create').addEventListener('click',async event=>{
        const button=event.currentTarget;
        if(!idempotencyKey)idempotencyKey=`adm-ui-${crypto.randomUUID()}`;
        button.disabled=true;
        try{
          const amount=Number(createSlot.querySelector('#ops-amount').value);
          if(!Number.isInteger(amount)||amount!==previewData.amount_minor){toast('Сумма должна точно совпадать с ценой продукта','err');return;}
          const body={...previewData.product_kind==='PLAN'?{plan_code:previewData.plan_code,duration_days:previewData.duration_days}:{package_sku:previewData.package_sku},
            recorded_amount_minor:amount,
            payment_method:createSlot.querySelector('#ops-method').value.trim(),
            external_reference:createSlot.querySelector('#ops-ref').value.trim(),
            comment:createSlot.querySelector('#ops-comment').value.trim()||null,
            idempotency_key:idempotencyKey};
          if(!body.payment_method){toast('Укажите способ оплаты','err');return;}
          if(!body.external_reference){toast('Укажите external reference','err');return;}
          const response=await ctx.adminFetch(`/admin/accounts/${account.id}/manual-payments`,{method:'POST',body:JSON.stringify(body)});
          const data=await response.json();
          if(!response.ok)throw new Error(data.error||'create failed');
          toast('Pending-платёж создан');
          ctx.reload&&ctx.reload();
          openRecordModal({...data.payment},ctx,{fresh:true});
          modal.close();
        }catch(error){toast(error.message,'err');}
        finally{button.disabled=false;}
      });
    }

    renderProducts();
  }

  // PH7-16 Wave 3: shared by the account-level entry point (Payments tab,
  // openLegacyTransition below) and the Operations -> Legacy Transitions
  // queue (openLegacyTransitionById) -- "same components, two list
  // contexts" per the plan, not two independent renderers.
  function _openTransitionModal(ctx,{title}){
    const modal=openModal({title});
    const request=async(path,body={})=>{
      const response=await ctx.adminFetch(path,{method:'POST',body:JSON.stringify(body)});
      const data=await response.json();if(!response.ok)throw new Error(data.error||'transition request failed');
      return data.transition;
    };
    const renderTransition=transition=>{
      const selectable=(transition.devices||[]).filter(device=>!device.selected);
      renderHtml(modal.el.querySelector('.modal-body'),html`
        <dl class="ops-dl">
          <dt>Состояние</dt><dd>${badge(transition.state)}</dd>
          <dt>Переход</dt><dd>${humanLabel(transition.source_plan_code)} → ${humanLabel(transition.target_plan_code)}</dd>
          <dt>Оплата</dt><dd>${formatRub(transition.expected_amount_minor)} · ${transition.duration_days} дн.</dd>
          <dt>Исходный срок</dt><dd>${formatTimestamp(transition.original_source_expiry)}</dd>
          <dt>Активация</dt><dd>${formatTimestamp(transition.activation_at)}</dd>
          <dt>Новый срок</dt><dd>${formatTimestamp(transition.target_expiry)}</dd>
          <dt>Устройства</dt><dd>${transition.active_device_count} / ${transition.target_device_limit}</dd>
        </dl>
        ${transition.state==='PENDING_PAYMENT'?html`<div class="notice notice-amber">Confirm payment фиксирует реальную оплату и одноразовый alignment grace. После этого отмена и правка запрещены.</div><button class="primary" id="lct-confirm">Подтвердить реальную оплату</button><button class="danger" id="lct-cancel">Отменить до подтверждения</button>`:''}
        ${transition.state==='SELECTION_REQUIRED'?html`<div class="notice notice-amber">Явно выберите ровно ${transition.capacity_excess} устройств. До activation_at они продолжат работать.</div>
          <div class="ops-form">${selectable.map(device=>html`<label><input type="checkbox" data-lct-device value="${device.slot_generation_id}"/> Слот ${device.slot_number} · generation ${device.generation}</label>`)}
          <label>Причина выбора<input id="lct-selection-reason" maxlength="300" value="capacity transition selection"/></label><button class="primary" id="lct-select">Записать выбор</button></div>`:''}
        ${transition.state==='MANUAL_REVIEW'?html`<div class="notice notice-amber">${transition.review_reason||'Требуется ручная проверка'}. Retry не меняет frozen facts и не выдаёт новый grace.</div><button id="lct-retry">Повторить после устранения причины</button>`:''}
        <details><summary>Audit/history</summary>${(transition.events||[]).map(event=>html`<div class="list-row"><div><strong>${humanLabel(event.event_type)}</strong><div class="cell-sub">${formatTimestamp(event.created_at)} · ${event.reason}</div></div></div>`)}</details>`);
      modal.el.querySelector('#lct-confirm')?.addEventListener('click',async()=>renderTransition(await request(`/admin/legacy-transitions/${transition.id}/confirm`)));
      modal.el.querySelector('#lct-cancel')?.addEventListener('click',async()=>{
        const prompted=window.prompt('Причина отмены (8..300 символов)');
        if(prompted===null)return;
        const reason=prompted.trim();
        if(reason.length<8||reason.length>300){toast('Причина должна содержать 8–300 символов','err');return;}
        renderTransition(await request(`/admin/legacy-transitions/${transition.id}/cancel`,{reason}));
      });
      modal.el.querySelector('#lct-select')?.addEventListener('click',async()=>{
        const ids=[...modal.el.querySelectorAll('[data-lct-device]:checked')].map(node=>Number(node.value));
        const reason=modal.el.querySelector('#lct-selection-reason').value.trim();
        renderTransition(await request(`/admin/legacy-transitions/${transition.id}/select-devices`,{slot_generation_ids:ids,reason}));
      });
      modal.el.querySelector('#lct-retry')?.addEventListener('click',async()=>{
        const prompted=window.prompt('Что проверено/исправлено (8..300 символов)');
        if(prompted===null)return;
        const reason=prompted.trim();
        if(reason.length<8||reason.length>300){toast('Причина должна содержать 8–300 символов','err');return;}
        renderTransition(await request(`/admin/legacy-transitions/${transition.id}/retry-review`,{reason}));
      });
    };
    return {modal,request,renderTransition};
  }

  async function openLegacyTransitionById(transitionId,ctx){
    const {modal,renderTransition}=_openTransitionModal(ctx,{title:'Переход с архивного тарифа'});
    renderHtml(modal.el.querySelector('.modal-body'),html`<div class="cell-sub">Загрузка…</div>`);
    const transition=await ctx.adminFetch(`/admin/legacy-transitions/${transitionId}`)
      .then(async response=>{const data=await response.json();if(!response.ok)throw new Error(data.error||'transition fetch failed');return data.transition;});
    renderTransition(transition);
  }

  async function openLegacyTransition(ctx){
    const catalog=await ensureCatalog(ctx.adminFetch);
    const current=await ctx.adminFetch(`/admin/accounts/${ctx.account.id}/legacy-transition`)
      .then(async response=>{const data=await response.json();if(!response.ok)throw new Error(data.error||'transition fetch failed');return data.transition;});
    const {modal,request,renderTransition}=_openTransitionModal(ctx,{title:'Перевести с архивного тарифа'});
    if(current){renderTransition(current);return;}
    const plans=catalog.plans.filter(item=>['BASIC','BASIC_PLUS','BASIC_PRO','WL','EXTENDED','FAMILY'].includes(item.plan_code));
    renderHtml(modal.el.querySelector('.modal-body'),html`<div class="cell-sub">Текущий архивный тариф работает до UTC-hour activation boundary. Цена проверяется серверным RUB-каталогом.</div>
      <div class="ops-form"><label>Commercial product<select id="lct-product">${plans.map((plan,index)=>html`<option value="${index}">${plan.display_name} · ${plan.duration_days} дн. · ${formatRub(plan.amount_minor)}</option>`)}</select></label>
      <label>Способ оплаты<input id="lct-method" maxlength="100"/></label><label>External reference<input id="lct-ref" maxlength="200"/></label>
      <label>Evidence/comment<textarea id="lct-comment" maxlength="500"></textarea></label><label>Audit reason<input id="lct-reason" maxlength="300" value="real paid legacy commercial transition"/></label>
      <button class="primary" id="lct-create">Создать payment/transition</button></div>`);
    modal.el.querySelector('#lct-create').addEventListener('click',async event=>{
      event.currentTarget.disabled=true;
      try{
        const plan=plans[Number(modal.el.querySelector('#lct-product').value)];
        const body={plan_code:plan.plan_code,duration_days:plan.duration_days,recorded_amount_minor:plan.amount_minor,
          payment_method:modal.el.querySelector('#lct-method').value.trim(),external_reference:modal.el.querySelector('#lct-ref').value.trim(),
          comment:modal.el.querySelector('#lct-comment').value.trim()||null,idempotency_key:`adm-lct-${crypto.randomUUID()}`};
        if(!body.payment_method||!body.external_reference)throw new Error('Заполните method и external reference');
        const paymentResponse=await ctx.adminFetch(`/admin/accounts/${ctx.account.id}/manual-payments`,{method:'POST',body:JSON.stringify(body)});
        const paymentData=await paymentResponse.json();if(!paymentResponse.ok)throw new Error(paymentData.error||'payment create failed');
        const transition=await request(`/admin/manual-payments/${paymentData.payment.id}/legacy-transition`,{reason:modal.el.querySelector('#lct-reason').value.trim()});
        renderTransition(transition);ctx.reload&&ctx.reload();
      }finally{event.currentTarget.disabled=false;}
    });
  }

  // --- record management ---------------------------------------------------

  function recordBody(record,syncState){
    return html`<dl class="ops-dl">
      <dt>Статус</dt><dd>${badge(record.status)}</dd>
      <dt>Продукт</dt><dd>${record.kind==='PLAN_PRODUCT'?`${humanLabel(record.plan_code)} · ${record.duration_days} дн.`:humanLabel(record.package_sku)}</dd>
      <dt>Сумма</dt><dd>${formatRub(record.amount_minor)}</dd>
      <dt>Method / reference</dt><dd>${record.payment_method} · <code>${record.external_reference}</code></dd>
      ${record.comment?html`<dt>Комментарий</dt><dd>${record.comment}</dd>`:''}
      <dt>Создан</dt><dd>${formatTimestamp(record.created_at)}</dd>
      ${record.applied_expiry?html`<dt>Applied expiry</dt><dd>${formatTimestamp(record.applied_expiry)}</dd>`:''}
      ${syncState?html`<dt>Child-sync</dt><dd>${badge(syncState.state)}</dd>`:''}
    </dl>`;
  }

  async function loadRecord(ctx,paymentId){
    const response=await ctx.adminFetch(`/admin/manual-payments/${paymentId}`);
    const data=await response.json();
    if(!response.ok)throw new Error(data.error||'fetch failed');
    return data;
  }

  function openRecordModal(record,ctx,fresh){
    const modalPromise=fresh?Promise.resolve({edits:[],application:null,sync_state:null}):loadRecord(ctx,record.id);
    modalPromise.then(detail=>{
      const rec=detail.payment||record;
      const syncState=detail.sync_state?detail.sync_state:(rec.sync_state?{state:rec.sync_state}:null);
      let actionsBlock=html``;
      if(rec.status==='PENDING'){
        actionsBlock=html`<div class="ops-actions">
          <button class="danger" data-rec="cancel">Отменить…</button>
          <button data-rec="edit">Исправить…</button>
          <button class="primary" data-rec="apply">Применить (apply)…</button></div>`;
      }else if(rec.status==='MANUAL_REVIEW'){
        actionsBlock=html`<div class="notice notice-amber">${rec.review_reason?('Причина: '+rec.review_reason):''}</div>
          <div class="ops-actions"><button data-rec="resolve">Вернуть в ожидание (resolve review)…</button></div>`;
      }
      const modal=openModal({title:`Ручной платёж ${rec.public_id}`,body:html`
        ${recordBody(rec,syncState)}
        ${detail.edits&&detail.edits.length?html`<details><summary>История правок (${detail.edits.length})</summary>
          ${detail.edits.map(edit=>html`<div class="list-row"><div><strong>${humanLabel(edit.edit_kind)}</strong>
          <div class="cell-sub">${formatTimestamp(edit.created_at)} · ${edit.reason||''}</div></div></div>`)}</details>`:''}`,
        footer:actionsBlock});
      modal.el.querySelectorAll('[data-rec]').forEach(button=>{
        button.addEventListener('click',async()=>{
          const action=button.dataset.rec;
          try{
            if(action==='apply')return await applyDialog(ctx,rec);
            if(action==='cancel')return await cancelDialog(ctx,rec);
            if(action==='edit')return await editDialog(ctx,rec);
            if(action==='resolve')return await resolveDialog(ctx,rec);
          }catch(error){toast(error.message,'err');}
        });
      });
    }).catch(error=>toast(error.message,'err'));
  }

  async function applyDialog(ctx,rec){
    const freshDetail=await loadRecord(ctx,rec.id);
    confirmFlow({title:'Apply: необратимое применение',
      body:html`${recordBody(freshDetail.payment,freshDetail.sync_state)}
        <div class="notice notice-amber"><strong>После apply запись иммутабельна.</strong>
        Срок продлевается по формуле max(текущий, now) + длительность; детям будет отправлена синхронизация.
        Другой тариф был бы направлен в MANUAL_REVIEW (PH5-06 ещё не реализован).</div>`,
      confirmLabel:'Применить платёж',
      onConfirm:async m=>{
        const response=await ctx.adminFetch(`/admin/manual-payments/${rec.id}/apply`,{method:'POST',body:'{}'});
        const data=await response.json().catch(()=>({}));
        if(!response.ok)throw new Error(data.error||'apply failed');
        const pay=data.payment||{};
        m.setBody(html`${recordBody(pay,data.sync_state?{state:data.sync_state}:null)}
          ${data.renewal_before_expiry?html`<dl class="ops-dl"><dt>Старый срок</dt><dd>${formatTimestamp(data.renewal_before_expiry)}</dd>
            <dt>Новый срок</dt><dd>${formatTimestamp(data.renewal_after_expiry)}</dd>
            <dt>Entitlement</dt><dd>${data.entitlement_summary?`${humanLabel(data.entitlement_summary.effective_status)} · до ${formatTimestamp(data.entitlement_summary.effective_expiry)}`:'—'}</dd>
            <dt>Child-sync</dt><dd>${humanLabel(data.sync_state)}</dd></dl>`:''}
          ${data.grant?html`<div class="notice notice-success">Начислен пакет: +${Math.round((data.grant.granted_bytes||0)/1e9)} ГБ</div>`:''}`);
        const footer=m.el.querySelector('.modal-footer');
        footer.innerHTML='';
        const ok=document.createElement('button');ok.className='primary';ok.textContent='Готово';
        ok.addEventListener('click',()=>{m.close();ctx.reload&&ctx.reload();});
        footer.appendChild(ok);
        ctx.reload&&ctx.reload();
      }});
  }

  async function cancelDialog(ctx,rec){
    const modal=openModal({title:`Отмена ${rec.public_id}`,body:html`
      <div class="cell-sub">Reference <code>${rec.external_reference}</code> останется зарезервированным навсегда (DL-054).</div>
      <div class="ops-form"><label>Причина отмены (обязательно)<input type="text" id="ops-cancel-reason" maxlength="300"/></label></div>`});
    modal.el.querySelector('.modal-footer').appendChild(makeSubmitRow('Отменить платёж',async()=>{
      const reason=modal.el.querySelector('#ops-cancel-reason').value.trim();
      if(reason.length<3){toast('Причина минимум 3 символа','err');return;}
      const response=await ctx.adminFetch(`/admin/manual-payments/${rec.id}/cancel`,{method:'POST',body:JSON.stringify({reason})});
      const data=await response.json();if(!response.ok)throw new Error(data.error||'cancel failed');
      toast('Платёж отменён');modal.close();ctx.reload&&ctx.reload();
    }));
  }

  async function resolveDialog(ctx,rec){
    const modal=openModal({title:'Вернуть запись в ожидание',body:html`
      <div class="ops-form"><label>Note решения (обязательно)<input type="text" id="ops-resolve-note" maxlength="300"/></label></div>`});
    modal.el.querySelector('.modal-footer').appendChild(makeSubmitRow('Вернуть в PENDING',async()=>{
      const note=modal.el.querySelector('#ops-resolve-note').value.trim();
      if(note.length<3){toast('Note минимум 3 символа','err');return;}
      const response=await ctx.adminFetch(`/admin/manual-payments/${rec.id}/resolve-review`,{method:'POST',body:JSON.stringify({resolution_note:note})});
      const data=await response.json();if(!response.ok)throw new Error(data.error||'resolve failed');
      toast('Возвращено в PENDING');modal.close();ctx.reload&&ctx.reload();
    }));
  }

  async function editDialog(ctx,rec){
    const freshDetail=await loadRecord(ctx,rec.id);
    const cur=freshDetail.payment;
    const modal=openModal({title:`Исправление ${cur.public_id} (pending)`,body:html`
      <div class="cell-sub">Изменяемые поля повторно валидируются по каталогу; каждая правка пишется в append-only историю before/after (DL-039).</div>
      <div class="ops-form grid-two">
        <label>Длительность<select id="ops-e-days"><option value="">—</option>${[30,60].map(d=>html`<option value="${d}" ${cur.duration_days===d?'selected':''}>${d} дн.</option>`)}</select></label>
        <label>Сумма ₽ (ровно цена нового SKU)<input type="number" id="ops-e-amount" min="1" step="1" value="${cur.amount_minor}"/></label>
        <label>Способ оплаты<input id="ops-e-method" maxlength="100" value="${cur.payment_method}"/></label>
        <label>External reference<input id="ops-e-ref" maxlength="200" value="${cur.external_reference}"/></label>
        <label class="wide">Комментарий<textarea id="ops-e-comment" maxlength="500">${cur.comment||''}</textarea></label>
        <label class="wide">Причина правки (минимум 8 символов)<input type="text" id="ops-e-reason" maxlength="1000"/></label>
      </div>`});
    modal.el.querySelector('.modal-footer').appendChild(makeSubmitRow('Сохранить правку',async()=>{
      const changes={};
      const days=modal.el.querySelector('#ops-e-days').value;
      const amount=Number(modal.el.querySelector('#ops-e-amount').value);
      const method=modal.el.querySelector('#ops-e-method').value.trim();
      const ref=modal.el.querySelector('#ops-e-ref').value.trim();
      const comment=modal.el.querySelector('#ops-e-comment').value.trim();
      const reason=modal.el.querySelector('#ops-e-reason').value.trim();
      if(!reason||reason.length<8){toast('Причина минимум 8 символов','err');return;}
      if(days&&Number(days)!==cur.duration_days){changes.duration_days=Number(days);}
      if(amount!==cur.amount_minor)changes.recorded_amount_minor=amount;
      if(method&&method!==cur.payment_method)changes.payment_method=method;
      if(ref&&ref!==cur.external_reference)changes.external_reference=ref;
      if(comment!==(cur.comment||''))changes.comment=comment||null;
      if(!Object.keys(changes).length){toast('Нет изменений','err');return;}
      const response=await ctx.adminFetch(`/admin/manual-payments/${rec.id}/edit`,{method:'POST',body:JSON.stringify({reason,changes})});
      const data=await response.json();if(!response.ok)throw new Error(data.error||'edit failed');
      toast('Правка сохранена');modal.close();ctx.reload&&ctx.reload();
    }));
  }

  function makeSubmitRow(label,onSubmit){
    const row=document.createElement('div');row.className='ops-submit-row';
    const btn=document.createElement('button');btn.className='primary';
    btn.textContent=label;
    btn.addEventListener('click',async()=>{btn.disabled=true;try{await onSubmit(btn);}catch(e){toast(e.message,'err');}finally{btn.disabled=false;}});
    row.appendChild(btn);return row;
  }

  function paymentsTab(detail,ctx){
    const records=detail.manual_payments||[];
    const legacy=(detail.payment_records||[]);
    return html`<div class="card">
      <div class="list-row"><div><div class="card-title">Ручные внешние платежи (RUB)</div>
      <span class="cell-sub">Server-authoritative: цены только из versioned fixed RUB-каталога; произвольная цена/тариф отклоняются сервером.</span></div>
      <div class="ops-actions">${detail.subscription?.plan_code?.startsWith('LEGACY_PAID_COMPAT_V1_')?html`<button data-action="legacy-commercial-transition">Перевести с архивного тарифа</button>`:''}<button class="primary" data-action="new-manual-payment" data-account-id="${detail.account.id}">Новый внешний платёж</button></div></div>
      ${records.length?records.map(rec=>html`<div class="list-row clickable" data-action="open-manual-payment" data-payment-id="${rec.id}" data-account-id="${detail.account.id}">
        <div><strong>${rec.public_id}</strong><div class="cell-sub">${rec.kind==='PLAN_PRODUCT'?`${humanLabel(rec.plan_code)} · ${rec.duration_days} дн.`:humanLabel(rec.package_sku)} · ${rec.payment_method} · ref <code>${rec.external_reference}</code></div></div>
        <div class="pay-status">${formatRub(rec.amount_minor)} ${badge(rec.status)}${rec.sync_state&&rec.sync_state!=='SYNCED'&&rec.status==='APPLIED'?html`<span class="badge badge-amber">sync: ${humanLabel(rec.sync_state)}</span>`:''}</div>
      </div>`):html`<div class="empty-state">Ручных платежей ещё не было</div>`}
    </div>
    <div class="card spaced-card"><div class="card-title">Канонические записи о платежах (provenance)</div>
      ${legacy.length?legacy.map(row=>html`<div class="list-row"><div><strong>${row.public_id}</strong>${row.payment_channel==='ADMIN_GRANT'?html` <span class="badge badge-purple">ADMIN GRANT</span>`:''}
      <div class="cell-sub">${humanLabel(row.payment_channel)} · ${row.record_status}${row.actor_ref?` · actor ${row.actor_ref}`:''}</div></div>
      <span>${row.currency&&row.amount_minor!=null?formatRub(row.amount_minor,row.currency):''}</span></div>`):html`<div class="empty-state">Записей нет</div>`}
    </div>
    ${(detail.legacy_stars_invoices||[]).length?html`<div class="card spaced-card"><div class="card-title">Legacy Stars invoices</div>
      <span class="cell-sub">Исторические Stars-инвойсы управляются на экране «Stars»; здесь read-only сводка.</span>
      ${detail.legacy_stars_invoices.map(inv=>html`<div class="list-row"><div><strong>#${inv.id}</strong>
      <div class="cell-sub">${inv.tariff_name} · ${inv.stars_price} ⭐ · ${inv.status}</div></div></div>`)}</div>`:html``}`;
  }

  return {ensureCatalog,openNewPayment,openLegacyTransition,openLegacyTransitionById,openRecordModal,paymentsTab};
}
