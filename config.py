import os
from dotenv import load_dotenv

load_dotenv()

MAX_BOT_TOKEN: str = os.environ["MAX_BOT_TOKEN"]
MAX_CHANNEL_ID: int = int(os.environ["MAX_CHANNEL_ID"])
MAX_GROUP_ID: int | None = int(os.environ["MAX_GROUP_ID"]) if os.getenv("MAX_GROUP_ID") else None

YOOKASSA_SHOP_ID: str = os.environ["YOOKASSA_SHOP_ID"]
YOOKASSA_SECRET_KEY: str = os.environ["YOOKASSA_SECRET_KEY"]

SUBSCRIPTION_PRICE: str = os.getenv("SUBSCRIPTION_PRICE", "99.00")
SUBSCRIPTION_CURRENCY: str = os.getenv("SUBSCRIPTION_CURRENCY", "RUB")

# SUBSCRIPTION_MINUTES перекрывает SUBSCRIPTION_MONTHS — только для тестов
_minutes = os.getenv("SUBSCRIPTION_MINUTES")
SUBSCRIPTION_MINUTES: int | None = int(_minutes) if _minutes else None
SUBSCRIPTION_MONTHS: int = int(os.getenv("SUBSCRIPTION_MONTHS", "1"))

DATABASE_URL: str = os.environ.get("DATABASE_URL") or os.environ["DATABASE_PUBLIC_URL"]
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
ADMIN_USER_ID: int | None = int(os.environ["ADMIN_USER_ID"]) if os.getenv("ADMIN_USER_ID") else None

# Список всех защищённых от кика пользователей (включает ADMIN_USER_ID + ADMIN_USER_IDS)
_extra = [int(x) for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]
PROTECTED_USER_IDS: set[int] = ({ADMIN_USER_ID} if ADMIN_USER_ID else set()) | set(_extra)
