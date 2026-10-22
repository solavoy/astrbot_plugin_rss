from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_rss.core.errors import PlatformUnsupportedError, UnknownSendResultError
from astrbot_plugin_rss.core.models import Article, DeliveryTarget, RenderedMessage
from astrbot_plugin_rss.normalizers.html_content import normalize_html
from astrbot_plugin_rss.renderers.default import DefaultRenderer
from astrbot_plugin_rss.senders.platform import CardSizeValidator, PlatformSender, _lark_recipient


class FakeChain:
    def __init__(self, chain=None):
        self.chain = list(chain or [])


class FakePlain:
    def __init__(self, text):
        self.text = text


class FakeImage:
    @staticmethod
    def fromURL(url):
        return ("image", url)

    @staticmethod
    def fromFileSystem(path):
        return ("image", path)


class FakeVideo:
    @staticmethod
    def fromURL(url):
        return ("video", url)


@pytest.mark.asyncio
async def test_sender_uses_astrbot_context_and_does_not_invent_message_id(monkeypatch: pytest.MonkeyPatch) -> None:
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = FakeChain
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = FakePlain
    components.Image = FakeImage
    components.Video = FakeVideo
    api = types.ModuleType("astrbot.api")
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", components)

    class Context:
        async def send_message(self, origin, chain):
            assert origin == "origin"
            assert isinstance(chain.chain[0], FakePlain)
            return True

    sender = PlatformSender(Context())
    result = await sender.send(
        DeliveryTarget("t", "i", "origin", "other", "s"),
        RenderedMessage("default", "text", {"text": "hello"}, ["a"]),
        "key",
    )
    assert result.platform_message_id is None


@pytest.mark.asyncio
async def test_generic_sender_uses_official_components_without_download_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = FakeChain
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = FakePlain
    components.Image = FakeImage
    components.Video = FakeVideo
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", types.ModuleType("astrbot.api"))
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", components)
    import astrbot_plugin_rss.senders.platform as sender_module

    async def validate(url):
        if "localhost" in url or not url.startswith("https://"):
            raise ValueError("unsafe test URL")
        return SimpleNamespace(url=url)

    monkeypatch.setattr(sender_module, "validate_public_url", validate)

    class Context:
        def __init__(self):
            self.chains = []

        async def send_message(self, _origin, chain):
            self.chains.append(chain.chain)
            return True

    context = Context()
    config = SimpleNamespace(send_as_media=True, unsupported_media_fallback="link")
    sender = PlatformSender(context, config)
    target = DeliveryTarget("t", "i", "origin", "unknown-adapter", "s")
    parts = [
        {"type": "text", "text": "start"},
        {"type": "image", "url": "https://example.org/a.png", "alt": "image"},
        {"type": "video", "url": "https://example.org/a.mp4", "alt": "video"},
        {"type": "image", "url": "http://localhost/private.png", "alt": "private"},
        {"type": "video", "url": "javascript:alert(1)", "alt": "unsafe"},
    ]
    message = RenderedMessage("default", "parts", {"parts": parts}, ["a"])
    await sender.send(target, message, "key")
    chain = context.chains[-1]
    assert ("image", "https://example.org/a.png") in chain
    assert ("video", "https://example.org/a.mp4") in chain
    assert not any(isinstance(item, tuple) and item[1] == "http://localhost/private.png" for item in chain)
    assert any(isinstance(item, FakePlain) and "http://localhost/private.png" in item.text for item in chain)
    assert any(isinstance(item, FakePlain) and "javascript:alert(1)" in item.text for item in chain)

    config.send_as_media = False
    await sender.send(target, message, "key")
    chain = context.chains[-1]
    assert not any(isinstance(item, tuple) and item[0] in {"image", "video"} for item in chain)
    plain = "".join(item.text for item in chain if isinstance(item, FakePlain))
    assert "image" in plain and "video" in plain
    assert "http://localhost/private.png" in plain
    assert "javascript:alert(1)" in plain


@pytest.mark.asyncio
async def test_lark_default_video_operation_uses_video_component_without_plugin_size_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = FakeChain
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = FakePlain
    components.Image = FakeImage
    components.Video = FakeVideo
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", types.ModuleType("astrbot.api"))
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", components)

    class Context:
        def __init__(self):
            self.chains = []

        async def send_message(self, _origin, chain):
            self.chains.append(chain.chain)
            return True

    video_url = "https://truth-archive.us-iad-1.linodeobjects.com/attachments/18653/clip-27626KB.mp4"
    context = Context()
    sender = PlatformSender(context, SimpleNamespace(send_as_media=True))
    target = DeliveryTarget("t", "i", "origin", "feishu", "oc_chat")

    await sender.send(target, RenderedMessage("default", "video", {"url": video_url}, ["video"]), "key")

    assert context.chains == [[("video", video_url)]]


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_never_rewritten_to_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = FakeChain
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = FakePlain
    components.Image = FakeImage
    components.Video = FakeVideo
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", types.ModuleType("astrbot.api"))
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", components)

    class Context:
        async def send_message(self, origin, chain):
            raise TimeoutError()

    sender = PlatformSender(Context())
    with pytest.raises(UnknownSendResultError):
        await sender.send(
            DeliveryTarget("t", "i", "origin", "other", "s"),
            RenderedMessage("default", "text", {"text": "hello"}, ["a"]),
            "stable",
        )


def test_real_astrbot_group_message_enum_value_uses_chat_id() -> None:
    class GroupMessage:
        value = "GroupMessage"

    target = DeliveryTarget("t", "i", "o", "lark", "group%oc_chat", GroupMessage())
    assert _lark_recipient(target) == ("oc_chat", "chat_id")


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", ["file", "media"])
async def test_lark_file_and_video_sender_use_file_key_and_stable_uuid(
    monkeypatch: pytest.MonkeyPatch, message_type: str
) -> None:
    received = {}

    class Builder:
        def __init__(self):
            self.data = {}

        def receive_id_type(self, value):
            self.data["receive_id_type"] = value
            return self

        def request_body(self, value):
            self.data["body"] = value
            return self

        def receive_id(self, value):
            self.data["receive_id"] = value
            return self

        def content(self, value):
            self.data["content"] = value
            return self

        def msg_type(self, value):
            self.data["msg_type"] = value
            return self

        def uuid(self, value):
            self.data["uuid"] = value
            return self

        def build(self):
            return self

    class Request:
        @staticmethod
        def builder():
            return Builder()

    class Body(Request):
        pass

    module = types.ModuleType("lark_oapi.api.im.v1")
    module.CreateMessageRequest = Request
    module.CreateMessageRequestBody = Body
    for name in ("lark_oapi", "lark_oapi.api", "lark_oapi.api.im"):
        package = types.ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    monkeypatch.setitem(sys.modules, "lark_oapi.api.im.v1", module)

    class Response:
        data = types.SimpleNamespace(message_id="om_1")

        def success(self):
            return True

    class Client:
        im = types.SimpleNamespace(
            v1=types.SimpleNamespace(
                message=types.SimpleNamespace(acreate=lambda request: _capture(request, received, Response()))
            )
        )

    class Context:
        def get_platform_inst(self, instance):
            return types.SimpleNamespace(lark_api=Client())

    async def create_request(request):
        received.update(request.data)
        received.update(request.data["body"].data)
        return Response()

    async def _capture(request, store, response):
        store.update(request.data)
        return response

    # Rebind the lambda to an awaitable method without requiring the real SDK.
    Client.im.v1.message.acreate = create_request
    sender = PlatformSender(Context())
    result = await sender.send(
        DeliveryTarget("t", "i", "o", "lark", "group%oc_chat", "GroupMessage"),
        RenderedMessage("feishu", message_type, {"file_key": "file_key_1", "file_name": "v.mp4"}, ["a"]),
        "stable-operation-id",
    )
    assert result.platform_message_id == "om_1"
    assert received["msg_type"] == message_type
    assert received["receive_id"] == "oc_chat"
    assert json.loads(received["content"]) == {"file_key": "file_key_1"}
    assert len(received["uuid"]) == 36


def test_card_size_validator_counts_encoded_content_string_and_outer_json() -> None:
    card = {"schema": "2.0", "body": {"elements": [{"tag": "markdown", "content": "中文" * 100}]}}
    size = CardSizeValidator.request_body_size(card, "oc_chat", "stable")
    content = json.dumps(card, ensure_ascii=False)
    expected = len(
        json.dumps(
            {
                "receive_id": "oc_chat",
                "content": content,
                "msg_type": "interactive",
                "uuid": __import__("uuid").uuid5(__import__("uuid").NAMESPACE_URL, "stable").__str__(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert size == expected


@pytest.mark.asyncio
async def test_preflight_reports_unsupported_message_without_sending() -> None:
    class Context:
        def __init__(self):
            self.calls = 0

        async def send_message(self, *_args):
            self.calls += 1
            return True

    context = Context()
    sender = PlatformSender(context)
    with pytest.raises(PlatformUnsupportedError):
        await sender.preflight(
            DeliveryTarget("t", "i", "origin", "other", "s"),
            RenderedMessage("feishu", "interactive", {"card": {"schema": "2.0"}}, ["a"]),
        )
    assert context.calls == 0


@pytest.mark.asyncio
async def test_normalizer_default_renderer_and_sender_preserve_nested_content_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = types.ModuleType("astrbot.api.event")
    event.MessageChain = FakeChain
    components = types.ModuleType("astrbot.api.message_components")
    components.Plain = FakePlain
    components.Image = FakeImage
    components.Video = FakeVideo
    monkeypatch.setitem(sys.modules, "astrbot", types.ModuleType("astrbot"))
    monkeypatch.setitem(sys.modules, "astrbot.api", types.ModuleType("astrbot.api"))
    monkeypatch.setitem(sys.modules, "astrbot.api.event", event)
    monkeypatch.setitem(sys.modules, "astrbot.api.message_components", components)
    import astrbot_plugin_rss.senders.platform as sender_module

    async def validate(url):
        return SimpleNamespace(url=url)

    monkeypatch.setattr(sender_module, "validate_public_url", validate)
    blocks = normalize_html(
        '<p>Alpha <a href="https://example.org/link">linked label</a> then '
        '<img src="https://example.org/pic.png" alt="inline picture"> tail</p>'
        "<p>Second paragraph</p><blockquote><p>Quoted line</p></blockquote>"
        "<ul><li>List item</li></ul>",
        "https://example.org/feed",
    )
    article = Article("a", "f", "entry", "https://example.org/source", "Article", content_blocks=blocks)
    target = DeliveryTarget("t", "i", "origin", "other", "s")
    [message] = await DefaultRenderer().render([article], target, "Source")
    sent = {}

    class Context:
        async def send_message(self, _origin, chain):
            sent["chain"] = chain.chain
            return True

    await PlatformSender(Context()).send(target, message, "key")
    chain = sent["chain"]
    output = "".join(component.text for component in chain if isinstance(component, FakePlain))
    assert output.index("Alpha") < output.index("linked label") < output.index("https://example.org/link")
    assert output.index("https://example.org/link") < output.index(" then ")
    image_index = chain.index(("image", "https://example.org/pic.png"))
    assert (
        chain.index(next(item for item in chain if isinstance(item, FakePlain) and " then " in item.text)) < image_index
    )
    assert image_index < chain.index(
        next(item for item in chain if isinstance(item, FakePlain) and " tail" in item.text)
    )
    assert output.index("tail") < output.index("Second paragraph") < output.index("> Quoted line")
    assert output.index("> Quoted line") < output.index("• ") < output.index("List item")
