export function formatTimestamp(value){
  if(value===null||value===undefined)return '—';
  return new Date(Number(value)*1000).toLocaleString('ru-RU');
}

export function formatDuration(seconds){
  if(seconds===null||seconds===undefined)return '—';
  const value=Math.max(0,Number(seconds));
  const days=Math.floor(value/86400);
  const hours=Math.floor((value%86400)/3600);
  return days?`${days} д ${hours} ч`:`${hours} ч`;
}

export function badgeClass(value){
  if(['ACTIVE','BOUND','OK_MIGRATED','MIGRATED','UNLIMITED'].includes(value))return 'badge-green';
  if(['DISABLED','CLOSED','EXPIRED','ERROR_RECONCILE','RECONCILE_REQUIRED','COMPATIBILITY_BLOCK'].includes(value))return 'badge-red';
  if(['CONTACT_USER','MANUAL_REVIEW','PENDING_LINK','MIGRATING'].includes(value))return 'badge-amber';
  return 'badge-gray';
}

export function maskTelegram(value){
  const text=String(value??'');
  if(text.length<=4)return '••••';
  return `${text.slice(0,2)}••••${text.slice(-2)}`;
}
