from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from ..core.errors import PermanentSendError, PlatformUnsupportedError, RetryableSendError, UnknownSendResultError
from ..core.models import DeliveryTarget, RenderedMessage, SendResult
from ..core.security import validate_public_url

FEISHU_HARD_LIMIT = 30_720
FEISHU_MAX_ELEMENTS = 200


class CardSizeValidator:
    @staticmethod
    def request_body_size(card: dict[str, Any], receive_id: str, idempotency_key: str) -> int:
        body = {
            "receive_id": receive_id,
            "content": json.dumps(card, ensure_ascii=False),
            "msg_type": "interactive",
            "uuid": _stable_uuid(idempotency_key),
        }
        return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class PlatformSender:
    """Sends through AstrBot's stored session or the existing Lark adapter client."""

    def __init__(self, context: object, config: object | None = None) -> None:
        self.context = context
        self.config = config

    async def preflight(self, target: DeliveryTarget, message: RenderedMessage) -> None:
        """Check required local capabilities without calling any send operation."""
        is_lark = "feishu" in target.platform.lower() or "lark" in target.platform.lower()
        if is_lark and message.message_type in {"interactive", "file", "media"}:
            try:
                from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody  # noqa: F401
            except ImportError as exc:
                raise PlatformUnsupportedError("lark_oapi is unavailable in this AstrBot environment") from exc
            try:
                adapter = self.context.get_platform_inst(target.platform_instance)
            except Exception as exc:
                raise PlatformUnsupportedError("target Lark platform instance is unavailable") from exc
            client = getattr(adapter, "lark_api", None)
            if client is None or getattr(client, "im", None) is None:
                raise PlatformUnsupportedError("target Lark instance has no initialized IM client")
            if message.message_type == "interactive":
                card = message.payload.get("card")
                if not isinstance(card, dict) or card.get("schema") != "2.0":
                    raise PermanentSendError("Lark interactive payload is not Card JSON 2.0")
                elements = card.get("body", {}).get("elements", [])
                if len(elements) > FEISHU_MAX_ELEMENTS:
                    raise PermanentSendError("Lark card exceeds the component limit")
                receive_id, _ = _lark_recipient(target)
                key = _stable_uuid("preflight")
                hard_limit = min(
                    FEISHU_HARD_LIMIT, int(getattr(self.config, "feishu_max_card_bytes", FEISHU_HARD_LIMIT))
                )
                if CardSizeValidator.request_body_size(card, receive_id, key) > hard_limit:
                    raise PermanentSendError("Lark request body exceeds the hard byte limit")
            elif "image_key" in message.payload or not message.payload.get("file_key"):
                raise PermanentSendError("Lark file message requires file_key and cannot use image_key")
            return
        if message.message_type in {"interactive", "file"}:
            raise PlatformUnsupportedError(f"{message.message_type} messages require a platform-specific sender")
        if not callable(getattr(self.context, "send_message", None)):
            raise PlatformUnsupportedError("AstrBot context does not expose send_message")
        try:
            from astrbot.api.event import MessageChain  # noqa: F401
            from astrbot.api.message_components import Image, Plain, Video  # noqa: F401
        except ImportError as exc:
            raise PlatformUnsupportedError("AstrBot message components are unavailable") from exc
        _parts(message)

    async def send(self, target: DeliveryTarget, message: RenderedMessage, idempotency_key: str) -> SendResult:
        if "feishu" in target.platform.lower() or "lark" in target.platform.lower():
            if message.message_type in {"interactive", "file", "media"}:
                return await self._send_lark_card(target, message, idempotency_key)
        return await self._send_astrbot(target, message)

    async def _send_lark_card(
        self, target: DeliveryTarget, message: RenderedMessage, idempotency_key: str
    ) -> SendResult:
        try:
            from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        except ImportError as exc:
            raise PlatformUnsupportedError("lark_oapi is unavailable in this AstrBot environment") from exc
        try:
            adapter = self.context.get_platform_inst(target.platform_instance)
        except Exception as exc:
            raise PlatformUnsupportedError("target Lark platform instance is unavailable") from exc
        client = getattr(adapter, "lark_api", None)
        if client is None or getattr(client, "im", None) is None:
            raise PlatformUnsupportedError("Lark adapter does not expose an initialized IM client")
        card = None
        if message.message_type == "interactive":
            card = message.payload.get("card")
            if not isinstance(card, dict) or card.get("schema") != "2.0":
                raise PermanentSendError("Lark interactive payload is not Card JSON 2.0")
            msg_type = "interactive"
            content = json.dumps(card, ensure_ascii=False)
            elements = card.get("body", {}).get("elements", [])
            if len(elements) > FEISHU_MAX_ELEMENTS:
                raise PermanentSendError("Lark card exceeds the component limit")
        elif message.message_type in {"file", "media"}:
            if "image_key" in message.payload:
                raise PermanentSendError("Lark file message cannot use image_key")
            file_key = message.payload.get("file_key")
            if not file_key:
                raise PermanentSendError("Lark file message is missing file_key")
            msg_type = message.message_type
            content = json.dumps({"file_key": str(file_key)}, ensure_ascii=False)
        else:
            raise PlatformUnsupportedError(f"unsupported Lark message type: {message.message_type}")
        receive_id, receive_id_type = _lark_recipient(target)
        key = _stable_uuid(idempotency_key)
        if card is not None:
            hard_limit = min(FEISHU_HARD_LIMIT, int(getattr(self.config, "feishu_max_card_bytes", FEISHU_HARD_LIMIT)))
            if CardSizeValidator.request_body_size(card, receive_id, key) > hard_limit:
                raise PermanentSendError("Lark request body exceeds the hard byte limit")
        try:
            request = (
                CreateMessageRequest.builder()
                .receive_id_type(receive_id_type)
                .request_body(
                    CreateMessageRequestBody.builder()
                    .receive_id(receive_id)
                    .content(content)
                    .msg_type(msg_type)
                    .uuid(key)
                    .build()
                )
                .build()
            )
        except Exception as exc:
            raise PermanentSendError("failed to build Lark create-message request") from exc
        try:
            response = await client.im.v1.message.acreate(request)
        except asyncio.TimeoutError as exc:
            raise UnknownSendResultError("Lark send timed out; delivery result is unknown") from exc
        except Exception as exc:
            # Once the request coroutine is invoked the server may have accepted it.
            raise UnknownSendResultError("Lark send failed with an unknown result") from exc
        try:
            success = response.success()
        except Exception as exc:
            raise UnknownSendResultError("Lark response could not be interpreted") from exc
        if not success:
            status = getattr(response, "status_code", None)
            code = getattr(response, "code", None)
            detail = f"Lark rejected message (status={status}, code={code})"
            if status == 429 or isinstance(status, int) and status >= 500:
                raise RetryableSendError(detail)
            raise PermanentSendError(detail)
        data = getattr(response, "data", None)
        return SendResult(getattr(data, "message_id", None) if data else None)

    async def _send_astrbot(self, target: DeliveryTarget, message: RenderedMessage) -> SendResult:
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Image, Plain, Video
        except ImportError as exc:
            raise PlatformUnsupportedError("AstrBot message components are unavailable") from exc
        parts = _parts(message)
        components: list[Any] = []
        images_sent = 0
        send_as_media = bool(getattr(self.config, "send_as_media", True))
        image_upload = bool(getattr(self.config, "enable_image_upload", True))
        max_images = max(0, int(getattr(self.config, "max_images_per_message", 8)))
        media_fallback = str(getattr(self.config, "unsupported_media_fallback", "link"))
        for part in parts:
            kind = str(part.get("type", "text"))
            text = str(part.get("text", ""))
            if kind == "text":
                if text:
                    components.append(Plain(text))
                continue
            url = str(part.get("url", ""))
            if kind == "image" and url and send_as_media and image_upload and images_sent < max_images:
                try:
                    safe_url = (await validate_public_url(url)).url
                    components.append(Image.fromURL(safe_url))
                    images_sent += 1
                    continue
                except Exception:
                    pass
            if kind == "video" and url and send_as_media:
                try:
                    safe_url = (await validate_public_url(url)).url
                    components.append(Video.fromURL(safe_url))
                    continue
                except Exception:
                    pass
            if kind in {"image", "video", "audio", "attachment"} and media_fallback == "omit":
                continue
            if url:
                label = part.get("alt") or text or ("查看图片" if kind == "image" else kind.capitalize())
                components.append(Plain(f"[{label}]({url})"))
        if not components:
            raise PermanentSendError("rendered message is empty")
        chain = MessageChain(chain=components)
        try:
            matched = await self.context.send_message(target.unified_origin, chain)
        except asyncio.TimeoutError as exc:
            raise UnknownSendResultError("AstrBot send timed out; delivery result is unknown") from exc
        except Exception as exc:
            raise UnknownSendResultError("AstrBot send failed with an unknown result") from exc
        if matched is False:
            raise PlatformUnsupportedError("AstrBot found no platform matching the target session")
        # Context.send_message only reports whether a platform matched; it does
        # not provide a platform message ID.
        return SendResult(None)


def _parts(message: RenderedMessage) -> list[dict[str, Any]]:
    if message.message_type == "parts":
        return list(message.payload.get("parts", []))
    if message.message_type == "text":
        return [{"type": "text", "text": str(message.payload.get("text", ""))}]
    if message.message_type == "video":
        return [{"type": "video", "url": str(message.payload.get("url", "")), "alt": message.payload.get("alt", "")}]
    if message.message_type == "interactive":
        raise PlatformUnsupportedError("interactive messages require a platform-specific sender")
    return [{"type": "text", "text": json.dumps(message.payload, ensure_ascii=False)}]


def _lark_recipient(target: DeliveryTarget) -> tuple[str, str]:
    kind = str(getattr(target.session_type, "value", target.session_type)).lower().replace("_", "").replace(" ", "")
    if kind in {"group", "chat", "groupmessage"}:
        return target.session_id.rsplit("%", 1)[-1], "chat_id"
    return target.session_id, "open_id"


def _stable_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, value))
