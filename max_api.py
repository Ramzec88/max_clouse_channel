import logging
import os

import certifi
import requests

import config

BASE_URL = "https://platform-api2.max.ru"
log = logging.getLogger(__name__)

_CERTS_DIR = os.path.join(os.path.dirname(__file__), "certs")
_RUSSIAN_ROOT = os.path.join(_CERTS_DIR, "russian_trusted_root_ca.pem")
_RUSSIAN_SUB = os.path.join(_CERTS_DIR, "russian_trusted_sub_ca.pem")
_COMBINED = os.path.join(_CERTS_DIR, "ca_bundle.pem")


def _build_ca_bundle() -> str:
    extras = [p for p in (_RUSSIAN_ROOT, _RUSSIAN_SUB) if os.path.exists(p)]
    if not extras:
        log.warning("Russian CA certs not found in certs/ — using default certifi bundle")
        return certifi.where()
    os.makedirs(_CERTS_DIR, exist_ok=True)
    with open(_COMBINED, "wb") as out:
        out.write(open(certifi.where(), "rb").read())
        for path in extras:
            out.write(open(path, "rb").read())
    log.info("CA bundle built: certifi + %d Russian CA(s) -> %s", len(extras), _COMBINED)
    return _COMBINED


_CA_BUNDLE = _build_ca_bundle()
_session = requests.Session()
_session.verify = _CA_BUNDLE


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
    resp = _session.post(
        _url("/messages"),
        params={"user_id": user_id},
        headers=_headers(),
        json=body,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def add_member_to_channel(chat_id: int, user_id: int) -> bool:
    resp = _session.post(
        _url(f"/chats/{chat_id}/members"),
        headers=_headers(),
        json={"user_ids": [user_id]},
        timeout=10,
    )
    log.info("add_member status=%s body=%s", resp.status_code, resp.text)
    if not resp.ok:
        log.warning("add_member http error: %s %s", resp.status_code, resp.text)
        return False
    data = resp.json()
    failed_ids = {str(x) for x in data.get("failed_user_ids", [])}
    if not data.get("success", True) or str(user_id) in failed_ids:
        details = data.get("failed_user_details", [])
        error_code = details[0].get("error_code") if details else "unknown"
        log.warning("add_member rejected by API: user_id=%s error=%s", user_id, error_code)
        return False
    return True


def remove_member_from_channel(chat_id: int, user_id: int) -> bool:
    resp = _session.delete(
        _url(f"/chats/{chat_id}/members"),
        params={"user_id": user_id},
        headers=_headers(),
        timeout=10,
    )
    log.info("remove_member status=%s body=%s", resp.status_code, resp.text)
    if not resp.ok:
        log.warning("remove_member failed: %s %s", resp.status_code, resp.text)
        return False
    return True


def get_channel_invite_link(chat_id: int) -> str | None:
    resp = _session.get(
        _url(f"/chats/{chat_id}"),
        headers=_headers(),
        timeout=10,
    )
    if not resp.ok:
        log.warning("get_channel_info failed: %s %s", resp.status_code, resp.text)
        return None
    return resp.json().get("link")


def get_updates(marker: int | None = None, timeout: int = 30) -> dict:
    params: dict = {"timeout": timeout}
    if marker is not None:
        params["marker"] = marker
    resp = _session.get(
        _url("/updates"),
        params=params,
        headers=_headers(),
        timeout=timeout + 5,
    )
    resp.raise_for_status()
    return resp.json()
