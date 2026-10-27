from astrbot_plugin_rss.core.models import Article, ContentBlock
from astrbot_plugin_rss.normalizers.markdown_content import (
    article_markdown,
    article_media,
    blocks_to_markdown,
    clean_cached_article,
    markdown_tokens,
)
from astrbot_plugin_rss.normalizers.media_content import is_pure_video_article


def test_legacy_block_serializer_is_the_only_path_for_old_tree_content():
    blocks = [
        ContentBlock(
            "heading",
            children=[ContentBlock("text", text="Legacy heading")],
            attrs={"level": 2},
        ),
        ContentBlock(
            "paragraph",
            children=[
                ContentBlock("strong", children=[ContentBlock("text", text="bold")]),
                ContentBlock("text", text=" then "),
                ContentBlock("link", url="https://example.org/help", children=[ContentBlock("text", "help")]),
            ],
        ),
        ContentBlock("list", children=[ContentBlock("list_item", children=[ContentBlock("text", "one")])]),
    ]

    markdown = blocks_to_markdown(blocks)
    assert "## Legacy heading" in markdown
    assert "**bold** then [help](https://example.org/help)" in markdown
    assert "- one" in markdown


def test_legacy_serializer_rejects_unsafe_urls_and_keeps_image_alt_text():
    blocks = [
        ContentBlock("image", url="javascript:alert(1)", attrs={"alt": "unsafe image"}),
        ContentBlock("link", url="data:text/html,bad", children=[ContentBlock("text", "unsafe link")]),
    ]

    markdown = blocks_to_markdown(blocks)
    assert "unsafe image" in markdown
    assert "unsafe link" in markdown
    assert "javascript:" not in markdown and "data:text" not in markdown


def test_article_media_reads_only_markdown_image_and_media_link_tokens():
    article = Article(
        "tokens",
        "feed",
        "entry",
        "https://example.org/article",
        "Article",
        content_blocks=[
            ContentBlock(
                "markdown",
                text=(
                    "Before `![ignored](https://cdn.example.org/ignored.png)` "
                    "![photo](https://cdn.example.org/photo.png) "
                    "[clip](https://cdn.example.org/clip.webm)"
                ),
            )
        ],
    )

    media = article_media(article)
    assert [(item.type, item.url) for item in media] == [
        ("image", "https://cdn.example.org/photo.png"),
        ("video", "https://cdn.example.org/clip.webm"),
    ]
    assert media[0].attrs["alt"] == "photo"
    assert [token.type for token in markdown_tokens(article_markdown(article))].count("inline") == 1


def test_pure_video_detection_ignores_associated_poster_but_not_real_body_content():
    video = "https://cdn.example.org/clip.mp4"
    poster = "https://cdn.example.org/poster.jpg"
    pure_video = Article(
        "video",
        "feed",
        "video-entry",
        "https://example.org/item",
        "Video",
        content_blocks=[
            ContentBlock(
                "markdown",
                text=f"[View video]({video})\n\n![poster]({poster})",
            )
        ],
        metadata={
            "media": [
                {"url": video, "medium": "video", "type": "video/mp4"},
                {"url": poster, "role": "thumbnail"},
            ]
        },
    )
    with_context = Article(
        "video-context",
        "feed",
        "video-context-entry",
        "https://example.org/item-2",
        "Video",
        content_blocks=[ContentBlock("markdown", text=f"A note\n\n[View video]({video})")],
        metadata=pure_video.metadata,
    )

    assert is_pure_video_article(pure_video)
    assert not is_pure_video_article(with_context)


def test_clean_cached_article_converts_legacy_tree_without_mutating_source_hash():
    article = Article(
        "old-cache",
        "feed",
        "entry",
        "https://example.org/item",
        "Old cache",
        content_hash="original-business-hash",
        content_blocks=[
            ContentBlock("paragraph", children=[ContentBlock("text", text="old body")]),
        ],
    )
    cleaned = clean_cached_article(article)

    assert cleaned is not article
    assert cleaned.content_hash == article.content_hash
    assert article.content_blocks[0].type == "paragraph"
    assert cleaned.content_blocks == [ContentBlock("markdown", text="old body")]
    assert clean_cached_article(cleaned) is cleaned
