import uuid

import yookassa
from yookassa import Payment

import config


def configure() -> None:
    yookassa.Configuration.account_id = config.YOOKASSA_SHOP_ID
    yookassa.Configuration.secret_key = config.YOOKASSA_SECRET_KEY


def create_payment(user_id: int, email: str) -> dict:
    """Создаёт платёж в ЮKassa с фискальным чеком. Возвращает payment_id и ссылку."""
    description = _description()
    payment = Payment.create(
        {
            "amount": {
                "value": config.SUBSCRIPTION_PRICE,
                "currency": config.SUBSCRIPTION_CURRENCY,
            },
            "confirmation": {
                "type": "redirect",
                "return_url": "https://max.ru",
            },
            "capture": True,
            "description": description,
            "metadata": {"user_id": str(user_id)},
            "receipt": {
                "customer": {"email": email},
                "items": [
                    {
                        "description": description,
                        "quantity": "1.00",
                        "amount": {
                            "value": config.SUBSCRIPTION_PRICE,
                            "currency": config.SUBSCRIPTION_CURRENCY,
                        },
                        "vat_code": 1,          # без НДС
                        "payment_mode": "full_payment",
                        "payment_subject": "service",
                    }
                ],
            },
        },
        str(uuid.uuid4()),
    )
    return {
        "payment_id": payment.id,
        "confirmation_url": payment.confirmation.confirmation_url,
    }


def check_payment_status(payment_id: str) -> str:
    """Возвращает статус платежа: pending / waiting_for_capture / succeeded / canceled."""
    payment = Payment.find_one(payment_id)
    return payment.status


def _description() -> str:
    if config.SUBSCRIPTION_MINUTES is not None:
        return f"Тестовая подписка на {config.SUBSCRIPTION_MINUTES} мин."
    return f"Подписка на закрытый канал на {config.SUBSCRIPTION_MONTHS} мес."
