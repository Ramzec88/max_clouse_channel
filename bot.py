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

_MARKET_BTN = {"type": "link", "text": "Магазин Мишки Макса", "url": "https://market.mishka-max.ru"}

_PRIVACY_MSG = (
    "Ваши настройки приватности запрещают добавление в каналы.\n\n"
    "Чтобы получить доступ:\n"
    "Настройки MAX → Конфиденциальность → "
    "Кто может добавлять в группы → Все\n\n"
    "После этого нажмите кнопку ниже.\n\n"
    f"{_SUPPORT}"
)

_JOIN_HINT = (
    "\n\nВажно: после перехода по ссылке нажмите «Присоединиться» / «Вступить» "
    "прямо в MAX. Если просто открыть чат и не нажать эту кнопку, вы останетесь "
    "в режиме предпросмотра — без доступа к материалам канала."
)

# user_id → "waiting_email"
_user_state: dict[int, str] = {}

# admin_id → {"segment": "all"|"active", "text": str} — ожидает подтверждения /broadcast
_pending_broadcast: dict[int, dict] = {}

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
    elif text == "/stats" and user_id == config.ADMIN_USER_ID:
        _cmd_stats(user_id)
    elif text == "/members" and user_id == config.ADMIN_USER_ID:
        _cmd_members(user_id)
    elif text.startswith("/find ") and user_id == config.ADMIN_USER_ID:
        _cmd_find(user_id, text[6:].strip())
    elif text.startswith("/findname ") and user_id == config.ADMIN_USER_ID:
        _cmd_findname(user_id, text[10:].strip())
    elif text.startswith("/refund ") and user_id == config.ADMIN_USER_ID:
        _cmd_refund(user_id, text[8:].strip())
    elif text.startswith("/broadcast ") and user_id == config.ADMIN_USER_ID:
        _cmd_broadcast(user_id, text[11:].strip())


def _on_callback(callback: dict) -> None:
    user_id: int = callback["user"]["user_id"]
    payload: str = callback.get("payload", "")
    if payload == "subscribe":
        _handle_subscribe(user_id)
    elif payload == "renew":
        _handle_subscribe(user_id)
    elif payload == "retry_add":
        _handle_retry_add(user_id)
    elif payload == "get_link":
        _handle_get_link(user_id)
    elif payload == "confirm_broadcast":
        _handle_confirm_broadcast(user_id)
    elif payload == "cancel_broadcast":
        _handle_cancel_broadcast(user_id)


def _cmd_start(user_id: int) -> None:
    sub = db.get_active_subscription(user_id)
    if sub:
        added = max_api.add_member_to_channel(config.MAX_CHANNEL_ID, user_id)
        if added:
            max_api.send_message(
                user_id,
                f"Ваша подписка активна до {_fmt_date(sub['expires_at'])}.\n"
                "Канал доступен в ваших чатах MAX.",
                buttons=[[{"type": "callback", "text": "Получить ссылку в канал", "payload": "get_link"}]],
            )
        else:
            max_api.send_message(
                user_id,
                f"Ваша подписка активна до {_fmt_date(sub['expires_at'])}.\n\n"
                + _PRIVACY_MSG,
                buttons=[
                    [{"type": "callback", "text": "Попробовать ещё раз", "payload": "retry_add"}],
                    [{"type": "callback", "text": "Получить ссылку в канал", "payload": "get_link"}],
                ],
            )
        return

    period = f"{config.SUBSCRIPTION_MINUTES} мин." if config.SUBSCRIPTION_MINUTES else f"{config.SUBSCRIPTION_MONTHS} мес."
    max_api.send_message(
        user_id,
        f"Добро пожаловать!\n\n"
        f"Получите доступ к закрытому каналу на {period} — "
        f"всего {config.SUBSCRIPTION_PRICE} {config.SUBSCRIPTION_CURRENCY}.\n\n"
        "Нажмите кнопку ниже, чтобы оплатить.",
        buttons=[
            [{"type": "callback", "text": f"Подписаться за {config.SUBSCRIPTION_PRICE} руб.", "payload": "subscribe"}],
            [_MARKET_BTN],
        ],
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

    db.save_pending_payment(
        user_id, payment["payment_id"], config.MAX_CHANNEL_ID, email, float(config.SUBSCRIPTION_PRICE)
    )
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

    if user_id in config.PROTECTED_USER_IDS:
        log.info("user_added: user_id=%s — защищённый пользователь, пропускаем", user_id)
        return

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
        buttons=[
            [{"type": "callback", "text": f"Подписаться за {config.SUBSCRIPTION_PRICE} руб.", "payload": "subscribe"}],
            [_MARKET_BTN],
        ],
    )


def _cmd_stats(user_id: int) -> None:
    from datetime import datetime, timezone
    s = db.get_stats()
    fin = db.get_finance_stats()
    ret = db.get_retention_stats()
    now = datetime.now(timezone.utc)
    cur = config.SUBSCRIPTION_CURRENCY

    if ret["renewal_rate"] is not None:
        renewal_line = f"{ret['renewal_rate']:.0f}% ({ret['renewed_count']} из {ret['expired_count']})"
        churn_line = f"{ret['churn_rate']:.0f}%"
    else:
        renewal_line = "нет данных за 30 дней"
        churn_line = "нет данных за 30 дней"

    max_api.send_message(
        user_id,
        f"Статистика на {now.strftime('%d.%m.%Y %H:%M')} UTC\n\n"
        f"── Текущий момент ──\n"
        f"Активных подписок:  {s['active']}\n"
        f"Новых сегодня:      {s['new_today']}\n"
        f"Новых за месяц:     {s['new_month']}\n"
        f"Всего оплачено:     {s['total']}\n"
        f"Истекают в 24 ч:    {s['expiring_soon']}\n\n"
        f"── Финансы ──\n"
        f"MRR:                {fin['mrr']:.0f} {cur}\n"
        f"Выручка сегодня:    {fin['revenue_today']:.0f} {cur}\n"
        f"Выручка за 7 дней:  {fin['revenue_week']:.0f} {cur}\n"
        f"Выручка за 30 дней: {fin['revenue_month']:.0f} {cur}\n"
        f"Средний LTV:        {fin['avg_ltv']:.0f} {cur}\n\n"
        f"── Удержание (за 30 дней) ──\n"
        f"Истекло подписок:   {ret['expired_count']}\n"
        f"Продлили:           {renewal_line}\n"
        f"Отток (churn):      {churn_line}\n"
        f"Среднее число оплат на подписчика: {ret['avg_periods']:.1f}",
    )
    log.info("Статистика запрошена admin user_id=%s", user_id)


def _cmd_findname(admin_id: int, query: str) -> None:
    if not query:
        max_api.send_message(admin_id, "Использование: /findname Татьяна")
        return
    rows = db.find_by_name(query)
    if not rows:
        max_api.send_message(admin_id, f"Пользователей с именем «{query}» не найдено.")
        return
    _send_subscription_rows(admin_id, rows, header=f"Результаты по имени «{query}»:")


def _format_subscription_row(r: dict) -> str:
    name = r.get("name") or f"id{r['user_id']}"
    username = f" (@{r['username']})" if r.get("username") else ""
    status_ru = {
        "active": "активна",
        "expired": "истекла",
        "pending": "ожидает оплаты",
        "canceled": "отменена",
        "refunded": "возврат оформлен",
    }.get(r["status"], r["status"])
    return (
        f"Пользователь: {name}{username}\n"
        f"user_id: {r['user_id']}\n"
        f"Статус: {status_ru}\n"
        + (f"Email: {r['email']}\n" if r.get("email") else "")
        + (f"Оплачено: {_fmt_date(r['confirmed_at'])}\n" if r.get("confirmed_at") else "")
        + (f"Истекает: {_fmt_date(r['expires_at'])}\n" if r.get("expires_at") else "")
        + f"payment_id: {r['payment_id']}"
    )


def _send_subscription_rows(admin_id: int, rows: list[dict], header: str) -> None:
    lines = [header, ""]
    for r in rows:
        lines.append(_format_subscription_row(r))
        lines.append("")
    max_api.send_message(admin_id, "\n".join(lines).strip())


def _cmd_find(admin_id: int, email: str) -> None:
    if not email:
        max_api.send_message(admin_id, "Использование: /find email@example.com")
        return
    rows = db.find_by_email(email)
    if not rows:
        max_api.send_message(admin_id, f"Записей с email {email} не найдено.")
        return
    _send_subscription_rows(admin_id, rows, header=f"Результаты по email: {email}")
    log.info("Поиск по email=%s запрошен admin user_id=%s", email, admin_id)


def _cmd_refund(admin_id: int, identifier: str) -> None:
    if not identifier:
        max_api.send_message(admin_id, "Использование: /refund user_id или /refund email@example.com")
        return

    if identifier.isdigit():
        user_id = int(identifier)
    else:
        rows = db.find_by_email(identifier)
        active_rows = [r for r in rows if r["status"] == "active"]
        if not active_rows:
            max_api.send_message(admin_id, f"Активная подписка с email {identifier} не найдена.")
            return
        user_id = active_rows[0]["user_id"]

    sub = db.get_active_subscription(user_id)
    if not sub:
        max_api.send_message(admin_id, f"Активная подписка для user_id={user_id} не найдена.")
        return

    db.mark_refunded(sub["payment_id"])
    removed = max_api.remove_member_from_channel(config.MAX_CHANNEL_ID, user_id)

    max_api.send_message(
        user_id,
        "Ваша подписка отменена, доступ к каналу закрыт.\n\n"
        f"{_SUPPORT}",
    )

    max_api.send_message(
        admin_id,
        f"Готово. user_id={user_id} исключён из канала (removed={removed}), "
        f"подписка payment_id={sub['payment_id']} помечена как возврат.",
    )
    log.info("Refund: user_id=%s payment_id=%s removed=%s admin=%s", user_id, sub['payment_id'], removed, admin_id)


def _cmd_members(user_id: int) -> None:
    rows = db.get_active_subscribers_with_info()
    if not rows:
        max_api.send_message(user_id, "Активных подписчиков нет.")
        return

    header = f"Активные подписчики ({len(rows)}), сортировка по дате истечения:\n"
    lines = []
    for i, r in enumerate(rows, 1):
        name = r.get("name") or f"id{r['user_id']}"
        username = f" (@{r['username']})" if r.get("username") else ""
        expires = _fmt_date(r["expires_at"])
        lines.append(f"{i}. {name}{username} — до {expires}")

    # Отправляем кусками по 3500 символов чтобы не превысить лимит MAX
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 3500:
            max_api.send_message(user_id, chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        max_api.send_message(user_id, chunk)
    log.info("Список участников запрошен admin user_id=%s", user_id)


_BROADCAST_SEGMENTS = {
    "all": "всем, кто писал боту",
    "active": "активным подписчикам",
    "inactive": "неактивным (оформляли подписку, но сейчас не продлена)",
}


def _broadcast_recipients(segment: str) -> list[int]:
    if segment == "all":
        return db.get_all_user_ids()
    if segment == "active":
        return [r["user_id"] for r in db.get_active_subscribers_with_info()]
    return db.get_inactive_subscriber_ids()


def _renew_button() -> list[dict]:
    return [{"type": "callback", "text": f"Оформить/продлить за {config.SUBSCRIPTION_PRICE} руб.", "payload": "renew"}]


def _link_button() -> list[dict]:
    return [{"type": "callback", "text": "Получить ссылку в канал", "payload": "get_link"}]


_BROADCAST_BUTTONS = {
    "btn": ("«Оформить/продлить подписку»", _renew_button),
    "link": ("«Получить ссылку в канал»", _link_button),
}


def _cmd_broadcast(admin_id: int, rest: str) -> None:
    segment_token, _, text = rest.partition(" ")
    text = text.strip()
    segment, _, flag = segment_token.partition("+")

    if segment not in _BROADCAST_SEGMENTS or (flag and flag not in _BROADCAST_BUTTONS):
        max_api.send_message(
            admin_id,
            "Использование: /broadcast <сегмент>[+btn|+link] <текст>\n\n"
            "Сегменты:\n"
            "all — всем, кто писал боту\n"
            "active — активным подписчикам\n"
            "inactive — оформляли подписку, но сейчас не продлена\n\n"
            "+btn — прикрепить кнопку «Оформить/продлить подписку» (например inactive+btn)\n"
            "+link — прикрепить кнопку «Получить ссылку в канал» (например active+link)",
        )
        return

    if not text:
        max_api.send_message(admin_id, "Текст рассылки не может быть пустым.")
        return

    recipients = _broadcast_recipients(segment)
    if not recipients:
        max_api.send_message(admin_id, "Получателей не найдено.")
        return

    _pending_broadcast[admin_id] = {"segment": segment, "text": text, "flag": flag or None}
    label = _BROADCAST_SEGMENTS[segment]
    button_note = f"\nБудет добавлена кнопка {_BROADCAST_BUTTONS[flag][0]}." if flag else ""
    max_api.send_message(
        admin_id,
        f"Получатели: {len(recipients)} ({label}){button_note}\n\n"
        f"Текст сообщения:\n{text}\n\n"
        "Отправить?",
        buttons=[
            [{"type": "callback", "text": "Отправить", "payload": "confirm_broadcast"}],
            [{"type": "callback", "text": "Отменить", "payload": "cancel_broadcast"}],
        ],
    )


def _handle_cancel_broadcast(admin_id: int) -> None:
    _pending_broadcast.pop(admin_id, None)
    max_api.send_message(admin_id, "Рассылка отменена.")


def _handle_confirm_broadcast(admin_id: int) -> None:
    pending = _pending_broadcast.pop(admin_id, None)
    if not pending:
        max_api.send_message(admin_id, "Нет ожидающей подтверждения рассылки.")
        return

    max_api.send_message(admin_id, "Рассылка запущена, пришлю отчёт по завершении.")
    threading.Thread(
        target=_run_broadcast,
        args=(admin_id, pending["segment"], pending["text"], pending["flag"]),
        daemon=True,
    ).start()


def _run_broadcast(admin_id: int, segment: str, text: str, flag: str | None) -> None:
    recipients = _broadcast_recipients(segment)
    buttons = [_BROADCAST_BUTTONS[flag][1]()] if flag else None
    sent = 0
    failed = 0
    for uid in recipients:
        try:
            max_api.send_message(uid, text, buttons=buttons)
            sent += 1
        except Exception:
            failed += 1
            log.warning("Рассылка: не удалось отправить user_id=%s", uid)
        time.sleep(0.35)

    log.info("Рассылка завершена: сегмент=%s успешно=%d ошибок=%d", segment, sent, failed)
    max_api.send_message(
        admin_id,
        f"Рассылка завершена.\nУспешно: {sent}\nОшибок: {failed}",
    )


def _handle_get_link(user_id: int) -> None:
    sub = db.get_active_subscription(user_id)
    if not sub:
        max_api.send_message(user_id, f"Активная подписка не найдена. Напишите /start.\n\n{_SUPPORT}")
        return

    # Сначала пробуем добавить через API — вдруг настройки изменились
    added = max_api.add_member_to_channel(config.MAX_CHANNEL_ID, user_id)
    if added:
        max_api.send_message(
            user_id,
            "Готово! Вы добавлены в канал. Откройте MAX — канал появился в ваших чатах.",
        )
        return

    # Иначе отправляем актуальную ссылку-приглашение
    invite_link = max_api.get_channel_invite_link(config.MAX_CHANNEL_ID)
    if invite_link:
        max_api.send_message(
            user_id,
            "Ваша персональная ссылка для вступления в канал:\n\n"
            f"{invite_link}"
            f"{_JOIN_HINT}\n\n"
            "Ссылка актуальна прямо сейчас. Если не сработает — нажмите кнопку ещё раз.",
        )
        log.info("get_link отправлена user_id=%s", user_id)
    else:
        max_api.send_message(user_id, f"Не удалось получить ссылку. Попробуйте позже.\n\n{_SUPPORT}")
        log.warning("get_link: не удалось получить ссылку для user_id=%s", user_id)


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
    total_count, user_count = db.get_subscription_counts(user_id)
    returning = f"\n♻️ {user_count}-я подписка этого пользователя" if user_count > 1 else ""
    email = sub.get("email", "—") if sub else "—"
    _notify_admin(
        f"Новая подписка #{total_count}!{returning}\n"
        f"{_user_label(user_id)}\n"
        f"Email: {email}\n"
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
                f"{invite_link}"
                f"{_JOIN_HINT}\n\n"
                "Чтобы в следующий раз (например, при продлении) доступ открывался "
                "автоматически: Настройки MAX → Конфиденциальность → "
                "Кто может добавлять в группы → Все\n\n"
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
            if user_id in config.PROTECTED_USER_IDS:
                log.info("Истечение: user_id=%s — защищённый пользователь, пропускаем кик", user_id)
                db.mark_expired(payment_id)
                continue

            if db.get_active_subscription(user_id):
                log.info(
                    "Истечение: user_id=%s уже продлил подписку — пропускаем кик, помечаем старую запись",
                    user_id,
                )
                db.mark_expired(payment_id)
                continue

            removed = max_api.remove_member_from_channel(channel_id, user_id)
            log.info("Удаление из канала: user_id=%s removed=%s", user_id, removed)

            # Помечаем истёкшей только после удаления — чтобы повторить если не вышло
            db.mark_expired(payment_id)

            period = f"{config.SUBSCRIPTION_MINUTES} мин." if config.SUBSCRIPTION_MINUTES else f"{config.SUBSCRIPTION_MONTHS} мес."
            max_api.send_message(
                user_id,
                "Ваша подписка на закрытый канал истекла. Вы были удалены из канала.\n\n"
                f"Чтобы возобновить доступ, оформите новую подписку на {period}.",
                buttons=[
                    [{"type": "callback", "text": f"Продлить за {config.SUBSCRIPTION_PRICE} руб.", "payload": "renew"}],
                    [_MARKET_BTN],
                ],
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
