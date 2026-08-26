# PH4-05 grace-period communications -- DRAFT ONLY, NOT SENT

Nothing in this file has been sent to any real user, and nothing in this
session sent it. This is the finalized text for the owner to review/edit
and publish through whatever channel they already use to reach legacy
users (Telegram channel/group post, pinned message, etc.) -- no code in
this session publishes it automatically, and no existing safe publishing
mechanism was identified in this project to do so (see "Explicit
non-goals" below).

Placeholders: `{DEADLINE_DATE}` (owner's local time zone, formatted for end
users -- filled in from the real `cohort_end_at` once the cohort actually
starts, never a raw UTC epoch), `{SUPPORT_CONTACT}` (however the owner
already handles support, e.g. the bot's own "🆘 Позвать человека" flow).

## Primary communication: one public informational post (owner-published)

This is the main channel per the 2026-08-26 product decision: grace
membership does not wait on Telegram registration, so most cohort members
cannot yet receive a personal bot message -- the post is what reaches them.

> 📢 Важное обновление MGBoost
>
> Мы переводим все подписки на новую, более защищённую систему выдачи
> конфигураций. Ваша текущая ссылка подписки (та же самая, которой вы
> пользуетесь сейчас) продолжит работать без каких-либо действий с вашей
> стороны в течение следующих 14 дней, до {DEADLINE_DATE}.
>
> Чтобы гарантированно сохранить доступ после этого срока, пожалуйста,
> в течение этих 14 дней зарегистрируйтесь в этом Telegram-боте:
>
> 1. Откройте бота и нажмите /start (или пришлите вашу текущую ссылку
>    подписки, если бот попросит).
> 2. Бот привяжет вашу подписку к вашему Telegram-аккаунту.
> 3. После этого мы переведём ваш аккаунт на новую систему и, если
>    потребуется, пришлём новую ссылку -- никаких сложных действий с вашей
>    стороны не нужно.
>
> Если у вас несколько устройств -- достаточно один раз пройти регистрацию,
> дальше мы разберёмся сами. После {DEADLINE_DATE} старая инфраструктура
> будет отключена, и без регистрации в боте восстановить доступ будет
> сложнее. Если у вас уже есть вопросы -- {SUPPORT_CONTACT}.

## Reminder for users who registered but have not yet switched

Only ever sent to an already-Telegram-`BOUND` account whose
`last_opaque_activity` is still null -- reuses the bot's existing private-
chat send path, the same one `/newsub` already uses, so this is safe to
implement without a new notification subsystem when there is time for it;
it must never delay or gate the cohort clock itself.

> Напоминание: осталось {DAYS_LEFT} дней до отключения старой ссылки
> подписки MGBoost ({DEADLINE_DATE}). Вы уже зарегистрированы -- отправьте
> /newsub, чтобы получить новую ссылку и обновить её в VPN-клиенте.

## LK banner (shown while an active grace period exists for the logged-in
account and `last_opaque_activity` is still null)

> ⚠️ Старая ссылка подписки будет отключена {DEADLINE_DATE}. Получите новую
> ссылку и обновите её в VPN-клиенте на всех устройствах, чтобы не потерять
> доступ.
> [Получить новую ссылку]

## Support-ticket macro (for manual use only, not automated)

> Здравствуйте! Ваша подписка переходит на новый защищённый формат ссылки.
> Старая ссылка перестанет работать {DEADLINE_DATE}. Пожалуйста,
> зарегистрируйтесь в нашем Telegram-боте (пришлите вашу текущую ссылку
> подписки), и мы переведём вас на новую систему. Если что-то не работает
> после этого, напишите нам снова, и мы поможем.

## Explicit non-goals of this draft

- No automated publish path exists in this project for a Telegram
  channel/group post, and none was built this session -- the owner
  publishes the primary post manually through whatever channel/account
  they already use. This session does not attempt to publish it, per
  instruction ("не пытайся самостоятельно публиковать его... если
  существующая инфраструктура не имеет явно предусмотренного безопасного
  механизма публикации").
- The per-user reminder and LK banner reuse fully existing, already-proven
  send/render mechanisms (the bot's private-chat message path, the LK's
  existing banner surface) -- wiring either is small, optional follow-up
  work that must never gate or delay the cohort clock itself, and was not
  wired live in this session (kept as a documented, ready-to-implement
  draft instead, per instruction not to expand scope beyond the clock).
- No language/tone A-B testing, no multi-language variants -- single
  Russian draft only, matching this project's existing bot/LK copy
  convention observed elsewhere in the codebase.
