import json
from types import SimpleNamespace

import pytest
from astrbot_plugin_rss.core.models import Article, ContentBlock
from astrbot_plugin_rss.renderers import telegram
from astrbot_plugin_rss.renderers.telegram import TELEGRAPH_CONTENT_LIMIT, TelegraphPublisher


def _walk_nodes(nodes):
    for node in nodes:
        if not isinstance(node, dict):
            continue
        yield node
        yield from _walk_nodes(node.get("children", []))


def _plain_nodes(nodes):
    result = []
    for node in nodes:
        if isinstance(node, str):
            result.append(node)
        elif isinstance(node, dict):
            result.extend(_plain_nodes(node.get("children", [])))
    return "".join(result)


def test_telegraph_maps_markdown_tokens_to_allowed_nodes_and_keeps_media_order():
    image_url = "https://cdn.example.org/photo.png"
    video_url = "https://cdn.example.org/video.mp4"
    markdown = (
        "# 标题\n\n"
        "**引流：**是 [帮助页面](https://example.org/help) 和 `**literal**`。\n\n"
        "1. 第一项\n2. 第二项\n\n"
        "| 名称 | 说明 |\n| --- | --- |\n| RSS | 内容 |\n\n"
        f"图片 ![封面]({image_url}) 之后是视频 [播放]({video_url})。\n\n"
        "```python\nvalue = '**literal**'\n```\n\n"
        "[危险](javascript:alert(1)) ![危险](data:image/png;base64,xx)"
    )
    nodes = telegram._telegraph_markdown_nodes(
        markdown,
        [ContentBlock("image", url=image_url), ContentBlock("video", url=video_url)],
    )

    all_nodes = list(_walk_nodes(nodes))
    assert nodes[0]["tag"] == "h3"
    assert any(node["tag"] == "strong" for node in all_nodes)
    strong_paragraph = next(
        node
        for node in nodes
        if node.get("tag") == "p"
        and any(isinstance(child, dict) and child.get("tag") == "strong" for child in node.get("children", []))
    )
    strong_index = next(
        i
        for i, child in enumerate(strong_paragraph["children"])
        if isinstance(child, dict) and child.get("tag") == "strong"
    )
    assert strong_paragraph["children"][strong_index + 1] == "是"
    assert {node["tag"] for node in all_nodes} >= {"ol", "li", "img", "video", "code", "pre", "a", "p"}
    assert _plain_nodes(nodes).index("第一项") < _plain_nodes(nodes).index("第二项")
    assert video_url not in _plain_nodes(nodes)
    assert image_url not in _plain_nodes(nodes)
    assert (
        len([node for node in all_nodes if node.get("tag") == "img" and node.get("attrs", {}).get("src") == image_url])
        == 1
    )
    assert (
        len(
            [node for node in all_nodes if node.get("tag") == "video" and node.get("attrs", {}).get("src") == video_url]
        )
        == 1
    )
    assert "**literal**" in _plain_nodes(nodes)
    assert not any(node.get("attrs", {}).get("href", "").startswith(("javascript:", "data:")) for node in all_nodes)
    assert all(node["tag"] in telegram._TELEGRAPH_TAGS for node in all_nodes)
    assert all(set(node.get("attrs", {})) <= {"href", "src"} for node in all_nodes)


def test_telegraph_cjk_strong_fix_does_not_touch_escaped_markdown_or_code():
    markdown = r"\*\*引流：\*\*是 `**literal：**是` https://example.org/**literal：**是"
    nodes = telegram._telegraph_markdown_nodes(markdown)
    all_nodes = list(_walk_nodes(nodes))

    assert not any(node.get("tag") == "strong" for node in all_nodes)
    assert "**literal：**是" in _plain_nodes(nodes)

    valid_with_url = telegram._telegraph_markdown_nodes("**引流：**是 https://example.org/**literal：**是")
    valid_tags = [node["tag"] for node in _walk_nodes(valid_with_url)]
    assert valid_tags.count("strong") == 1
    assert "https://example.org/**literal：**是" in _plain_nodes(valid_with_url)


@pytest.mark.asyncio
async def test_telegraph_clips_large_markdown_node_and_keeps_structure_and_source_footer(monkeypatch):
    article = Article(
        "long-article",
        "feed",
        "entry",
        "https://example.org/original",
        "Large article",
        content_blocks=[ContentBlock("markdown", text="正文前缀 **保留加粗** `literal **code**` " + "长" * 100_000)],
    )
    captured = {}

    def create_page(data):
        captured["payload"] = json.loads(data.decode("utf-8"))
        return {"ok": True, "result": {"url": "https://telegra.ph/page"}}

    monkeypatch.setattr(telegram, "_post_json", create_page)
    publisher = TelegraphPublisher(
        SimpleNamespace(
            telegraph_access_token="token",
            telegraph_cache_enabled=False,
            max_fetch_concurrency=1,
            timezone="Asia/Shanghai",
        )
    )
    try:
        await publisher.publish(article)
    finally:
        await publisher.close()

    content = captured["payload"]["content"]
    assert (
        len(json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= TELEGRAPH_CONTENT_LIMIT
    )
    assert content[-1]["tag"] == "p"
    assert content[-1]["children"][-1]["attrs"]["href"] == article.canonical_url
    body = content[1]
    assert body["tag"] == "p"
    assert "正文前缀 " in _plain_nodes([body])
    body_tags = [node["tag"] for node in _walk_nodes([body])]
    assert "strong" in body_tags
    assert "pre" not in body_tags
    assert any(node["tag"] == "code" and "literal **code**" in _plain_nodes([node]) for node in _walk_nodes([body]))
    assert all(node["tag"] in telegram._TELEGRAPH_TAGS for node in _walk_nodes(content))
