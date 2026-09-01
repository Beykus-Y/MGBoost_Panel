// PH7-16 Wave 0B — shared safe-render / toast / modal-close kernel.
// ES module (Wave 0A shipped this as a classic script relying on shared
// top-level scope; Wave 0B makes every cross-file dependency an explicit
// import/export instead). No behavior change from Wave 0A.
export class SafeMarkup{
  constructor(value){this.value=value}
}
export function esc(s){
  return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
export function html(strings,...values){
  let value='';
  strings.forEach((part,index)=>{
    value+=part;
    if(index<values.length)value+=safeMarkupValue(values[index]);
  });
  return new SafeMarkup(value);
}
export function safeMarkupValue(value){
  if(value instanceof SafeMarkup)return value.value;
  if(Array.isArray(value))return value.map(safeMarkupValue).join('');
  return esc(value);
}
export function renderHtml(element,markup){
  if(!(markup instanceof SafeMarkup))throw new TypeError('SafeMarkup required');
  const template=document.createElement('template');
  template.innerHTML=markup.value;
  element.replaceChildren(template.content.cloneNode(true));
}

export function toast(msg,type='ok'){
  const t=document.getElementById('toast');
  t.textContent=msg;t.className='show '+(type==='ok'?'ok':'err');
  clearTimeout(t._t);t._t=setTimeout(()=>t.className='',2500);
}

export function closeModal(id){document.getElementById(id).classList.remove('open')}

// Module scripts defer like classic `defer` scripts (run after DOM parse),
// so this top-level DOM wiring is as safe here as it was in the Wave 0A
// classic-script version.
document.querySelectorAll('.modal-overlay').forEach(m=>m.addEventListener('click',e=>{if(e.target===m)m.classList.remove('open')}));
