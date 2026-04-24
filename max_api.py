import logging

import requests

import config

BASE_URL = "https://platform-api.max.ru"
log = logging.getLogger(__name__)


def _url(path: str) -> str:
    return f"{BASE_URL}{path}"


def _params(extra: dict | None = None) -> dict:
    p = {"access_token": config.MAX_BOT_TOKEN}
    if extra:
        p.update(extra)
    return p


def send_message(user_id: int, text: str, buttons: list[list[dict]] | None = None) -> dict:
    body: dict = {"text": text}
    if buttons:
        body["attachments"] = [
            {
                "type": "inline_keyboard",
                "payload": {"buttons": buttons},
            }
        ]
    resp = requests.post(
        _url("/messages"),
        params=_params({"user_id": user_id}),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def add_member_to_channel(chat_id: int, user_id: int) -> bool:
    resp = requests.post(
        _url(f"/chats/{chat_id}/members"),
        params=_params(),
        json={"user_ids": [user_id]},
        timeout=10,
    )
    if not resp.ok:
        log.warning("add_member failed: %s %s", resp.status_code, resp.text)
        return False
    return True


def remove_member_from_channel(chat_id: int, user_id: int) -> bool:
    resp = requests.delete(
        _url(f"/chats/{chat_id}/members"),
        params=_params(),
        json={"user_id": user_id},
        timeout=10,
    )
    if not resp.ok:
        log.warning("remove_member failed: %s %s", resp.status_code, resp.text)
        return False
    return True


def get_updates(marker: int | None = None, timeout: int = 30) -> dict:
    params = _params({"timeout": timeout})
    if marker is not None:
        params["marker"] = marker
    resp = requests.get(
        _url("/updates"),
        params=params,
        timeout=timeout + 5,
    )
    resp.raise_for_status()
    return resp.json()
