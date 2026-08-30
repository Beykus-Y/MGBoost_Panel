"""Initial-onboarding promo ingress: кнопка «🎟 Ввести промокод» для
совершенно нового пользователя. Все сценарии маршрутизируют Updates через
реальный aiogram Dispatcher (handler-level вызовы пропускают реальный
порядок регистрации — здесь он и есть предмет теста: промо-метка должна
перехватываться ДО msg_waiting_link).

Ключевые инварианты:
* вход переиспользует СУЩЕСТВУЮЩИЙ promo FSM (waiting_promo_code +
  `_promo_redeem_message`) — второй redemption path не создаётся;
* back/cancel и любые отказы возвращают onboarding-пользователя в его
  onboarding, а не в меню активного пользователя / AI-диалог;
* backend fail-closed для пользователя без canonical account закреплён
  как есть (никаких обходов и ложного успеха)."""
import asyncio
import importlib
import os
import sys
import tempfile

import pytest

from src.security import AdminSessionStore

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PRIMARY = "owner:mgboost-primary:v1"
PRIMARY_LOGIN = "authenticated-primary-login"


@pytest.fixture
def db(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="bot-onboarding-promo-")
    monkeypatch.setenv("DATA_DIR", tmp)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_ACTOR_ID", PRIMARY)
    monkeypatch.setenv("PRIMARY_MGBOOST_ADMIN_LOGIN", PRIMARY_LOGIN)
    import src.config as config
    import src.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.DB_PATH = os.path.join(tmp, "db.sqlite3")
    instance = database.Database()
    yield instance
    instance._conn.close()


class StubTelegramSession:

    @staticmethod
    def make():
        from aiogram.client.session.base import BaseSession

        class Session(BaseSession):
            def __init__(self):
                super().__init__()
                self.methods = []

            async def close(self):
                pass

            async def make_request(self, bot, method, timeout=None):
                self.methods.append(method)
                return True

            async def stream_content(self, url, headers=None, timeout=30,
                                     chunk_size=65536, raise_for_status=True):
                if False:
                    yield b""

        return Session()


def _text_update(telegram_id, text, update_id=1):
    from datetime import datetime, timezone
    from aiogram.types import Chat, Message, Update, User

    message = Message(
        message_id=100 + update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=User(id=telegram_id, is_bot=False, first_name="U"),
        text=text,
    )
    return Update(update_id=update_id, message=message)


def _callback_update(telegram_id, data, update_id=1):
    from datetime import datetime, timezone
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    message = Message(
        message_id=100 + update_id,
        date=datetime.now(timezone.utc),
        chat=Chat(id=telegram_id, type="private"),
        from_user=User(id=telegram_id, is_bot=False, first_name="U"),
    )
    callback = CallbackQuery(
        id=str(update_id),
        from_user=User(id=telegram_id, is_bot=False, first_name="U"),
        chat_instance="test-instance", data=data, message=message,
    )
    return Update(update_id=update_id, callback_query=callback)


async def _feed(db, updates, telegram_id):
    from aiogram import Bot, Dispatcher
    from aiogram.fsm.storage.memory import MemoryStorage
    from src.bot_support import setup_support_handlers

    session = StubTelegramSession.make()
    bot = Bot("123456:abcdefghijklmnopqrstuvwxyzABCDE", session=session)
    dp = Dispatcher(storage=MemoryStorage())
    setup_support_handlers(dp, db, marzban=None)
    for index, upd in enumerate(updates, start=1):
        await dp.feed_update(bot, upd)
    context = dp.fsm.get_context(bot=bot, chat_id=telegram_id, user_id=telegram_id)
    state = await context.get_state()
    await dp.storage.close()
    await bot.session.close()
    return session.methods, state


def _texts(methods):
    return [getattr(m, "text", "") or "" for m in methods]


def _reply_keyboards(methods):
    return [
        [btn.text for row in m.reply_markup.keyboard for btn in row]
        for m in methods
        if getattr(m, "reply_markup", None) is not None
        and getattr(m.reply_markup, "keyboard", None) is not None
    ]


def _direct_owner(db, telegram_id):
    account = db.accounts.create_account("DIRECT", now=1)
    db.accounts.link_telegram_owner(
        account["id"], telegram_id, provenance="DIRECT_BIND", actor="test", now=1,
    )
    return account


def _define_promo(db, code, effect_kind, effect_params, trial_class=None):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    cap = db.primary_admin_authority.authorize_session(session)
    db.promo.create_definition(
        cap, code=code, effect_kind=effect_kind, trial_class=trial_class,
        effect_params=effect_params, reason="onboarding promo ingress test",
        idempotency_key=f"promo-def-{code}-0000000001", now=1_000,
    )


# --- 1. новый пользователь видит «🎟 Ввести промокод» -------------------------

def test_new_user_start_shows_promo_button(db):
    methods, state = asyncio.run(_feed(db, [_text_update(3001, "/start")], 3001))
    assert state == "SupportStates:waiting_link"
    texts = _texts(methods)
    # reply-клавиатура: старая метка покупки не тронута, промокод добавлен
    assert _reply_keyboards(methods)[0] == ["🛒 Купить / Продлить", "🎟 Ввести промокод"]
    assert any("«🎟 Ввести промокод» — применить промокод." in t for t in texts)
    # inline-меню: все три входа онбординга, старые формулировки/коллбэки те же
    inline = [
        [btn.callback_data for row in m.reply_markup.inline_keyboard for btn in row]
        for m in methods
        if getattr(m, "reply_markup", None) is not None
        and getattr(m.reply_markup, "inline_keyboard", None) is not None
    ]
    assert inline == [["buy_open", "start_promo", "start_link"]]
    labels = [
        [btn.text for row in m.reply_markup.inline_keyboard for btn in row]
        for m in methods
        if getattr(m, "reply_markup", None) is not None
        and getattr(m.reply_markup, "inline_keyboard", None) is not None
    ]
    assert labels == [["🛒 Выбрать тариф", "🎟 Ввести промокод", "У меня уже есть подписка"]]


# --- 2. нажатие переводит в СУЩЕСТВУЮЩИЙ promo input state --------------------

def test_promo_reply_button_enters_existing_promo_input_state(db):
    methods, state = asyncio.run(_feed(
        db,
        [_text_update(3002, "/start", update_id=1),
         _text_update(3002, "🎟 Ввести промокод", update_id=2)],
        3002,
    ))
    assert state == "SupportStates:waiting_promo_code"
    assert "Введите промокод одним сообщением:" in _texts(methods)


def test_promo_inline_callback_enters_same_promo_input_state(db):
    methods, state = asyncio.run(_feed(
        db,
        [_text_update(3003, "/start", update_id=1),
         _callback_update(3003, "start_promo", update_id=2)],
        3003,
    ))
    assert state == "SupportStates:waiting_promo_code"
    assert "Введите промокод одним сообщением:" in _texts(methods)


# --- 3. cancel/back возвращает в initial onboarding ---------------------------

def test_cancel_returns_to_initial_onboarding_not_main_menu(db):
    methods, state = asyncio.run(_feed(
        db,
        [_text_update(3004, "/start", update_id=1),
         _text_update(3004, "🎟 Ввести промокод", update_id=2),
         _text_update(3004, "❌ Отмена", update_id=3)],
        3004,
    ))
    assert state == "SupportStates:waiting_link"
    texts = _texts(methods)
    assert "Действие отменено. Выберите, с чего начать:" in texts
    assert "С чего начнём?" in texts
    last_kb = _reply_keyboards(methods)[-1]
    assert "📱 Моя подписка" not in last_kb  # не меню активного пользователя
    assert "🛒 Купить / Продлить" in last_kb and "🎟 Ввести промокод" in last_kb


def test_back_alias_also_returns_to_onboarding(db):
    methods, state = asyncio.run(_feed(
        db,
        [_text_update(3005, "/start", update_id=1),
         _text_update(3005, "🎟 Ввести промокод", update_id=2),
         _text_update(3005, "⬅️ Назад к боту", update_id=3)],
        3005,
    ))
    assert state == "SupportStates:waiting_link"
    assert "Действие отменено. Выберите, с чего начать:" in _texts(methods)


# --- 4. existing canonical user: прежнее 5-кнопочное меню ---------------------

def test_existing_canonical_user_keeps_five_button_menu(db):
    _direct_owner(db, 3006)
    methods, state = asyncio.run(_feed(db, [_text_update(3006, "/start")], 3006))
    assert state == "SupportStates:in_dialog"
    assert _reply_keyboards(methods)[0] == [
        "📱 Моя подписка", "🛒 Купить / Продлить",
        "💻 Устройства", "🎟 Промокод",
        "🆘 Поддержка",
    ]


# --- 5. legacy-bound / special cases не регрессировали ------------------------

def test_legacy_linked_user_start_unchanged(db):
    db.save_tg_user(3007, "legacyuser")
    methods, state = asyncio.run(_feed(db, [_text_update(3007, "/start")], 3007))
    assert state == "SupportStates:in_dialog"
    assert "С возвращением" in "".join(_texts(methods))
    assert "🎟 Ввести промокод" not in _reply_keyboards(methods)[0]


def test_legacy_main_menu_promo_guard_unchanged(db):
    """Legacy-bound пользователь (tg_user, без canonical account) в главном
    меню по-прежнему получает прежний guard, а не вход в промо-ввод."""
    db.save_tg_user(3008, "legacyuser")
    methods, state = asyncio.run(_feed(
        db,
        [_text_update(3008, "/start", update_id=1),
         _text_update(3008, "🎟 Промокод", update_id=2)],
        3008,
    ))
    assert state == "SupportStates:in_dialog"
    assert any("подтверждённому аккаунту" in t for t in _texts(methods))


# --- 6. старые cached reply labels продолжают обрабатываться ------------------

def test_stale_main_menu_promo_label_still_works_from_onboarding(db):
    """«🎟 Промокод» со старой закэшированной клавиатуры главного меню не
    должен упираться в «не могу распознать ссылку» после /start."""
    methods, state = asyncio.run(_feed(
        db,
        [_text_update(3009, "/start", update_id=1),
         _text_update(3009, "🎟 Промокод", update_id=2)],
        3009,
    ))
    assert state == "SupportStates:waiting_promo_code"
    assert "Введите промокод одним сообщением:" in _texts(methods)


def test_stale_buy_reply_label_still_routed_to_buy_funnel(db):
    methods, state = asyncio.run(_feed(
        db,
        [_text_update(3010, "/start", update_id=1),
         _text_update(3010, "🛒 Купить / Продлить", update_id=2)],
        3010,
    ))
    texts = _texts(methods)
    assert any("временно недоступна" in t for t in texts)  # воронка покупки
    assert not any("распознать ссылку" in t for t in texts)
    assert state == "SupportStates:waiting_link"


# --- 7. backend fail-closed для нового пользователя закреплён честно ----------

@pytest.mark.parametrize("code,kind,params,trial_class", [
    ("NEWTRIAL10", "TRIAL_GRANT", {"days": 3}, "onboard-trial"),
    ("NEWEXT7", "EXTEND_SUBSCRIPTION", {"days": 7}, None),
    ("NEWDISC50", "PURCHASE_DISCOUNT", {"discount_percent": 50}, None),
])
def test_new_user_promo_fail_closed_without_canonical_account(db, code, kind, params, trial_class):
    """Новый Telegram user без canonical account не может применить ни один
    из трёх эффектов: backend (`PromoStore.redeem_for_telegram_user` /
    `reserve_purchase_for_telegram_user`) требует существующий account.
    Тест закрепляет fail-closed результат: честный отказ, ноль redemption
    строк, возврат в onboarding — никакого ложного успеха и обходов."""
    _define_promo(db, code, kind, params, trial_class)
    methods, state = asyncio.run(_feed(
        db,
        [_text_update(3011, "/start", update_id=1),
         _text_update(3011, "🎟 Ввести промокод", update_id=2),
         _text_update(3011, code, update_id=3)],
        3011,
    ))
    texts = _texts(methods)
    assert any("не найден" in t for t in texts)
    assert not any("Промокод применён" in t or "Скидка зафиксирована" in t
                   for t in texts)
    assert db._conn.execute(
        "SELECT COUNT(*) c FROM mgboost_promo_redemptions"
    ).fetchone()["c"] == 0
    assert state == "SupportStates:waiting_link"
    last_kb = _reply_keyboards(methods)[-1]
    assert "📱 Моя подписка" not in last_kb
    assert "🎟 Ввести промокод" in last_kb


# --- 8. полный NEW USER TRIAL SIGNUP journey (WL_TRIAL разрешён) --------------

def _define_wl_trial(db, code="TRIALBOT"):
    _raw, session = AdminSessionStore().create(PRIMARY_LOGIN, "test-server-jwt")
    cap = db.primary_admin_authority.authorize_session(session)
    db.promo.create_definition(
        cap, code=code, effect_kind="TRIAL_GRANT", trial_class="WL_TRIAL",
        effect_params={"days": 1}, reason="onboarding wl trial journey test",
        idempotency_key=f"promo-def-{code}-0000000001", now=1_000,
    )


def test_full_wl_trial_journey_from_onboarding(db, monkeypatch):
    """/start → 🎟 Ввести промокод → валидный WL_TRIAL → trial активирован,
    canonical DIRECT account создан, первая opaque-ссылка выдана тем же
    существующим безопасным flow, пользователь выходит полноценным
    пользователем с обычным 5-кнопочным меню."""
    import src.config as config
    from src.promo import ensure_wl_trial_plan_version
    monkeypatch.setattr(config, "PUBLIC_HOST", "sub.beykus.fun")
    monkeypatch.setattr(config, "OPAQUE_SUBSCRIPTION_ENABLED", True)
    ensure_wl_trial_plan_version(db.accounts, now=1)
    _define_wl_trial(db)

    methods, state = asyncio.run(_feed(
        db,
        [_text_update(3012, "/start", update_id=1),
         _text_update(3012, "🎟 Ввести промокод", update_id=2),
         _text_update(3012, "trialbot", update_id=3)],
        3012,
    ))
    texts = _texts(methods)
    assert any("Trial активирован: 1 дн." in t for t in texts)
    # первая выдача ссылки — обычный пользовательский путь, не скрытый /newsub
    assert any("Ваша новая ссылка подписки" in t for t in texts)
    assert state == "SupportStates:in_dialog"
    assert _reply_keyboards(methods)[-1] == [
        "📱 Моя подписка", "🛒 Купить / Продлить",
        "💻 Устройства", "🎟 Промокод",
        "🆘 Поддержка",
    ]
    account = db.accounts.get_active_account_by_telegram_id(3012)
    assert account is not None and account["account_source"] == "DIRECT"
    subscription = db._conn.execute(
        "SELECT pv.plan_code FROM mgboost_subscriptions s "
        "JOIN mgboost_plan_versions pv ON pv.id=s.current_plan_version_id "
        "WHERE s.account_id=?",
        (account["id"],),
    ).fetchone()
    assert subscription["plan_code"] == "WL_TRIAL"
    credential = db._conn.execute(
        "SELECT status FROM mgboost_subscription_credentials WHERE account_id=?",
        (account["id"],),
    ).fetchone()
    assert credential is not None and credential["status"] == "ACTIVE"
