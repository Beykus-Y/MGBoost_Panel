// Reusable modal factory for the account-centric operational actions.
// Deliberately NOT wired into admin.js's global data-action delegation: every
// button created here gets its own direct listener, so the global handler
// never sees modal-internal targets. Rendering stays inside the existing
// SafeMarkup discipline (html`` fragments passed in by callers).

export function createModals({html, renderHtml}){
  function openModal({title, body, footer}){
    const overlay=document.createElement('div');
    overlay.className='modal-overlay ops-modal-overlay';
    overlay.innerHTML='<div class="modal ops-modal"><div class="modal-header"><h3></h3><button class="ops-close" aria-label="Закрыть">×</button></div><div class="modal-body"></div><div class="modal-footer"></div></div>';
    overlay.querySelector('h3').textContent=title||'';
    document.body.appendChild(overlay);
    requestAnimationFrame(()=>overlay.classList.add('open'));
    const bodyEl=overlay.querySelector('.modal-body');
    const footerEl=overlay.querySelector('.modal-footer');
    if(body)renderHtml(bodyEl,body);
    if(footer)renderHtml(footerEl,footer);
    let closed=false;
    const escListener=event=>{if(event.key==='Escape')api.close();};
    const api={el:overlay,
      setBody(markup){renderHtml(bodyEl,markup);},
      close(){if(closed)return;closed=true;overlay.remove();document.removeEventListener('keydown',escListener);}};
    overlay.addEventListener('click',event=>{if(event.target===overlay)api.close();});
    overlay.querySelector('.ops-close').addEventListener('click',()=>api.close());
    document.addEventListener('keydown',escListener);
    return api;
  }

  // Two-step consequence dialog: shows an explicit consequences block and
  // enables the confirm button only after the admin ticks the acknowledgement.
  function confirmFlow({title, body, confirmLabel='Подтвердить', busyLabel='Выполняю…', requireChecked=true, onConfirm}){
    const modal=openModal({title, body});
    const bar=document.createElement('div');
    bar.className='ops-confirm-bar';
    if(requireChecked){
      bar.appendChild(Object.assign(document.createElement('label'),{className:'ops-checkline'}));
      renderHtml(bar.lastChild,html`<input type="checkbox" id="ops-final-check"/> <span>Я понимаю последствия и подтверждаю действие</span>`);
    }
    const confirmBtn=document.createElement('button');
    confirmBtn.className='danger';
    renderHtml(confirmBtn,html`${confirmLabel}`);
    confirmBtn.disabled=requireChecked;
    if(requireChecked){
      bar.querySelector('#ops-final-check').addEventListener('change',e=>{confirmBtn.disabled=!e.target.checked;});
    }else{
      confirmBtn.disabled=false;
    }
    confirmBtn.addEventListener('click',async()=>{
      confirmBtn.disabled=true;
      renderHtml(confirmBtn,html`${busyLabel}`);
      let outcome;
      try{outcome=await onConfirm(modal);}
      catch(error){renderHtml(confirmBtn,html`${confirmLabel}`);confirmBtn.disabled=false;throw error;}
      if(outcome===false){
        // Validation abort handled by onConfirm itself (e.g. an inline
        // notice); stay open, restore the button.
        renderHtml(confirmBtn,html`${confirmLabel}`);
        confirmBtn.disabled=requireChecked&&!modal.el.querySelector('#ops-final-check')?.checked;
        return;
      }
      if(!modal.el.isConnected)modal.close();
    });
    bar.appendChild(confirmBtn);
    modal.el.querySelector('.modal-footer').appendChild(bar);
    return modal;
  }

  return {openModal,confirmFlow};
}
