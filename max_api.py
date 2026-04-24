import logging

import requests

import config

BASE_URL = "https://platform-api.max.ru"
log = logging.getLogger(__name__)


def _url(path: str) -> str:
    return f"{BASE_URL}{path}"


def _headers() -> dict:
    return {"Authorization": config.MAX_BOT_TOKEN}


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
        params={"user_id": user_id},
        headers=_headers(),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def add_member_to_channel(chat_id: int, user_id: int) -> bool:
    resp = requests.post(
        _url(f"/chats/{chat_id}/members"),
        headers=_headers(),
        json={"user_ids": [user_id]},
        timeout=10,
    )
    log.info("add_member status=%s body=%s", resp.status_code, resp.text)
    if not resp.ok:
        log.warning("add_member failed: %s %s", resp.status_code, resp.text)
        return False
    return True


def remove_member_from_channel(chat_id: int, user_id: int) -> bool:
    resp = requests.delete(
        _url(f"/chats/{chat_id}/members"),
        headers=_headers(),
        json={"user_id": user_id},
        timeout=10,
    )
    log.info("remove_member status=%s body=%s", resp.status_code, resp.text)
    if not resp.ok:
        log.warning("remove_member failed: %s %s", resp.status_code, resp.text)
        return False
    return True


def get_updates(marker: int | None = None, timeout: int = 30) -> dict:
    params: dict = {"timeout": timeout}
    if marker is not None:
        params["marker"] = marker
    resp = requests.get(
        _url("/updates"),
        params=params,
        headers=_headers(),
        timeout=timeout + 5,
    )
    resp.raise_for_status()
    return resp.json()
