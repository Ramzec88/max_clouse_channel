import logging
import re
import threading
import time

import config
import database as db
import max_api
import payments

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

_SUPPORT = "Поддержка: https://max.ru/id320203526914_3_bot"

_PRIVACY_MSG = (
    "Ваши настройки приватности запрещают добавление в каналы.\n\n"
    "Чтобы получить доступ:\n"
    "Настройки MAX → Конфиденциальность → "
    "Кто может добавлять в группы → Все\n\n"
    "После этого нажмите кнопку ниже.\n\n"
    f"{_SUPPORT}"
)

# user_id → "waiting_email"
_user_state: dict[int, str] = {}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ──────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────

def _save_user_info(user: dict) -> None:
    try:
        db.upsert_user(
            user["user_id"],
            user.get("name"),
            user.get("username"),
        )
    except Exception:
        log.warning("Не удалось сохранить инфо о пользователе %s", user.get("user_id"))


def _user_label(user_id: int) -> str:
    u = db.get_user(user_id)
    parts = []
    if u:
        if u.get("name"):
            parts.append(u["name"])
        if u.get("username"):
            parts.append(f"https://max.ru/{u['username']}")
    parts.append(f"user_id: {user_id}")
    return "\n".join(parts)


# ──────────────────────────────────────────────
# Обработчики событий от MAX
# ──────────────────────────────────────────────

def handle_update(update: dict) -> None:
    update_type = update.get("update_type")
    try:
        if update_type == "message_created":
            _on_message(update["message"])
        elif update_type == "message_callback":
            _on_callback(update["callback"])
        elif update_type == "user_added":
            _on_user_added(update)
        elif update_type == "bot_started":
            user = update["user"]
            _save_user_info(user)
            _cmd_start(user["user_id"])
        else:
            log.info("UNKNOWN UPDATE type=%s full=%s", update_type, update)
    except Exception:
        log.exception("Ошибка при обработке события %s", update_type)


def _on_message(message: dict) -> None:
    sender = message["sender"]
    user_id: int = sender["user_id"]
    _save_user_info(sender)
    text: str = message.get("body", {}).get("text", "").strip()

    if _user_state.get(user_id) == "waiting_email":
        _handle_email_input(user_id, text)
        return

    if text in ("/start", "/start@"):
        _cmd_start(user_id)


def _on_callback(callback: dict) -> None:
    user_id: int = callback["user"]["user_id"]
    payload: str = callback.get("payload", "")
    if payload == "subscribe":
        _handle_subscribe(user_id)
    elif payload == "renew":
        _handle_subscribe(user_id)
    elif payload == "retry_add":
        _handle_retry_add(user_id)


def _cmd_start(user_id: int) -> None:
    sub = db.get_active_subscription(user_id)
    if sub:
        # Есть активная подписка — пробуем добавить (на случай если ещё не в канале)
        added = max_api.add_member_to_channel(config.MAX_CHANNEL_ID, user_id)
        if added:
            max_api.send_message(
                user_id,
                f"Ваша подписка активна до {_fmt_date(sub['expires_at'])}.\n"
                "Канал доступен в ваших чатах MAX.",
            )
        else:
            max_api.send_message(
                user_id,
                f"Ваша подписка активна до {_fmt_date(sub['expires_at'])}.\n\n"
                + _PRIVACY_MSG,
                buttons=[[{"type": "callback", "text": "Попробовать ещё раз", "payload": "retry_add"}]],
            )
        return

    period = f"{config.SUBSCRIPTION_MINUTES} мин." if config.SUBSCRIPTION_MINUTES else f"{config.SUBSCRIPTION_MONTHS} мес."
    max_api.send_message(
        user_id,
        f"Добро пожаловать!\n\n"
        f"Получите доступ к закрытому каналу на {period} — "
        f"всего {config.SUBSCRIPTION_PRICE} {config.SUBSCRIPTION_CURRENCY}.\n\n"
        "Нажмите кнопку ниже, чтобы оплатить.",
        buttons=[[{"type": "callback", "text": f"Подписаться за {config.SUBSCRIPTION_PRICE} руб.", "payload": "subscribe"}]],
    )


def _handle_subscribe(user_id: int) -> None:
    if db.has_pending_payment(user_id):
        max_api.send_message(
            user_id,
            "Ваш платёж уже ожидает подтверждения. Завершите оплату по ранее отправленной ссылке.",
        )
        return

    if db.get_active_subscription(user_id):
        max_api.send_message(
            user_id,
            "У вас уже есть активная подписка. Напишите /start.",
        )
        return

    _user_state[user_id] = "waiting_email"
    max_api.send_message(
        user_id,
        "Для выставления чека укажите ваш email:",
    )


def _handle_email_input(user_id: int, text: str) -> None:
    if not _EMAIL_RE.match(text):
        max_api.send_message(
            user_id,
            "Это не похоже на email. Попробуйте ещё раз (например: name@mail.ru):",
        )
        return

    _user_state.pop(user_id, None)
    email = text.lower()

    try:
        payment = payments.create_payment(user_id, email)
    except Exception:
        log.exception("Не удалось создать платёж для user_id=%s", user_id)
        max_api.send_message(user_id, f"Не удалось создать платёж. Попробуйте позже.\n\n{_SUPPORT}")
        return

    db.save_pending_payment(user_id, payment["payment_id"], config.MAX_CHANNEL_ID)
    max_api.send_message(
        user_id,
        f"Оплатите подписку по ссылке:\n{payment['confirmation_url']}\n\n"
        "После оплаты вы будете автоматически добавлены в канал (обычно в течение 15 секунд).",
    )
    log.info("Создан платёж %s для user_id=%s email=%s", payment["payment_id"], user_id, email)


def _on_user_added(update: dict) -> None:
    # Срабатывает при любом вступлении: по ссылке или через API
    if not update.get("is_channel"):
        return  # интересуют только каналы
    user_id: int = update["user_id"]
    chat_id: int = update["chat_id"]

    if chat_id != config.MAX_CHANNEL_ID:
        return  # событие из другого чата

    if db.get_active_subscription(user_id):
        log.info("user_added: user_id=%s — подписка активна, пропускаем", user_id)
        return

    # Нет активной подписки — немедленно кикаем
    log.warning("user_added: user_id=%s — нет подписки, кикаем", user_id)
    max_api.remove_member_from_channel(chat_id, user_id)

    period = f"{config.SUBSCRIPTION_MINUTES} мин." if config.SUBSCRIPTION_MINUTES else f"{config.SUBSCRIPTION_MONTHS} мес."
    max_api.send_message(
        user_id,
        "У вас нет активной подписки на этот канал.\n\n"
        f"Оформите подписку на {period} — "
        f"{config.SUBSCRIPTION_PRICE} {config.SUBSCRIPTION_CURRENCY}.\n\n"
        f"{_SUPPORT}",
        buttons=[[{"type": "callback", "text": f"Подписаться за {config.SUBSCRIPTION_PRICE} руб.", "payload": "subscribe"}]],
    )


def _handle_retry_add(user_id: int) -> None:
    sub = db.get_active_subscription(user_id)
    if not sub:
        max_api.send_message(user_id, f"Активная подписка не найдена. Напишите /start.\n\n{_SUPPORT}")
        return

    added = max_api.add_member_to_channel(config.MAX_CHANNEL_ID, user_id)
    if added:
        max_api.send_message(
            user_id,
            "Готово! Вы добавлены в канал. Откройте MAX — канал появился в ваших чатах.",
        )
        log.info("retry_add успешно: user_id=%s", user_id)
    else:
        max_api.send_message(
            user_id,
            "Всё ещё не получается. Убедитесь что изменили настройку и попробуйте снова.\n\n"
            + _PRIVACY_MSG,
            buttons=[[{"type": "callback", "text": "Попробовать ещё раз", "payload": "retry_add"}]],
        )
        log.warning("retry_add не удался: user_id=%s", user_id)


# ──────────────────────────────────────────────
# Фоновый поллер платежей и истечений
# ──────────────────────────────────────────────

def run_payment_poller() -> None:
    log.info("Поллер платежей запущен (интервал %d сек.)", config.POLL_INTERVAL_SECONDS)
    while True:
        time.sleep(config.POLL_INTERVAL_SECONDS)
        try:
            active_count = len(db.get_active_subscriptions_all())
            if active_count:
                log.info("Поллер: активных подписок в БД: %d", active_count)
            _check_pending_payments()
            _check_expired_subscriptions()
        except Exception:
            log.exception("Необработанная ошибка в поллере, продолжаем")


def _check_pending_payments() -> None:
    pending = db.get_pending_payments()
    if not pending:
        return
    log.info("Поллер: проверяю %d платёж(ей)", len(pending))
    for row in pending:
        payment_id: str = row["payment_id"]
        user_id: int = row["user_id"]
        try:
            status = payments.check_payment_status(payment_id)
            log.info("Платёж %s → статус: %s", payment_id, status)
        except Exception:
            log.exception("Ошибка проверки статуса платежа %s", payment_id)
            continue

        if status == "succeeded":
            _on_payment_succeeded(payment_id, user_id)
        elif status == "canceled":
            _on_payment_canceled(payment_id, user_id)


def _notify_admin(text: str) -> None:
    if config.ADMIN_USER_ID:
        try:
            max_api.send_message(config.ADMIN_USER_ID, text)
        except Exception:
            log.warning("Не удалось отправить уведомление админу")


def _on_payment_succeeded(payment_id: str, user_id: int) -> None:
    db.activate_subscription(payment_id)
    sub = db.get_active_subscription(user_id)
    expires = _fmt_date(sub["expires_at"]) if sub else "—"
    _notify_admin(
        f"Новая подписка!\n"
        f"{_user_label(user_id)}\n"
        f"payment_id: {payment_id}\n"
        f"Действует до: {expires}"
    )
    added = max_api.add_member_to_channel(config.MAX_CHANNEL_ID, user_id)
    if added:
        max_api.send_message(
            user_id,
            "Оплата прошла успешно!\n\nВы добавлены в закрытый канал. "
            "Откройте MAX — канал уже появился в ваших чатах.",
        )
        log.info("Подписка активирована (API): user_id=%s payment_id=%s", user_id, payment_id)
    else:
        # Настройки приватности блокируют прямое добавление — отправляем ссылку в личку
        invite_link = max_api.get_channel_invite_link(config.MAX_CHANNEL_ID)
        if invite_link:
            max_api.send_message(
                user_id,
                "Оплата прошла успешно!\n\n"
                "Ваши настройки приватности не позволяют добавить вас автоматически. "
                "Вступите по персональной ссылке:\n\n"
                f"{invite_link}\n\n"
                f"{_SUPPORT}",
            )
        else:
            max_api.send_message(
                user_id,
                f"Оплата прошла успешно, но не удалось добавить вас в канал.\n\n{_SUPPORT}",
            )
        log.warning("Подписка активирована (invite-link fallback): user_id=%s payment_id=%s", user_id, payment_id)


def _on_payment_canceled(payment_id: str, user_id: int) -> None:
    db.cancel_payment(payment_id)
    max_api.send_message(
        user_id,
        "Платёж был отменён или просрочен. Напишите /start чтобы попробовать снова.",
    )
    log.info("Платёж отменён: user_id=%s payment_id=%s", user_id, payment_id)


def _check_expired_subscriptions() -> None:
    try:
        expired = db.get_expired_subscriptions()
    except Exception:
        log.exception("Ошибка получения истёкших подписок")
        return

    if expired:
        log.info("Поллер: найдено %d истёкших подписок", len(expired))

    for row in expired:
        payment_id: str = row["payment_id"]
        user_id: int = row["user_id"]
        channel_id: int = row["channel_id"]
        try:
            removed = max_api.remove_member_from_channel(channel_id, user_id)
            log.info("Удаление из канала: user_id=%s removed=%s", user_id, removed)

            # Помечаем истёкшей только после удаления — чтобы повторить если не вышло
            db.mark_expired(payment_id)

            period = f"{config.SUBSCRIPTION_MINUTES} мин." if config.SUBSCRIPTION_MINUTES else f"{config.SUBSCRIPTION_MONTHS} мес."
            max_api.send_message(
                user_id,
                "Ваша подписка на закрытый канал истекла. Вы были удалены из канала.\n\n"
                f"Чтобы возобновить доступ, оформите новую подписку на {period}.",
                buttons=[[{"type": "callback", "text": f"Продлить за {config.SUBSCRIPTION_PRICE} руб.", "payload": "renew"}]],
            )
            log.info("Подписка истекла: user_id=%s payment_id=%s", user_id, payment_id)
        except Exception:
            log.exception("Ошибка при обработке истёкшей подписки user_id=%s payment_id=%s", user_id, payment_id)


# ──────────────────────────────────────────────
# Основной цикл long-polling
# ──────────────────────────────────────────────

def run_poll_loop() -> None:
    log.info("Long-polling запущен.")
    marker: int | None = None
    while True:
        try:
            data = max_api.get_updates(marker=marker)
            for update in data.get("updates", []):
                handle_update(update)
            marker = data.get("marker")
        except Exception:
            log.exception("Ошибка long-polling, повтор через 5 сек.")
            time.sleep(5)


# ──────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────

def _fmt_date(iso: str) -> str:
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(iso).astimezone(timezone.utc)
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except Exception:
        return iso


# ──────────────────────────────────────────────
# Точка входа
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import platform
    log.info("Python %s on %s", platform.python_version(), platform.system())

    db.init_db()
    payments.configure()

    poller_thread = threading.Thread(target=run_payment_poller, daemon=True)
    poller_thread.start()

    run_poll_loop()
