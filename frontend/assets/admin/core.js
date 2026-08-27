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

const HUMAN_LABELS=Object.freeze({
  ACTIVE:'Активен',DISABLED:'Отключён',EXPIRED:'Истёк',PENDING:'Ожидает',CLOSED:'Закрыт',
  UNLIMITED:'Безлимит',UNKNOWN_LEGACY:'Legacy-условия',NO_SUBSCRIPTION:'Нет подписки',
  BOUND:'Привязан',UNREGISTERED:'Не привязан',PENDING_LINK:'Ожидает привязки',AMBIGUOUS:'Нужна проверка',
  PARENT_READY:'Готов',NOT_READY:'Не готов',OK_MIGRATED:'Миграция штатно',
  WAITING_FOR_REGISTRATION:'Ожидает Telegram',CONTACT_USER:'Связаться с клиентом',
  MANUAL_REVIEW:'Ручная проверка',COMPATIBILITY_BLOCK:'Проблема совместимости',
  RECONCILE_REQUIRED:'Нужна сверка',MIGRATING:'Миграция идёт',MIGRATED:'Завершена',
  LEGACY_REVOKE_PENDING:'Ожидает отключения legacy',LEGACY_REVOKED:'Legacy отключён',
  ERROR_RECONCILE:'Ошибка сверки',NO_LINEAGE:'Нет миграции',
  INTERNAL:'Внутренний',DIRECT:'Прямой',PRIMARY:'Основной',SECONDARY:'Дополнительный',
  PROVEN:'Подтверждено',ABSENT:'Нет подтверждения',OWNER:'Владелец',REVOKED:'Отозван',
  FREE:'Свободен',INTERNAL_SLOT:'Служебный',CUSTOMER:'Клиентский',
  APPLIED:'Применено',IN_FLIGHT:'Выполняется',RETRY:'Повтор',ERROR:'Ошибка',
  AUTO:'Автоматически',ENDED:'Завершён',NOT_CREATED:'Не создан',
  SYNCED:'Синхронизировано',CANCELLED:'Отменён',LIMITED:'Ограниченный',NONE:'Нет',
  PLAN_PRODUCT:'Продление тарифа',WL_PACKAGE:'WL-пакет',
  ENTITLEMENT_MUTATION:'Entitlement',PAYMENT_RECORD:'Платёж',
  MANUAL_PAYMENT:'Ручной платёж',MANUAL_PAYMENT_EDIT:'Правка платежа',
  DEVICE_LIFECYCLE:'Устройство',MIGRATION_BINDING:'Миграция',LEGACY_GRACE:'Grace-период',
  SUBSCRIPTION_CREDENTIAL:'Credential',OWNERSHIP_REBIND:'Владелец Telegram',
  CREATED:'Создан',CONFIRMED:'Подтверждён',ADMIN_GRANTED:'Начислен админом',
  EXTERNAL_PAYMENT:'Внешний платёж',TELEGRAM_STARS:'Telegram Stars',
  MANUAL_PAYMENT_SRC:'Ручной платёж',SYSTEM:'Система',ADMIN:'Админ',
  ORDINARY:'Обычный rebind',COMPROMISE:'Компрометация',BASIC:'Базовый',
  BASIC_PLUS:'Базовый Плюс',BASIC_PRO:'Базовый Про',WL:'WL',
  EXTENDED:'Расширенный',FAMILY:'Семейный',
  SLOT_DISABLE:'Пауза устройства',SLOT_ENABLE:'Снятие паузы',
  ADMIN_EXPIRY_ADJUSTMENT:'Изменение срока',EXTEND_DAYS:'Продление',
  REDUCE_DAYS:'Сокращение',SET_EXACT:'Точная дата',END_NOW:'Завершить сейчас',
  IN_SYNC:'Синхронизировано',PARTIAL:'Частично',REPLAYED:'Повтор операции',
});

export function humanLabel(value,fallback){
  if(value===null||value===undefined||value==='')return fallback??'—';
  return HUMAN_LABELS[value]||fallback||String(value);
}

export function formatPercent(value){
  return `${Math.max(0,Math.min(100,Number(value)||0))}%`;
}

export function maskTelegram(value){
  const text=String(value??'');
  if(text.length<=4)return '••••';
  return `${text.slice(0,2)}••••${text.slice(-2)}`;
}

export function formatRub(amountMinor,currency='₽'){
  if(amountMinor===null||amountMinor===undefined)return '—';
  return `${Number(amountMinor).toLocaleString('ru-RU')} ${currency}`;
}

export function formatGb(bytes){
  if(bytes===null||bytes===undefined)return '—';
  return `${(Number(bytes)/1e9).toLocaleString('ru-RU',{maximumFractionDigits:1})} ГБ`;
}
