# PH4-05 grace-period communications -- DRAFT ONLY, NOT SENT

Nothing in this file has been sent to any real user, and nothing in this
session sent it. These are drafts for the owner to review/edit/approve
before PH4-05 is ever started for a real account. No code in this session
sends a Telegram message or renders an LK notice from this text.

Placeholders: `{DAYS_LEFT}`, `{DEADLINE_DATE}` (owner's local time zone,
formatted for end users, not raw UTC epoch), `{NEW_URL_HELP_LINK}` (support
contact/instructions, not the raw opaque URL itself -- never put the actual
bearer token in a broadcast message).

## Telegram (bot message, private chat only, same channel `/newsub` already uses)

Initial notice, sent once when an account's grace period starts:

> Обновление подписки MGBoost
>
> Ваша старая ссылка подписки будет отключена через 14 дней
> ({DEADLINE_DATE}). Пожалуйста, перейдите на новую защищённую ссылку
> заранее -- отправьте команду /newsub в этот чат, чтобы получить новую
> ссылку, и обновите её в вашем VPN-клиенте.
>
> Если у вас уже несколько устройств -- обновите ссылку на каждом из них.
> Если возникнут сложности, напишите в поддержку: {NEW_URL_HELP_LINK}

Reminder (e.g. at day 7 and day 12, only if `last_opaque_activity` is still
null for the account -- do not remind someone who has already switched):

> Напоминание: осталось {DAYS_LEFT} дней до отключения старой ссылки
> подписки MGBoost ({DEADLINE_DATE}). Похоже, вы ещё не переключились на
> новую ссылку. Отправьте /newsub, чтобы получить новую ссылку сейчас.

## LK (in-app banner, shown while an active grace period exists for the
logged-in account and `last_opaque_activity` is still null)

> ⚠️ Старая ссылка подписки будет отключена {DEADLINE_DATE} ({DAYS_LEFT}
> дней осталось). Получите новую ссылку и обновите её в VPN-клиенте на всех
> устройствах, чтобы не потерять доступ.
> [Получить новую ссылку]

## Support-ticket macro (for manual use only, not automated)

> Здравствуйте! Ваша подписка переходит на новый защищённый формат ссылки.
> Старая ссылка перестанет работать {DEADLINE_DATE}. Я отправил(а) вам
> новую ссылку -- пожалуйста, обновите её в клиенте на всех ваших
> устройствах. Если что-то не работает после обновления, напишите нам
> снова, и мы поможем.

## Explicit non-goals of this draft

- No automated send path exists yet for any of the above -- wiring a real
  send (bot broadcast job, LK banner render) is separate follow-up work,
  intentionally not built in this session (it would be the first
  genuinely user-visible PH4-05 change, and is exactly the kind of action
  this session was told to stop short of).
- No language/tone A-B testing, no multi-language variants -- single
  Russian draft only, matching this project's existing bot/LK copy
  convention observed elsewhere in the codebase.
