from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1].parent))

from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Article, ContentBlock, DeliveryTarget
from astrbot_plugin_rss.normalizers.html_content import normalize_html
from astrbot_plugin_rss.renderers.default import DefaultRenderer, _block_parts_many, _utf16_length, article_text
from astrbot_plugin_rss.renderers.feishu import HARD_LIMIT, FeishuRenderer
from astrbot_plugin_rss.renderers.registry import RendererRegistry
from astrbot_plugin_rss.renderers.telegram import TelegramRenderer, _telegraph_nodes


def article(article_id: str, blocks: list[ContentBlock], title: str = "一篇文章") -> Article:
    return Article(
        article_id,
        "feed",
        article_id,
        f"https://example.org/{article_id}",
        title,
        first_seen_at=None,
        content_blocks=blocks,
    )


def target(platform: str = "feishu", session_type: str = "group") -> DeliveryTarget:
    return DeliveryTarget("target", "instance", "platform:instance:group%oc_x", platform, "group%oc_x", session_type)


async def test_renderers_use_configured_timezone_and_timestamp_fallbacks(monkeypatch):
    timestamp = datetime(2026, 7, 1, 0, 0, tzinfo=UTC).timestamp()
    item = article("timezone", [ContentBlock("paragraph", "正文")], title="时区测试")
    item.author = "作者"
    item.published_at = timestamp
    item.updated_at = datetime(2026, 7, 2, 0, 0, tzinfo=UTC).timestamp()
    item.metadata["feed_title"] = "真实源"
    config = SimpleNamespace(
        timezone="America/Los_Angeles",
        send_as_media=True,
        enable_content_truncation=False,
        content_truncation_length=0,
        enable_default_renderer=True,
        telegraph_access_token="token",
        telegraph_author_name="",
        telegraph_cache_enabled=True,
        telegraph_fallback_enabled=True,
        max_fetch_concurrency=2,
    )
    feishu = FeishuRenderer(config)
    card = (await feishu.render([item], target(), "列表名"))[0].payload["card"]
    assert card["header"]["subtitle"]["content"] == "作者：作者 | 真实源 | 2026-06-30 17:00"

    default_messages = await DefaultRenderer(config).render([item], target("other"), "列表名")
    default_text = "".join(part.get("text", "") for part in default_messages[0].payload["parts"])
    assert "作者：作者 | 真实源 | 2026-06-30 17:00" in default_text
    assert "2026-07-02" not in default_text
    assert "2026-06-30 17:00" in article_text(item, "America/Los_Angeles")

    long_item = article("timezone-long", [ContentBlock("paragraph", "正文。" * 3000)])
    long_item.published_at = timestamp
    telegram_hard_fit = await DefaultRenderer(config).render([long_item], target("telegram"), "源")
    hard_fit_text = "".join(part.get("text", "") for part in telegram_hard_fit[0].payload["parts"])
    assert "2026-06-30 17:00" in hard_fit_text

    from astrbot_plugin_rss.renderers import telegram as telegram_module

    sent_payloads = []

    def create_page(data):
        sent_payloads.append(json.loads(data))
        return {"ok": True, "result": {"url": f"https://telegra.ph/page-{len(sent_payloads)}"}}

    monkeypatch.setattr(telegram_module, "_post_json", create_page)
    repo = type("Repo", (), {"get_telegraph_page": lambda *_: None, "set_telegraph_page": lambda *_: None})()
    telegram_renderer = TelegramRenderer(config, repo)
    try:
        telegram_messages = await telegram_renderer.render([item], target("telegram"), "列表名")
        assert "2026-06-30 17:00" in telegram_messages[0].payload["text"]
        assert sent_payloads[0]["content"][0]["children"] == ["作者：作者 | 真实源 | 2026-06-30 17:00"]
    finally:
        await telegram_renderer.close()

    fallback = article("fallback", [ContentBlock("paragraph", "正文")])
    fallback.updated_at = 0
    fallback.first_seen_at = 1234567890
    assert "1969-12-31 16:00" in article_text(fallback, "America/Los_Angeles")
    fallback.updated_at = None
    assert "2009-02-13 15:31" in article_text(fallback, "America/Los_Angeles")


async def test_feishu_card_v2_and_outer_json_size() -> None:
    renderer = FeishuRenderer(PluginConfig())
    messages = await renderer.render([article("a", [ContentBlock("paragraph", "你好，世界")])], target(), "测试源")
    assert len(messages) == 1
    message = messages[0]
    card = message.payload["card"]
    assert card["schema"] == "2.0"
    assert "elements" in card["body"]
    assert card["header"]["title"]["content"] == "一篇文章"
    assert card["header"]["subtitle"]["content"] == "作者：未知作者 | 测试源"
    body = "\n".join(e.get("content", "") for e in card["body"]["elements"])
    assert body.index("你好，世界") < body.index("原文地址")
    assert "原文地址：[https://example.org/a](https://example.org/a)" in body
    assert not any(e.get("tag") == "button" for e in card["body"]["elements"])
    assert FeishuRenderer.request_size(message, target(), "00000000-0000-0000-0000-000000000000") <= HARD_LIMIT
    assert json.loads(json.dumps(card, ensure_ascii=False))["schema"] == "2.0"


async def test_feishu_list_batches_complete_articles_and_single_article_fits() -> None:
    renderer = FeishuRenderer(PluginConfig())
    articles = [article(str(i), [ContentBlock("paragraph", "正文。" * 5000)]) for i in range(3)]
    messages = await renderer.render(articles, target(), "列表", is_list=True, batch_id="batch")
    assert len(messages) >= 1
    assert [aid for message in messages for aid in message.article_ids] == ["0", "1", "2"]
    for message in messages:
        assert FeishuRenderer.request_size(message, target(), "00000000-0000-0000-0000-000000000000") <= HARD_LIMIT
        assert message.payload["card"]["header"]["title"]["content"] in {"一篇文章"}
        assert any("原文地址" in elem.get("content", "") for elem in message.payload["card"]["body"]["elements"])


async def test_default_renderer_keeps_list_full_and_direct_truncation_uses_blocks() -> None:
    config = PluginConfig(enable_content_truncation=True, content_truncation_length=8)
    renderer = DefaultRenderer(config)
    item = article(
        "a",
        [
            ContentBlock("paragraph", "这是很长的第一段内容。"),
            ContentBlock("image", url="https://example.org/a.png", attrs={"alt": "图"}),
        ],
    )
    direct = await renderer.render([item], target("other"), "源", is_list=False)
    listed = await renderer.render([item], target("other"), "源", is_list=True)
    direct_parts = direct[0].payload["parts"]
    list_parts = listed[0].payload["parts"]
    direct_body = "".join(p.get("text", "") for p in direct_parts)
    listed_body = "".join(p.get("text", "") for p in list_parts)
    assert len(direct_body) < len(listed_body)
    assert any(p["type"] == "image" for p in list_parts)
    assert "作者：未知作者 | 源" in listed_body
    assert "原文地址：[https://example.org/a](https://example.org/a)" in listed_body
    assert "作者：未知作者 | 源\n\n这是很长的第一段内容。" in listed_body
    assert "原文地址：[https://example.org/a](https://example.org/a)" in article_text(item)


async def test_default_renderer_long_telegram_text_keeps_video_as_separate_operation():
    item = article(
        "long-video",
        [
            ContentBlock("paragraph", "正文内容。" * 1200),
            ContentBlock("quote", children=[ContentBlock("video", url="https://example.org/clip.mp4")]),
        ],
    )
    messages = await DefaultRenderer(PluginConfig()).render([item], target("telegram"), "源")
    assert [message.message_type for message in messages] == ["parts", "video"]
    body = "".join(part.get("text", "") for part in messages[0].payload["parts"])
    assert len(body.encode("utf-16-le")) // 2 <= 4096
    assert messages[1].payload["url"] == "https://example.org/clip.mp4"


def test_telegraph_uses_valid_node_tree_and_preserves_ordered_media() -> None:
    blocks = [
        ContentBlock("paragraph", "先文字"),
        ContentBlock("image", url="https://example.org/img.png"),
        ContentBlock("code", "print(1)"),
    ]
    nodes = _telegraph_nodes(blocks)
    assert [n["tag"] for n in nodes] == ["p", "img", "pre"]
    assert nodes[0]["children"] == ["先文字"]


def test_send_as_media_false_uses_links_for_default_feishu_and_telegraph():
    import asyncio

    item = article(
        "media-off",
        [
            ContentBlock("image", url="https://example.org/p.png", attrs={"alt": "图片"}),
            ContentBlock("video", url="https://example.org/v.mp4", attrs={"title": "视频"}),
        ],
    )
    config = PluginConfig(send_as_media=False)

    async def run():
        default_messages = await DefaultRenderer(config).render([item], target("other"), "源")
        card_messages = await FeishuRenderer(
            config,
            image_key=lambda *_: _unexpected_media_callback(),
            video_file=lambda *_: _unexpected_media_callback(),
        ).render([item], target(), "源")
        return default_messages, card_messages

    default_messages, card_messages = asyncio.run(run())
    assert len(default_messages) == 1
    assert all(part["type"] == "text" for part in default_messages[0].payload["parts"])
    assert "https://example.org/p.png" in "".join(part["text"] for part in default_messages[0].payload["parts"])
    card_body = "\n".join(item["content"] for item in card_messages[0].payload["card"]["body"]["elements"])
    assert "https://example.org/p.png" in card_body
    assert "https://example.org/v.mp4" in card_body
    telegraph = _telegraph_nodes(
        [
            ContentBlock("paragraph", children=[ContentBlock("image", url="https://example.org/p.png")]),
            ContentBlock("video", url="https://example.org/v.mp4"),
        ],
        send_as_media=False,
    )
    assert telegraph[0]["children"][0]["tag"] == "a"
    assert telegraph[1]["tag"] == "a"


async def _unexpected_media_callback():
    raise AssertionError("send_as_media=False must skip media download/upload")


def test_telegraph_converts_real_normalizer_nested_blocks_without_losing_order() -> None:
    blocks = normalize_html(
        '<p>先<a href="https://example.org/link">链接</a><img src="https://example.org/a.png" alt="图">后</p>'
        '<blockquote><p>引用</p><img src="https://example.org/q.png"></blockquote>',
        "https://example.org/feed",
    )
    nodes = _telegraph_nodes(blocks)
    assert nodes[0]["tag"] == "p"
    assert nodes[0]["children"] == [
        "先",
        {"tag": "a", "attrs": {"href": "https://example.org/link"}, "children": ["链接"]},
        {"tag": "img", "attrs": {"src": "https://example.org/a.png"}},
        "后",
    ]
    assert nodes[1]["tag"] == "blockquote"
    assert [n["tag"] for n in nodes[1]["children"]] == ["p", "img"]


def test_telegraph_flattens_nested_lightbox_children_in_both_media_modes():
    blocks = normalize_html(
        '<div><p>x</p><div class="lightbox-wrapper"><a class="lightbox" '
        'href="https://example.org/full.png"><img src="https://example.org/preview.png" alt="图"></a>'
        "</div></div>",
        "https://example.org/feed",
    )

    def validate_nodes(value):
        assert isinstance(value, list)
        flattened = []
        for node in value:
            assert isinstance(node, (str, dict))
            if isinstance(node, dict):
                assert isinstance(node.get("tag"), str)
                flattened.append(node)
                flattened.extend(validate_nodes(node.get("children", [])))
        return flattened

    embedded = _telegraph_nodes(blocks, send_as_media=True)
    linked = _telegraph_nodes(blocks, send_as_media=False)
    embedded_nodes = validate_nodes(embedded)
    linked_nodes = validate_nodes(linked)
    assert any(node.get("tag") == "img" for node in embedded_nodes)
    assert any(node.get("tag") == "a" for node in linked_nodes)


def test_telegraph_emits_only_documented_element_tags_and_no_text_tag() -> None:
    nodes = _telegraph_nodes(
        [
            ContentBlock(
                "paragraph",
                children=[
                    ContentBlock("link", url="https://example.org", children=[ContentBlock("text", "link")]),
                    ContentBlock("image", url="https://example.org/img.png"),
                ],
            )
        ]
    )
    allowed = {
        "a",
        "aside",
        "b",
        "blockquote",
        "br",
        "code",
        "em",
        "figcaption",
        "figure",
        "h3",
        "h4",
        "hr",
        "i",
        "iframe",
        "img",
        "li",
        "ol",
        "p",
        "pre",
        "s",
        "strong",
        "u",
        "ul",
        "video",
    }

    def check(value):
        if isinstance(value, str):
            return
        assert value["tag"] in allowed
        for child in value.get("children", []):
            check(child)

    for node in nodes:
        check(node)


async def test_direct_truncation_is_shared_and_nested_media_stays_ordered() -> None:
    config = PluginConfig(enable_content_truncation=True, content_truncation_length=8)
    nested = ContentBlock(
        "paragraph",
        children=[
            ContentBlock("text", "开头这是一段很长的文字"),
            ContentBlock("image", url="https://example.org/p.png", attrs={"alt": "段中图片"}),
            ContentBlock("text", "末尾文字"),
        ],
    )
    item = article("nested", [nested])
    feishu = FeishuRenderer(config)
    direct = await feishu.render([item], target(), "源", is_list=False)
    listed = await feishu.render([item], target(), "源", is_list=True)
    direct_md = "\n".join(e.get("content", "") for e in direct[0].payload["card"]["body"]["elements"])
    listed_md = "\n".join(e.get("content", "") for e in listed[0].payload["card"]["body"]["elements"])
    assert len(direct_md) < len(listed_md)
    assert "段中图片" in direct_md
    parts = _block_parts_many(nested)
    media_index = next(i for i, part in enumerate(parts) if part["type"] == "image")
    assert [p["type"] for p in parts[:media_index]] == ["text"]
    assert [p["type"] for p in parts[media_index + 1 :]] == ["text", "text"]
    assert parts[media_index]["url"] == "https://example.org/p.png"


async def test_telegram_hard_fit_clips_nested_paragraph_within_4096() -> None:
    long_block = ContentBlock("paragraph", children=[ContentBlock("text", "很长的正文内容" * 1000)])
    item = article("long", [long_block])
    messages = await TelegramRenderer().render([item], target("telegram"), "源")
    body = "".join(part.get("text", "") for part in messages[0].payload["parts"])
    assert len(body) <= 4096
    assert "…" in body
    assert item.canonical_url in body
    assert f"原文地址：[{item.canonical_url}]({item.canonical_url})" in body


async def test_telegram_emoji_hard_limit_uses_utf16_units() -> None:
    long_block = ContentBlock("paragraph", children=[ContentBlock("text", "😀" * 5000)])
    item = article("emoji", [long_block], title="Emoji article")
    messages = await TelegramRenderer().render([item], target("telegram"), "Telegram source")
    body = "".join(part.get("text", "") for part in messages[0].payload["parts"])
    assert _utf16_length(body) <= 4096
    assert "…" in body
    assert item.canonical_url in body
    assert f"原文地址：[{item.canonical_url}]({item.canonical_url})" in body
    assert body.encode("utf-16-le").decode("utf-16-le") == body


async def test_feishu_truncated_card_retains_uploaded_image_key() -> None:
    async def image_key(_target, url, _alt):
        return f"img-key:{url.rsplit('/', 1)[-1]}"

    config = PluginConfig(
        max_images_per_message=1,
        feishu_max_card_bytes=900,
    )
    renderer = FeishuRenderer(config, image_key=image_key)
    item = article(
        "image",
        [
            ContentBlock("image", url="https://example.org/p.png", attrs={"alt": "picture"}),
            ContentBlock("paragraph", "很长的正文" * 500),
        ],
    )
    [message] = await renderer.render([item], target(), "超长飞书卡片")
    elements = message.payload["card"]["body"]["elements"]
    assert any(element.get("tag") == "img" and element.get("img_key") == "img-key:p.png" for element in elements)
    assert all("https://example.org/p.png" not in element.get("content", "") for element in elements)
    assert FeishuRenderer.request_size(message, target()) <= config.feishu_max_card_bytes


async def test_feishu_embeds_uploaded_discourse_images_without_wrapping_links() -> None:
    from astrbot_plugin_rss.normalizers.discourse import normalize_image_links

    original_urls = [
        "https://forum.example/uploads/default/original/2X/first.png",
        "https://forum.example/uploads/default/original/2X/second.jpg",
        "https://forum.example/uploads/default/original/2X/third.png",
    ]
    blocks = normalize_image_links(
        normalize_html(
            '<p>前<img src="/uploads/default/original/2X/first.png" alt="第一图">中</p>'
            '<p><a href="/uploads/default/original/2X/second.jpg">image</a></p>'
            '<p>再<a class="lightbox-wrapper" href="/uploads/default/original/2X/third.png">'
            '<img src="/uploads/default/optimized/2X/third.png" alt="第三图"></a>后</p>'
            '<p><a href="https://example.org/article/1">普通文章</a></p>',
            "https://forum.example/t/topic/3048407",
        )
    )
    blocks.append(
        ContentBlock(
            "link",
            url="https://forum.example/uploads/default/original/2X/legacy.png",
            children=[
                ContentBlock(
                    "image",
                    url="https://forum.example/uploads/default/original/2X/legacy.png",
                    attrs={"alt": "旧缓存图"},
                )
            ],
        )
    )
    item = article("discourse-images", blocks)
    item.metadata["source_adapter"] = "discourse"
    item.metadata["feed_title"] = "Forum"
    uploaded = []

    async def image_key(_target, url, _alt):
        uploaded.append(url)
        return f"image-key:{url.rsplit('/', 1)[-1]}"

    [message] = await FeishuRenderer(image_key=image_key).render([item], target(), "List title")
    elements = message.payload["card"]["body"]["elements"]
    body = "\n".join(element.get("content", "") for element in elements)
    images = [element for element in elements if element.get("tag") == "img"]
    assert uploaded == original_urls + ["https://forum.example/uploads/default/original/2X/legacy.png"]
    assert [image["img_key"] for image in images] == [
        "image-key:first.png",
        "image-key:second.jpg",
        "image-key:third.png",
        "image-key:legacy.png",
    ]
    assert [image["alt"]["content"] for image in images] == ["第一图", "image", "第三图", "旧缓存图"]
    assert all(url not in body for url in original_urls)
    assert elements.index(next(e for e in elements if "前" in e.get("content", ""))) < elements.index(images[0])
    assert elements.index(images[0]) < elements.index(next(e for e in elements if "中" in e.get("content", "")))
    assert body.index("普通文章") < body.index("https://example.org/article/1")


async def test_feishu_image_upload_failure_keeps_one_chinese_source_link() -> None:
    async def unavailable(_target, _url, _alt):
        return None

    item = article("image-failure", [ContentBlock("image", url="https://example.org/p.png", attrs={"alt": "图片一"})])
    [message] = await FeishuRenderer(image_key=unavailable).render([item], target(), "源")
    body = "\n".join(element.get("content", "") for element in message.payload["card"]["body"]["elements"])
    assert body.count("https://example.org/p.png") == 1
    assert "[图片：图片一](https://example.org/p.png)" in body


async def test_feishu_generic_anchor_image_upload_preserves_article_href_and_text_order() -> None:
    item = article(
        "generic-anchor-image",
        normalize_html(
            '<p>图片前<a href="https://example.org/article/1">'
            '<img src="https://cdn.example.org/picture.jpg" alt="封面">更多</a>图片后</p>'
            '<p><a href="https://example.org/image">image</a></p>'
        ),
    )
    item.metadata["source_adapter"] = "generic"
    uploads = []

    async def image_key(_target, url, _alt):
        uploads.append(url)
        return "image-key:cover"

    [message] = await FeishuRenderer(image_key=image_key).render([item], target(), "源")
    elements = message.payload["card"]["body"]["elements"]
    body = "\n".join(element.get("content", "") for element in elements)
    assert uploads == ["https://cdn.example.org/picture.jpg"]
    image_index = next(i for i, element in enumerate(elements) if element.get("tag") == "img")
    assert elements[image_index]["img_key"] == "image-key:cover"
    assert "[相关链接](https://example.org/article/1)" in body
    assert body.count("更多") == 1
    assert "https://cdn.example.org/picture.jpg" not in body
    assert "[image](https://example.org/image)" in body
    assert next(i for i, e in enumerate(elements) if "图片前" in e.get("content", "")) < image_index
    assert image_index < next(i for i, e in enumerate(elements) if "更多" in e.get("content", ""))
    after_image = next(e["content"] for e in elements[image_index + 1 :] if e.get("tag") == "markdown")
    assert "更多" in after_image and after_image.index("更多") < after_image.index("图片后")


async def test_feishu_native_image_size_preserves_emoji_and_avoids_upscaling() -> None:
    sizes_requested = []

    async def image_key(_target, url, _alt):
        return f"key:{url.rsplit('/', 1)[-1]}"

    async def image_size(_target, url):
        sizes_requested.append(url)
        return {
            "tiny.png": (12, 10),
            "rect-emoji.png": (40, 20),
            "small.png": (80, 60),
            "width-only.png": (80, 60),
            "large-hint.png": (80, 60),
            "square-hint.png": (80, 60),
            "large.png": (1200, 600),
            "skinny.png": (1, 1000),
            "unknown.png": None,
        }[url.rsplit("/", 1)[-1]]

    item = article(
        "native-image-sizing",
        normalize_html(
            '<p>before<img class="emoji" src="https://example.org/tiny.png" alt=":lion:">'
            '<img class="emoji" src="https://example.org/rect-emoji.png" alt="rectangle">'
            '<img src="https://example.org/small.png" width="80" height="60" alt="small">'
            '<img src="https://example.org/width-only.png" width="40" alt="width only">'
            '<img src="https://example.org/large-hint.png" width="800" height="600" alt="large hint">'
            '<img src="https://example.org/square-hint.png" width="40" height="40" alt="square hint">'
            '<img src="https://example.org/large.png" alt="large">'
            '<img src="https://example.org/skinny.png" height="1" alt="skinny">'
            '<img class="emoji" src="https://example.org/unknown.png" alt="unknown">after</p>'
        ),
    )
    [message] = await FeishuRenderer(
        PluginConfig(max_images_per_message=10), image_key=image_key, image_size=image_size
    ).render([item], target(), "源")
    elements = message.payload["card"]["body"]["elements"]
    images = [element for element in elements if element.get("tag") == "img"]

    assert [image["size"] for image in images] == [
        "12px 10px",
        "20px 10px",
        "80px 60px",
        "40px 30px",
        "80px 60px",
        "40px 30px",
        "580px 290px",
        "1px 1px",
        "20px 20px",
    ]
    assert sizes_requested == [
        "https://example.org/tiny.png",
        "https://example.org/rect-emoji.png",
        "https://example.org/small.png",
        "https://example.org/width-only.png",
        "https://example.org/large-hint.png",
        "https://example.org/square-hint.png",
        "https://example.org/large.png",
        "https://example.org/skinny.png",
        "https://example.org/unknown.png",
    ]
    assert all(image["transparent"] is True for image in images)
    assert elements[0]["content"] == "before"
    assert elements[-2]["content"] == "after"
    assert FeishuRenderer.request_size(message, target()) <= HARD_LIMIT


def test_feishu_thin_image_dimensions_floor_only_after_scaling():
    from astrbot_plugin_rss.renderers.feishu import _display_size

    assert _display_size((1, 1000), emoji=False, height_hint=1) == "1px 1px"


async def test_feishu_uses_link_children_and_renders_list_articles_individually() -> None:
    renderer = FeishuRenderer(PluginConfig())
    items = [
        article(
            str(i),
            [ContentBlock("link", url=f"https://example.org/{i}", children=[ContentBlock("text", f"有意义的链接{i}")])],
        )
        for i in range(2)
    ]
    messages = await renderer.render(items, target(), "源", is_list=True)
    assert len(messages) == 2
    assert [m.article_ids for m in messages] == [["0"], ["1"]]
    assert "有意义的链接0" in " ".join(e.get("content", "") for e in messages[0].payload["card"]["body"]["elements"])


async def test_feishu_list_articles_keep_independent_titles_and_real_feed_titles() -> None:
    first = article("first", [ContentBlock("paragraph", "第一篇")], title="标题一")
    second = article("second", [ContentBlock("paragraph", "第二篇")], title="标题二")
    first.metadata["feed_title"] = "真实 RSS 源"
    renderer = FeishuRenderer(PluginConfig())
    messages = await renderer.render([first, second], target(), "列表名", is_list=True)
    assert [message.payload["card"]["header"]["title"]["content"] for message in messages] == ["标题一", "标题二"]
    assert messages[0].payload["card"]["header"]["subtitle"]["content"] == "作者：未知作者 | 真实 RSS 源"
    assert "列表名" in messages[1].payload["card"]["header"]["subtitle"]["content"]


async def test_telegraph_cache_uses_presentation_hash(monkeypatch) -> None:
    from astrbot_plugin_rss.renderers import telegram as telegram_module

    class Repository:
        def __init__(self):
            self.values = {("cached", "old-business-hash"): "https://telegra.ph/old"}
            self.lookups = []

        def get_telegraph_page(self, article_id, content_hash):
            self.lookups.append((article_id, content_hash))
            return self.values.get((article_id, content_hash))

        def set_telegraph_page(self, article_id, content_hash, url, _created):
            self.values[(article_id, content_hash)] = url

    calls = []
    payloads = []

    def create(data):
        calls.append(True)
        payloads.append(json.loads(data))
        return {"ok": True, "result": {"url": "https://telegra.ph/new"}}

    monkeypatch.setattr(telegram_module, "_post_json", create)
    repo = Repository()
    from astrbot_plugin_rss.renderers.telegram import TelegraphPublisher

    item = article("cached", [ContentBlock("paragraph", "正文")])
    item.content_hash = "old-business-hash"
    item.author = "作者甲"
    item.published_at = 1792382400
    item.metadata["feed_title"] = "真实 RSS 源"
    publisher = TelegraphPublisher(type("Config", (), {"telegraph_access_token": "token"})(), repo)
    assert await publisher.publish(item) == "https://telegra.ph/new"
    key = repo.lookups[-1][1]
    assert key != item.content_hash
    assert payloads[0]["title"] == item.title
    assert payloads[0]["content"][0]["children"][0] == "作者：作者甲 | 真实 RSS 源 | 2026-10-19 12:00"
    footer = payloads[0]["content"][-1]
    assert footer["children"][0] == "原文地址："
    assert footer["children"][1]["children"] == [item.canonical_url]
    assert await publisher.publish(item) == "https://telegra.ph/new"
    assert len(calls) == 1


async def test_telegraph_cache_key_changes_with_timezone(monkeypatch):
    from astrbot_plugin_rss.renderers import telegram as telegram_module
    from astrbot_plugin_rss.renderers.telegram import TelegraphPublisher

    class Repository:
        def __init__(self):
            self.pages = {}
            self.keys = []

        def get_telegraph_page(self, article_id, content_hash):
            self.keys.append((article_id, content_hash))
            return self.pages.get((article_id, content_hash))

        def set_telegraph_page(self, article_id, content_hash, url, _created):
            self.pages[(article_id, content_hash)] = url

    calls = []
    payloads = []

    def create(data):
        payloads.append(json.loads(data))
        calls.append(len(calls) + 1)
        return {"ok": True, "result": {"url": f"https://telegra.ph/page-{len(calls)}"}}

    monkeypatch.setattr(telegram_module, "_post_json", create)
    repo = Repository()
    item = article("timezone-cache", [ContentBlock("paragraph", "正文")])
    item.published_at = datetime(2026, 7, 1, 0, 0, tzinfo=UTC).timestamp()
    config = SimpleNamespace(telegraph_access_token="token", timezone="Asia/Shanghai", max_fetch_concurrency=1)
    publisher = TelegraphPublisher(config, repo)
    try:
        assert await publisher.publish(item) == "https://telegra.ph/page-1"
        first_key = repo.keys[-1][1]
        assert payloads[0]["content"][0]["children"] == ["作者：未知作者 | 2026-07-01 08:00"]
        config.timezone = "America/Los_Angeles"
        assert await publisher.publish(item) == "https://telegra.ph/page-2"
        second_key = repo.keys[-1][1]
        assert second_key != first_key
        assert payloads[1]["content"][0]["children"] == ["作者：未知作者 | 2026-06-30 17:00"]
        assert await publisher.publish(item) == "https://telegra.ph/page-2"
        assert len(calls) == 2
    finally:
        await publisher.close()


async def test_feishu_card_precedes_separate_video_media_operation() -> None:
    async def upload(_target, url):
        return {"file_key": f"key:{url}", "file_name": "clip.mp4", "file_type": "mp4"}

    config = PluginConfig()
    renderer = FeishuRenderer(config, video_file=upload)
    item = article(
        "video",
        [
            ContentBlock(
                "paragraph",
                children=[ContentBlock("text", "视频："), ContentBlock("video", url="https://example.org/clip.mp4")],
            )
        ],
    )
    messages = await renderer.render([item], target(), "源")
    assert [message.message_type for message in messages] == ["interactive", "media"]
    assert messages[0].payload["card"]["body"]["elements"][-1]["content"].startswith("原文地址：")
    assert messages[1].payload["file_key"] == "key:https://example.org/clip.mp4"
    assert messages[1].payload["source_url"] == "https://example.org/clip.mp4"
    assert messages[1].article_ids == ["video"]


async def test_feishu_registry_routes_pure_video_through_default_sender_in_batch_order() -> None:
    video_url = "https://truth-archive.us-iad-1.linodeobjects.com/attachments/18653/clip.mp4"
    poster_url = "https://truth-archive.us-iad-1.linodeobjects.com/attachments/18653/poster.jpg"
    pure_video = Article(
        "pure-video",
        "feed",
        "video-entry",
        "https://trumpstruth.org/entry/video",
        "Video post",
        first_seen_at=None,
        content_blocks=[
            ContentBlock("paragraph"),
            ContentBlock(
                "paragraph",
                children=[ContentBlock("link", url=video_url, children=[ContentBlock("text", "View video")])],
            ),
            ContentBlock("video", url=video_url),
            ContentBlock("image", url=poster_url, attrs={"role": "thumbnail"}),
        ],
        metadata={
            "media": [
                {"url": video_url, "medium": "video", "type": "video/mp4"},
                {"url": poster_url, "role": "thumbnail"},
            ]
        },
    )
    ordinary = article("ordinary", [ContentBlock("paragraph", "Body text")])

    class TrackingDownloader:
        def __init__(self):
            self.calls = []

        async def download(self, *args, **kwargs):
            self.calls.append((args, kwargs))
            raise AssertionError("pure video must use AstrBot's Video.fromURL path")

    downloader = TrackingDownloader()
    client = SimpleNamespace(config=SimpleNamespace(app_id="app"), im=SimpleNamespace())
    context = SimpleNamespace(get_platform_inst=lambda _instance: SimpleNamespace(lark_api=client))
    registry = RendererRegistry(PluginConfig(timezone="UTC"), context=context, media_downloader=downloader)
    try:
        messages = await registry.render([pure_video, ordinary], target(), "Truth")
    finally:
        await registry.close()

    assert [message.message_type for message in messages] == ["parts", "video", "interactive"]
    assert [message.article_ids for message in messages] == [["pure-video"], ["pure-video"], ["ordinary"]]
    assert messages[1].payload["url"] == video_url
    assert downloader.calls == []
