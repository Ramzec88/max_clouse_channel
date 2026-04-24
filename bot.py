import logging
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
    except Exception:
        log.exception("Ошибка при обработке события %s", update_type)


def _on_message(message: dict) -> None:
    user_id: int = message["sender"]["user_id"]
    text: str = message.get("body", {}).get("text", "").strip()
    if text in ("/start", "/start@"):
        _cmd_start(user_id)


def _on_callback(callback: dict) -> None:
    user_id: int = callback["user"]["user_id"]
    payload: str = callback.get("payload", "")
    if payload == "subscribe":
        _handle_subscribe(user_id)
    elif payload == "renew":
        _handle_subscribe(user_id)


def _cmd_start(user_id: int) -> None:
    sub = db.get_active_subscription(user_id)
    if sub:
        max_api.send_message(
            user_id,
            f"У вас уже есть активная подписка до {_fmt_date(sub['expires_at'])}.\n"
            f"Ссылка на канал: {config.MAX_CHANNEL_INVITE_LINK}",
        )
        return

    period = f"{config.SUBSCRIPTION_MINUTES} мин." if config.SUBSCRIPTION_MINUTES else f"{config.SUBSCRIPTION_MONTHS} мес."
    max_api.send_message(
        user_id,
        f"Добро пожаловать!\n\n"
        f"Получите доступ к закрытому каналу на {period} — всего {config.SUBSCRIPTION_PRICE} {config.SUBSCRIPTION_CURRENCY}.\n\n"
        "Нажмите кнопку ниже, чтобы оплатить.",
        buttons=[[{"type": "callback", "text": f"Подписаться за {config.SUBSCRIPTION_PRICE} руб.", "payload": "subscribe"}]],
    )


def _handle_subscribe(user_id: int) -> None:
    if db.has_pending_payment(user_id):
        max_api.send_message(
            user_id,
            "Ваш платёж уже ожидает подтверждения. Пожалуйста, завершите оплату по ранее отправленной ссылке.",
        )
        return

    if db.get_active_subscription(user_id):
        max_api.send_message(
            user_id,
            "У вас уже есть активная подписка. Напишите /start чтобы получить ссылку на канал.",
        )
        return

    try:
        payment = payments.create_payment(user_id)
    except Exception:
        log.exception("Не удалось создать платёж для user_id=%s", user_id)
        max_api.send_message(user_id, "Не удалось создать платёж. Попробуйте позже.")
        return

    db.save_pending_payment(user_id, payment["payment_id"], config.MAX_CHANNEL_ID)
    max_api.send_message(
        user_id,
        f"Оплатите подписку по ссылке:\n{payment['confirmation_url']}\n\n"
        "После оплаты вы будете автоматически добавлены в канал (обычно в течение 15 секунд).",
    )
    log.info("Создан платёж %s для user_id=%s", payment["payment_id"], user_id)


# ──────────────────────────────────────────────
# Фоновый поллер платежей и истечений
# ──────────────────────────────────────────────

def run_payment_poller() -> None:
    log.info("Поллер платежей запущен (интервал %d сек.)", config.POLL_INTERVAL_SECONDS)
    while True:
        time.sleep(config.POLL_INTERVAL_SECONDS)
        _check_pending_payments()
        _check_expired_subscriptions()


def _check_pending_payments() -> None:
    pending = db.get_pending_payments()
    for row in pending:
        payment_id: str = row["payment_id"]
        user_id: int = row["user_id"]
        try:
            status = payments.check_payment_status(payment_id)
        except Exception:
            log.exception("Ошибка проверки статуса платежа %s", payment_id)
            continue

        if status == "succeeded":
            _on_payment_succeeded(payment_id, user_id)
        elif status == "canceled":
            _on_payment_canceled(payment_id, user_id)


def _on_payment_succeeded(payment_id: str, user_id: int) -> None:
    db.activate_subscription(payment_id)
    added = max_api.add_member_to_channel(config.MAX_CHANNEL_ID, user_id)
    if added:
        max_api.send_message(
            user_id,
            "Оплата прошла успешно!\n\nВы добавлены в закрытый канал. "
            "Откройте MAX — канал уже появился в ваших чатах.",
        )
        log.info("Подписка активирована (API): user_id=%s payment_id=%s", user_id, payment_id)
    else:
        # Настройки приватности не позволяют добавить напрямую — даём ссылку в личку
        max_api.send_message(
            user_id,
            "Оплата прошла успешно!\n\n"
            "Не удалось добавить вас автоматически — скорее всего, в настройках MAX "
            "у вас закрыто добавление в каналы.\n\n"
            f"Перейдите по ссылке для вступления:\n{config.MAX_CHANNEL_INVITE_LINK}\n\n"
            "Чтобы в будущем добавление работало автоматически:\n"
            "Настройки MAX → Конфиденциальность → Кто может добавлять в группы → Все",
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
    expired = db.get_expired_subscriptions()
    for row in expired:
        payment_id: str = row["payment_id"]
        user_id: int = row["user_id"]
        channel_id: int = row["channel_id"]

        db.mark_expired(payment_id)
        max_api.remove_member_from_channel(channel_id, user_id)

        period = f"{config.SUBSCRIPTION_MINUTES} мин." if config.SUBSCRIPTION_MINUTES else f"{config.SUBSCRIPTION_MONTHS} мес."
        max_api.send_message(
            user_id,
            "Ваша подписка на закрытый канал истекла. Вы были удалены из канала.\n\n"
            f"Чтобы возобновить доступ, оформите новую подписку на {period}.",
            buttons=[[{"type": "callback", "text": f"Продлить за {config.SUBSCRIPTION_PRICE} руб.", "payload": "renew"}]],
        )
        log.info("Подписка истекла: user_id=%s payment_id=%s", user_id, payment_id)


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
    log.info("БД: %s (внимание: на Railway данные сбросятся при рестарте)", config.DB_PATH)

    db.init_db()
    payments.configure()

    poller_thread = threading.Thread(target=run_payment_poller, daemon=True)
    poller_thread.start()

    run_poll_loop()
