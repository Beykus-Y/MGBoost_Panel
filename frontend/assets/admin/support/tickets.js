// PH7-16 Wave 5 -- Tickets screen, ported out of admin.js verbatim.
// Owner-corrected placement: top-level Support, not Technical -- this is
// real day-to-day support tooling, not Marzban-internals debt. Fully
// self-contained: no state shared with any other screen.
export function createTicketsUi({html,renderHtml,closeModal,proxyApi}){
  let _currentTicketId=null;
  const _TICKET_STATUS_LABELS={open:'Открыт',waiting_human:'Ждёт оператора',new_user:'Новый польз.',closed:'Закрыт'};
  const _TICKET_STATUS_COLORS={open:'#4af',waiting_human:'#fa4',new_user:'#a4f',closed:'#888'};

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
      if(!tickets.length){renderHtml(tbody,html`<tr><td colspan="6" style="text-align:center;color:var(--text3)">Тикетов нет</td></tr>`);return;}
      renderHtml(tbody,html`${tickets.map(t=>html`
        <tr>
          <td>#${t.id}</td>
          <td><span style="color:${_TICKET_STATUS_COLORS[t.status]||'#888'};font-weight:600">${_TICKET_STATUS_LABELS[t.status]||t.status}</span></td>
          <td>${t.marzban_username||`tg:${t.telegram_id}`}</td>
          <td style="font-size:12px;color:var(--text2)">${_tsAgo(t.updated_at)}</td>
          <td style="font-size:12px;color:var(--text3)">${_fmtDate(t.created_at)}</td>
          <td><button data-action="open-ticket" data-ticket-id="${t.id}">Открыть</button></td>
        </tr>`)}`);
    }catch(e){renderHtml(tbody,html`<tr><td colspan="6" style="color:#f66">Ошибка загрузки</td></tr>`);}
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
      const bg=m.role==='user'?'var(--bg4)':m.role==='ai'?'#1a3a2a':'#2a2a1a';
      const label=m.role==='user'?'Пользователь':m.role==='ai'?'AI':'Оператор';
      return html`<div style="margin-bottom:8px;padding:8px;background:${bg};border-radius:6px">
        <div style="font-size:11px;color:var(--text3);margin-bottom:3px">${label} · ${_fmtDate(m.ts)}</div>
        <div style="white-space:pre-wrap;font-size:13px">${m.text}</div>
      </div>`;
    })}`:html`<div style="color:var(--text3);font-size:13px">Сообщений нет</div>`);
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
    status.style.color='#6f6';status.textContent='Отправлено';
    setTimeout(()=>{status.textContent='';status.style.color='';},2000);
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
