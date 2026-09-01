// PH7-16 Wave 5 -- Tickets screen, ported out of admin.js verbatim.
// Owner-corrected placement: top-level Support, not Technical -- this is
// real day-to-day support tooling, not Marzban-internals debt. Fully
// self-contained: no state shared with any other screen.
export function createTicketsUi({html,renderHtml,closeModal,proxyApi}){
  let _currentTicketId=null;
  const _TICKET_STATUS_LABELS={open:'Открыт',waiting_human:'Ждёт оператора',new_user:'Новый польз.',closed:'Закрыт'};
  // Closed status->semantic mapping (redesign plan section 19.2):
  // waiting_human -> warning (needs operator attention), new_user -> info
  // (normal expected onboarding state, not a problem), open -> neutral,
  // closed -> success. No status maps to `error` -- a ticket by itself is
  // never in an error condition.
  const _TICKET_STATUS_BADGE={open:'badge-gray',waiting_human:'badge-amber',new_user:'badge-blue',closed:'badge-green'};

  function _tsAgo(ts){
    const diff=Math.floor(Date.now()/1000)-ts;
    if(diff<60)return'только что';
    if(diff<3600)return`${Math.floor(diff/60)} мин назад`;
    if(diff<86400)return`${Math.floor(diff/3600)} ч назад`;
    return`${Math.floor(diff/86400)} дн назад`;
  }
  function _fmtDate(ts){return new Date(ts*1000).toLocaleString('ru',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});}

  async function loadTickets(status){
    const tbody=document.getElementById('tickets-tbody');
    renderHtml(tbody,html`<tr><td colspan="6"><div class="loading"><span class="spinner"></span></div></td></tr>`);
    try{
      const qs=status?`?status=${status}`:'';
      const r=await proxyApi('/admin/tickets'+qs);
      const tickets=await r.json();
      if(!tickets.length){renderHtml(tbody,html`<tr><td colspan="6" class="empty-state">Тикетов нет</td></tr>`);return;}
      renderHtml(tbody,html`${tickets.map(t=>html`
        <tr>
          <td>#${t.id}</td>
          <td><span class="badge ${_TICKET_STATUS_BADGE[t.status]||'badge-gray'}">${_TICKET_STATUS_LABELS[t.status]||t.status}</span></td>
          <td>${t.marzban_username||`tg:${t.telegram_id}`}</td>
          <td class="ticket-col-updated">${_tsAgo(t.updated_at)}</td>
          <td class="ticket-col-created">${_fmtDate(t.created_at)}</td>
          <td><button data-action="open-ticket" data-ticket-id="${t.id}">Открыть</button></td>
        </tr>`)}`);
    }catch(e){renderHtml(tbody,html`<tr><td colspan="6" class="error-state">Ошибка загрузки</td></tr>`);}
  }

  async function openTicket(id){
    _currentTicketId=id;
    const r=await proxyApi(`/admin/tickets/${id}`);
    if(!r.ok)return;
    const {ticket,messages}=await r.json();
    document.getElementById('ticket-modal-title').textContent=
      `Тикет #${id} — ${ticket.marzban_username||`tg:${ticket.telegram_id}`} [${_TICKET_STATUS_LABELS[ticket.status]||ticket.status}]`;
    const chat=document.getElementById('ticket-chat');
    renderHtml(chat,messages.length?html`${messages.map(m=>{
      const roleClass=m.role==='user'?'ticket-message--user':m.role==='ai'?'ticket-message--ai':'ticket-message--operator';
      const label=m.role==='user'?'Пользователь':m.role==='ai'?'AI':'Оператор';
      return html`<div class="ticket-message ${roleClass}">
        <div class="ticket-message-meta">${label} · ${_fmtDate(m.ts)}</div>
        <div class="ticket-message-text">${m.text}</div>
      </div>`;
    })}`:html`<div class="ticket-empty-chat">Сообщений нет</div>`);
    chat.scrollTop=chat.scrollHeight;
    document.getElementById('ticket-reply-text').value='';
    document.getElementById('ticket-action-status').textContent='';
    document.getElementById('ticket-modal').classList.add('open');
  }

  async function sendTicketReply(){
    if(!_currentTicketId)return;
    const text=document.getElementById('ticket-reply-text').value.trim();
    if(!text)return;
    const status=document.getElementById('ticket-action-status');
    status.textContent='Отправка...';
    const r=await proxyApi(`/admin/tickets/${_currentTicketId}/reply`,{method:'POST',body:JSON.stringify({text})});
    if(!r.ok){status.textContent='Ошибка';return;}
    status.classList.add('ticket-reply-status--ok');status.textContent='Отправлено';
    setTimeout(()=>{status.textContent='';status.classList.remove('ticket-reply-status--ok');},2000);
    document.getElementById('ticket-reply-text').value='';
    await openTicket(_currentTicketId);
  }

  async function closeTicket(){
    if(!_currentTicketId)return;
    const status=document.getElementById('ticket-action-status');
    status.textContent='Закрываю...';
    const r=await proxyApi(`/admin/tickets/${_currentTicketId}/close`,{method:'POST'});
    if(!r.ok){status.textContent='Ошибка';return;}
    closeModal('ticket-modal');
    loadTickets(document.getElementById('ticket-filter').value||undefined);
  }

  return {loadTickets,openTicket,sendTicketReply,closeTicket};
}
