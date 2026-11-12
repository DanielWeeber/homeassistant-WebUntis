"""Download links for the attachments of WebUntis messages ("Mitteilungen").

Attachments are served by an authenticated Home Assistant view. The sensor
attribute contains signed paths (authSig), so a logged-in user can open them
directly in the browser; nothing is stored in /local, www or the media folder.
"""

from __future__ import annotations

import logging
import mimetypes
from datetime import datetime, timedelta
from html import escape
from http import HTTPStatus
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import quote

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    MESSAGES_ATTACHMENT_URL_EXPIRY,
    MESSAGES_ATTACHMENT_URL_RENEW,
    MESSAGES_ATTACHMENT_VIEW_URL,
    MESSAGES_ATTACHMENTS_PLACEHOLDER,
)
from .utils.messages import download_attachment, fetch_attachments

if TYPE_CHECKING:
    from . import WebUntis

_LOGGER = logging.getLogger(__name__)


def attachments_path(entry_id: str, message_id) -> str:
    """Return the view path for all attachments of a message."""
    return MESSAGES_ATTACHMENT_VIEW_URL.format(
        entry_id=entry_id, message_id=quote(str(message_id), safe="")
    )


def attachment_path(entry_id: str, message_id, attachment_id: str) -> str:
    """Return the view path for one attachment."""
    return attachments_path(entry_id, message_id) + "/" + quote(attachment_id, safe="")


@callback
def async_signed_url(
    hass: HomeAssistant, cache: dict[str, tuple[str, datetime]], path: str
) -> str:
    """Return a signed path, re-signed only when it expires within a day.

    Keeping the URL stable avoids a new sensor state (and recorder row) every
    update.
    """
    now = dt_util.utcnow()
    cached = cache.get(path)
    if cached and cached[1] - now > timedelta(seconds=MESSAGES_ATTACHMENT_URL_RENEW):
        return cached[0]
    expiry = timedelta(seconds=MESSAGES_ATTACHMENT_URL_EXPIRY)
    url = async_sign_path(hass, path, expiry, use_content_user=True)
    cache[path] = (url, now + expiry)
    return url


@callback
def async_add_attachment_links(server: WebUntis) -> None:
    """Add the "attachments" list ({name, url}) to every known message."""
    cache = server.attachment_urls
    used: set[str] = set()
    entry_id = server.entry_id

    def sign(path: str) -> str:
        used.add(path)
        return async_signed_url(server.hass, cache, path)

    for message in server.messages:
        attachments = server.message_attachments.get(message["id"])
        if attachments is not None:
            message["attachments"] = [
                {
                    "name": attachment["name"],
                    "url": sign(
                        attachment_path(entry_id, message["id"], attachment["id"])
                    ),
                }
                for attachment in attachments
            ]
        elif message["has_attachments"]:
            # Not loaded yet (unread): the link loads them when it is opened
            message["attachments"] = [
                {
                    "name": MESSAGES_ATTACHMENTS_PLACEHOLDER,
                    "url": sign(attachments_path(entry_id, message["id"])),
                }
            ]
        else:
            message["attachments"] = []

    for path in set(cache) - used:
        cache.pop(path)


def _file_response(name: str, content: bytes) -> web.Response:
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return web.Response(
        body=content,
        content_type=content_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(name, safe='')}",
            "Cache-Control": "private, no-store",
        },
    )


def _list_response(
    server: WebUntis, message_id, attachments: list[dict]
) -> web.Response:
    items = "".join(
        '<li><a href="{}">{}</a></li>'.format(
            escape(
                async_signed_url(
                    server.hass,
                    server.attachment_urls,
                    attachment_path(server.entry_id, message_id, attachment["id"]),
                )
            ),
            escape(attachment["name"]),
        )
        for attachment in attachments
    )
    body = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<style>body{font-family:-apple-system,system-ui,sans-serif;font-size:18px;"
        "line-height:1.5;margin:16px}li{margin:12px 0}a{word-break:break-word}</style>"
        f"<title>WebUntis</title></head><body><ul>{items}</ul></body></html>"
    )
    return web.Response(
        text=body,
        content_type="text/html",
        headers={"Cache-Control": "private, no-store"},
    )


class WebUntisAttachmentView(HomeAssistantView):
    """Serve attachments of the messages known to a WebUntis config entry."""

    url = MESSAGES_ATTACHMENT_VIEW_URL
    extra_urls: ClassVar[list[str]] = [
        MESSAGES_ATTACHMENT_VIEW_URL + "/{attachment_id}"
    ]
    name = "api:webuntis:message_attachments"
    requires_auth = True

    async def get(
        self,
        request: web.Request,
        entry_id: str,
        message_id: str,
        attachment_id: str | None = None,
    ) -> web.Response:
        """Return one attachment, or all attachments of a message."""
        hass: HomeAssistant = request.app["hass"]
        entry = hass.config_entries.async_get_entry(entry_id)
        server: WebUntis | None = None
        if entry is not None and entry.domain == DOMAIN:
            server = hass.data.get(DOMAIN, {}).get(entry.unique_id)
        if server is None or not server.message_attachments_enabled:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Not found")

        # Only messages currently known for this entry: no generic proxy
        message = next((m for m in server.messages if str(m["id"]) == message_id), None)
        if message is None or not message["has_attachments"]:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Unknown message")
        message_key = message["id"]

        cached = server.message_attachments.get(message_key)
        if attachment_id is not None and (
            cached is None or not any(a["id"] == attachment_id for a in cached)
        ):
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Unknown attachment")

        try:
            attachments, content = await server.async_with_session(
                lambda: self._load(server, message_key, cached, attachment_id)
            )
        except Exception as err:
            _LOGGER.warning(
                "Loading attachment %s of message %s failed: %s",
                attachment_id or "list",
                message_key,
                err,
            )
            return web.Response(status=HTTPStatus.BAD_GATEWAY, text="WebUntis error")

        if attachments is None:
            return web.Response(status=HTTPStatus.BAD_GATEWAY, text="WebUntis error")
        server.message_attachments[message_key] = attachments
        if content is not None:
            return _file_response(content[0], content[1])
        if not attachments:
            return web.Response(status=HTTPStatus.NOT_FOUND, text="No attachments")
        return _list_response(server, message_key, attachments)

    @staticmethod
    def _load(server: WebUntis, message_id, cached, attachment_id):
        """Run in the executor: (attachment list, (name, bytes) or None)."""
        attachments = cached
        if attachments is None:
            attachments = fetch_attachments(server.session, [message_id]).get(
                message_id
            )
            if attachments is None:
                return None, None
        if attachment_id is not None:
            attachment = next(a for a in attachments if a["id"] == attachment_id)
        elif len(attachments) == 1:
            attachment = attachments[0]
        else:
            return attachments, None
        return attachments, (
            attachment["name"],
            download_attachment(server.session, attachment),
        )
