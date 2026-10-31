import sys
from pathlib import Path

import pytest
from astrbot_plugin_rss.core.models import Article, ContentBlock, FetchResult
from astrbot_plugin_rss.normalizers.markdown_content import (
    article_markdown,
    article_media,
    clean_cached_article,
    html_to_markdown,
    markdown_tokens,
)
from astrbot_plugin_rss.normalizers.media_content import is_pure_video_article
from astrbot_plugin_rss.parsers.feed_parser import FeedParseError, FeedParser, _clean_link

FIXTURES = Path(__file__).parent / "fixtures"


def result(name: str) -> FetchResult:
    return FetchResult(
        "feed-1",
        "https://linux.do/c/feedback/2.rss",
        "https://linux.do/c/feedback/2.rss",
        200,
        "application/rss+xml",
        (FIXTURES / name).read_bytes(),
    )


def parse_xml(xml: bytes, feed_url: str = "https://example.org/feed"):
    return FeedParser().parse(FetchResult("f", feed_url, feed_url, 200, "application/rss+xml", xml))


def body(article: Article) -> str:
    assert len(article.content_blocks) == 1
    assert article.content_blocks[0].type == "markdown"
    assert article.content_blocks[0].children == []
    return article_markdown(article)


def token_types(markdown: str) -> list[str]:
    return [token.type for token in markdown_tokens(markdown)]


def inline_tokens(markdown: str):
    for token in markdown_tokens(markdown):
        if token.type == "inline":
            yield from token.children or []


def test_discourse_feed_is_stored_as_one_ordered_markdown_body():
    parsed = FeedParser().parse(result("discourse_feedback.xml"))
    article = parsed.articles[0]
    markdown = body(article)

    assert parsed.metadata["source_adapter"] == "discourse"
    assert article.title == "测试反馈 😀"
    assert article.author == "作者"
    assert article.canonical_url == "https://linux.do/t/topic/42/1"
    assert article.published_at == 1792382400.0
    assert "[帮助](https://linux.do/help)" in markdown
    assert "> 引用内容" in markdown
    assert "![图片](https://linux.do/uploads/photo.png)" in markdown
    assert "```" in markdown and 'print("rss")' in markdown
    assert "- 第一项" in markdown and "- 第二项" in markdown
    assert [media.url for media in article_media(article)] == ["https://linux.do/uploads/photo.png"]


def test_atom_relative_link_date_and_plain_text_are_normalized():
    parsed = FeedParser().parse(result("atom_sample.xml"))
    article = parsed.articles[0]

    assert article.canonical_url == "https://linux.do/entry/1"
    assert article.published_at == 1792368000.0
    assert body(article) == "Summary"
    assert article.metadata["source_adapter"] == "generic"


def test_rss10_rdf_and_rsshub_generator_adapter_are_preserved():
    rdf = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel rdf:about="https://example.org/"><title>Example</title><link>https://example.org/</link>
      <description>RDF channel</description><items><rdf:Seq><rdf:li rdf:resource="https://example.org/a"/></rdf:Seq></items></channel>
      <item rdf:about="https://example.org/a"><title>RDF item</title><link>https://example.org/a</link><dc:creator>Writer</dc:creator></item>
      </rdf:RDF>"""
    rsshub = b"""<rss version="2.0"><channel><title>Hub</title><link>https://rsshub.example/</link>
      <description>Generated</description><generator>RSSHub</generator><item><guid>hub-1</guid><title>Entry</title>
      <link>https://rsshub.example/item/1</link><description>Body</description></item></channel></rss>"""
    parsed_rdf = parse_xml(rdf, "https://example.org/rss")
    parsed_hub = parse_xml(rsshub, "https://rsshub.example/feed")

    assert parsed_rdf.articles[0].title == "RDF item"
    assert parsed_rdf.articles[0].author == "Writer"
    assert parsed_hub.metadata["source_adapter"] == "rsshub"
    assert parsed_hub.articles[0].canonical_url == "https://rsshub.example/item/1"
    assert body(parsed_hub.articles[0]) == "Body"


@pytest.mark.parametrize(
    "xml",
    [
        b"<html><body>not a feed</body></html>",
        b"<!DOCTYPE rss [<!ENTITY x 'x'>]><rss><channel/></rss>",
        b"<rss><channel>",
    ],
)
def test_rejects_unsafe_or_invalid_feed_xml(xml):
    with pytest.raises(FeedParseError):
        parse_xml(xml)


def test_markdownify_keeps_commonmark_semantics_and_drops_active_html():
    html = (
        "<h1>Release</h1><p>A <strong>bold <b>again</b> <em>italic</em></strong> end; "
        '<a href="../help">help</a> <code>&lt;x&gt;</code>.</p>'
        "<blockquote><p>quoted</p></blockquote><ul><li>first</li><li>second</li></ul>"
        '<pre><code class="language-python">print(1)</code></pre>'
        "<script>alert(1)</script><style>.hidden{}</style><svg><text>hidden-svg</text></svg>"
    )
    markdown = html_to_markdown(html, "https://example.org/topic/1")
    child_types = [token.type for token in inline_tokens(markdown)]

    assert markdown.startswith("# Release")
    assert "**bold again *italic***" in markdown
    assert "[help](https://example.org/help)" in markdown
    assert "- first" in markdown and "- second" in markdown
    assert "```python" in markdown and "print(1)" in markdown
    assert "alert(1)" not in markdown and "hidden-svg" not in markdown
    assert child_types.count("strong_open") == 1
    assert child_types.count("em_open") == 1
    assert child_types.count("link_open") == 1
    assert child_types.count("code_inline") == 1


def test_markdownify_keeps_tables_and_uses_atx_headings_and_dash_lists():
    markdown = html_to_markdown(
        "<h2>Table</h2><table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>1</td></tr></table>"
        "<ul><li>Item</li></ul>"
    )

    assert "## Table" in markdown
    assert "| Name | Value |" in markdown
    assert "| --- | --- |" in markdown
    assert "| A | 1 |" in markdown
    assert "- Item" in markdown
    assert "* Item" not in markdown


def test_html_url_safety_rejects_active_and_malformed_destinations():
    markdown = html_to_markdown(
        '<p>Safe <a href="javascript:alert(1)">unsafe link</a> '
        '<img src="data:image/png;base64,AAAA" alt="bad image">'
        '<img src="http://[" alt="broken image">'
        '<a href="https://user:pass@example.org/file">credential link</a></p>',
        "https://example.org/feed",
    )
    assert "unsafe link" in markdown and "javascript:" not in markdown
    assert "data:image" not in markdown
    assert "http://[" not in markdown
    assert "credential link" in markdown and "user:pass" not in markdown
    assert _clean_link("http://[", "https://example.org/feed") == ""


def test_relative_image_urls_emoji_shortcodes_and_custom_emoji_are_safe():
    media = []
    markdown = html_to_markdown(
        '<p><img class="emoji" src="/images/emoji/gift.png" alt=":gift:"> '
        '<img class="emoji emoji-custom" src="/emoji/custom/mascot.png" alt=":company_mascot:"> '
        '<img src="/photo.png" alt="photo" width="320" height="180"></p>',
        "https://example.org/feed",
        media=media,
    )

    assert "🎁" in markdown
    assert "![:company_mascot:]" in markdown
    assert "![photo](https://example.org/photo.png)" in markdown
    assert [item["url"] for item in media] == [
        "https://example.org/emoji/custom/mascot.png",
        "https://example.org/photo.png",
    ]
    assert media[-1]["width"] == 320 and media[-1]["height"] == 180
    assert media[0]["class"] == ["emoji", "emoji-custom"]


def test_converter_depth_failure_is_controlled_and_does_not_change_process_limit():
    before = sys.getrecursionlimit()
    deep = "<div>" * 1200 + "safe" + "</div>" * 1200

    with pytest.raises(ValueError, match="safe conversion depth"):
        html_to_markdown(deep)
    assert sys.getrecursionlimit() == before


def test_feed_parser_falls_back_to_next_body_candidate_after_deep_html_failure():
    deep = "<div>" * 1200 + "hidden" + "</div>" * 1200
    xml = (
        "<rss><channel><item><guid>x</guid><title>T</title><link>https://example.org/t/1</link>"
        "<content:encoded xmlns:content='http://purl.org/rss/1.0/modules/content/'><![CDATA["
        + deep
        + "]]></content:encoded><description>Fallback summary</description></item></channel></rss>"
    ).encode()

    article = parse_xml(xml).articles[0]
    assert body(article) == "Fallback summary"


def test_feed_parser_reports_deep_html_failure_when_no_fallback_candidate_exists():
    deep = "<div>" * 1200 + "hidden" + "</div>" * 1200
    xml = (
        "<rss><channel><item><guid>x</guid><title>T</title><link>https://example.org/t/1</link>"
        "<description><![CDATA[" + deep + "]]></description></item></channel></rss>"
    ).encode()

    with pytest.raises(FeedParseError, match="could not convert feed HTML"):
        parse_xml(xml)


def test_generatorless_discourse_footer_is_removed_at_markdown_ingestion_stage():
    xml = """<rss><channel><title>Source title</title><item><guid>x</guid><title>Topic</title>
      <link>https://forum.example/t/example-topic/42/1</link><description><![CDATA[
      <p>Body text also mentions Read full topic.</p><blockquote><p>3 个帖子 - 3 位参与者</p></blockquote>
      <p>11 posts - 10 participants</p><p><a href="https://forum.example/t/example-topic/42">Read full topic</a></p>
      ]]></description></item></channel></rss>""".encode()
    parsed = parse_xml(xml, "https://forum.example/feed")
    article = parsed.articles[0]
    markdown = body(article)

    assert parsed.metadata["source_adapter"] == "discourse"
    assert "Body text also mentions Read full topic." in markdown
    assert "3 个帖子 - 3 位参与者" in markdown
    assert "11 posts - 10 participants" not in markdown
    assert "[Read full topic](https://forum.example/t/example-topic/42)" not in markdown
    assert article.metadata["feed_title"] == "Source title"


def test_discourse_footer_cleanup_requires_final_stats_and_same_topic_link():
    canonical = "https://forum.example/t/3048407/1"
    trailing_list = html_to_markdown(
        '<p>Body</p><p>3 posts - 3 participants</p><p><a href="/t/3048407">Read full topic</a></p>'
        "<ul><li>Later body content</li></ul>",
        canonical,
        is_discourse=True,
    )
    wrong_topic = html_to_markdown(
        '<p>Body</p><p>3 posts - 3 participants</p><p><a href="/t/999">Read full topic</a></p>',
        canonical,
        is_discourse=True,
    )

    assert "3 posts - 3 participants" in trailing_list
    assert "Read full topic" in trailing_list
    assert "3 posts - 3 participants" in wrong_topic
    assert "Read full topic" in wrong_topic


def test_discourse_title_anchors_are_removed_but_generic_heading_links_remain():
    discourse = html_to_markdown(
        '<h2>标题<a name="p-23913342-h-1" class="anchor" '
        'href="https://linux.do#p-23913342-h-1">¶</a></h2>'
        '<p>正文<a name="p-23913342-h-2" class="anchor" '
        'href="https://linux.do#p-23913342-h-2" aria-label="标题链接"></a></p>'
        '<h3><a name="p-23913342-h-3" class="anchor" '
        'href="https://linux.do#p-23913342-h-3">真实标题文字</a></h3>',
        "https://linux.do/t/topic/1",
        is_discourse=True,
    )
    generic = html_to_markdown(
        '<h2><a class="anchor" id="section" href="https://example.org/a#section">Linked heading</a></h2>',
        "https://example.org/a",
    )

    assert "[¶]" not in discourse
    assert "标题链接" not in discourse
    assert "## 标题" in discourse and "真实标题文字" in discourse
    assert "[Linked heading](https://example.org/a#section)" in generic


def test_feed_parser_normalizes_discourse_onebox_and_removes_only_matching_source_decorations():
    activity_url = (
        "https://personal-act.wps.cn/lego-act/friend-card/desktop?activity_number=fake-activity"
        "&page_number=fake-page&token=fake-token#fake-section"
    )
    escaped_activity_url = activity_url.replace("&", "&amp;")
    icon_url = "https://personal-act.wpscdn.cn/operations-fe/fe-static/img/favicon/default.ico"
    html = f'''<aside class="onebox allowlistedgeneric" data-onebox-src="{escaped_activity_url}">
      <header class="source"><img class="site-icon" src="{icon_url}" alt="" width="32" height="32">
        <a href="{escaped_activity_url}">personal-act.wps.cn</a></header>
      <article class="onebox-body"><h3><a href="{escaped_activity_url}">WPS 大会员亲友卡</a></h3></article>
      <div class="onebox-metadata"></div><div style="clear: both"></div></aside>
      <p>4 posts - 4 participants</p><p><a href="https://linux.do/t/topic/42">Read full topic</a></p>'''
    xml = (
        "<rss><channel><title>Forum</title><item><guid>onebox-entry</guid><title>Forum post</title>"
        "<link>https://linux.do/t/topic/42/1</link><description><![CDATA["
        + html
        + "]]></description></item></channel></rss>"
    ).encode()

    article = parse_xml(xml, "https://linux.do/c/feedback/42.rss").articles[0]
    markdown = body(article)

    assert f"[WPS 大会员亲友卡]({activity_url})" in markdown
    assert markdown.count(activity_url) == 1
    assert "[personal-act.wps.cn]" not in markdown
    assert icon_url not in markdown
    assert all(item.get("url") != icon_url for item in article.metadata["media"])
    assert "4 posts - 4 participants" not in markdown
    assert "Read full topic" not in markdown


def test_onebox_cleanup_keeps_summary_images_and_other_links_in_order():
    activity_url = "https://personal-act.wps.cn/lego-act/friend-card/desktop?token=fake-token"
    icon_url = "https://static.example.org/site-icon.ico"
    preview_url = "https://static.example.org/card-preview.jpg"
    other_url = "https://personal-act.wps.cn/terms"
    html = f'''<aside class="onebox allowlistedgeneric" data-onebox-src="{activity_url}">
      <header class="source"><a href="{activity_url}"><img class="site-icon" src="{icon_url}" alt="">
        personal-act.wps.cn</a><span class="onebox-author">Shared by Editor · Oct 30</span></header>
      <article class="onebox-body"><h3><a href="{activity_url}">Card title</a></h3>
        <p>Useful summary with <a href="{other_url}">terms</a>.</p>
        <p><img src="{preview_url}" alt="Preview"></p></article></aside>'''
    media = []

    markdown = html_to_markdown(html, "https://forum.example/t/thread/9", media=media)

    assert markdown.index("[Card title]") < markdown.index("Useful summary")
    assert "Useful summary" in markdown
    assert "Shared by Editor · Oct 30" in markdown
    assert f"[terms]({other_url})" in markdown
    assert f"![Preview]({preview_url})" in markdown
    assert markdown.count(activity_url) == 1
    assert icon_url not in markdown
    assert [item["url"] for item in media] == [preview_url]


@pytest.mark.parametrize(
    ("source_href", "title_href", "source_label"),
    [
        (
            "https://personal-act.wps.cn/source?token=fake-token",
            "https://personal-act.wps.cn/target?token=fake-token",
            "personal-act.wps.cn",
        ),
        (
            "https://personal-act.wps.cn/target?token=fake-token",
            "https://personal-act.wps.cn/target?token=fake-token",
            "Open the activity details",
        ),
        (
            "https://personal-act.wps.cn/target?token=fake-token",
            None,
            "personal-act.wps.cn",
        ),
        (
            "https://personal-act.wps.cn/target?token=fake-token",
            "javascript:alert(1)",
            "personal-act.wps.cn",
        ),
        (
            "https://personal-act.wps.cn/target?token=fake-token",
            "https://personal-act.wps.cn/target?token=fake-token",
            "personal-act.wps.cn source note",
        ),
    ],
)
def test_onebox_cleanup_falls_back_for_mismatched_or_descriptive_sources(
    source_href: str, title_href: str | None, source_label: str
):
    icon_url = "https://static.example.org/site-icon.ico"
    title_link = f'<a href="{title_href}">Card title</a>' if title_href is not None else "<a>Card title</a>"
    html = f'''<aside class="onebox allowlistedgeneric" data-onebox-src="{source_href}">
      <header class="source"><a href="{source_href}"><img class="site-icon" src="{icon_url}" alt="">{source_label}</a></header>
      <article class="onebox-body"><h3>{title_link}</h3></article></aside>'''
    media = []

    markdown = html_to_markdown(html, "https://forum.example/t/thread/9", media=media)

    assert icon_url in markdown
    assert any(item.get("url") == icon_url for item in media)
    assert "Card title" in markdown
    assert source_label in markdown


def test_onebox_cleanup_ignores_a_title_link_inside_a_hidden_template():
    source_url = "https://personal-act.wps.cn/target?token=fake-token"
    icon_url = "https://static.example.org/site-icon.ico"
    html = f'''<aside class="onebox allowlistedgeneric" data-onebox-src="{source_url}">
      <header class="source"><a href="{source_url}"><img class="site-icon" src="{icon_url}" alt="">personal-act.wps.cn</a></header>
      <article class="onebox-body"><template><h3><a href="{source_url}">Hidden title</a></h3></template></article>
    </aside>'''
    media = []

    markdown = html_to_markdown(html, "https://forum.example/t/thread/9", media=media)

    assert source_url in markdown and "personal-act.wps.cn" in markdown
    assert icon_url in markdown
    assert "Hidden title" not in markdown
    assert any(item.get("url") == icon_url for item in media)


def test_onebox_cleanup_does_not_filter_ordinary_domain_links_or_small_images():
    source_url = "https://personal-act.wps.cn/target?token=fake-token"
    icon_url = "https://static.example.org/site-icon.ico"
    html = (
        f'<p><a href="{source_url}">personal-act.wps.cn</a> '
        f'<img class="site-icon" src="{icon_url}" alt="Icon" width="16" height="16"></p>'
    )
    media = []

    markdown = html_to_markdown(html, "https://forum.example/t/thread/9", media=media)

    assert f"[personal-act.wps.cn]({source_url})" in markdown
    assert f"![Icon]({icon_url})" in markdown
    assert any(item.get("url") == icon_url for item in media)


def test_feed_date_failures_and_entities_do_not_break_article_metadata():
    xml = b"""<rss><channel><title>Feed &amp; News</title><item><guid>x</guid><title>Item</title>
      <link>https://example.org/item</link><pubDate>not a date</pubDate><description><![CDATA[<p>A &amp; B</p>]]></description>
      </item></channel></rss>"""
    parsed = parse_xml(xml)
    article = parsed.articles[0]

    assert parsed.title == "Feed & News"
    assert article.published_at is None
    assert article.updated_at is None
    assert "A & B" in body(article)


def test_media_rss_thumbnail_association_and_view_image_anchor_are_deduplicated():
    original = "https://cdn.example.org/photo.jpg"
    thumbnail = "https://cdn.example.org/photo-small.jpg"
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Truth</title>
      <item><guid>image-entry</guid><title>Image entry</title><link>https://example.org/entry/1</link>
      <description><![CDATA[<p>Caption</p><p><a href="{original}">View image</a></p>]]></description>
      <media:content url="{original}" medium="image" type="image/jpeg">
        <media:thumbnail url="{thumbnail}" width="640" height="360"/>
      </media:content></item></channel></rss>'''.encode()
    article = parse_xml(xml).articles[0]
    media = article_media(article)

    assert len(media) == 1
    assert media[0].type == "image" and media[0].url == original
    assert media[0].attrs["thumbnail_url"] == thumbnail
    assert article.metadata["media"][1]["associated_url"] == original
    assert "Caption" in body(article)


def test_video_media_metadata_and_markdown_link_are_deduplicated_and_pure_video():
    video = "https://truth.example/media/clip.mp4"
    poster = "https://truth.example/media/poster.jpg"
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Truth</title>
      <item><guid>video-entry</guid><title>Video</title><link>https://truth.example/entry/1</link>
      <description><![CDATA[<p><a href="{video}">View video</a></p>]]></description>
      <media:content url="{video}" medium="video" type="video/mp4">
        <media:thumbnail url="{poster}" width="640" height="360"/>
      </media:content></item></channel></rss>'''.encode()
    article = parse_xml(xml).articles[0]
    media = article_media(article)

    assert len(media) == 1
    assert media[0].type == "video" and media[0].url == video
    assert media[0].attrs["poster"] == poster
    assert is_pure_video_article(article)


def test_nested_thumbnails_keep_their_own_originals_in_markdown_media_order():
    originals = ["https://cdn.example.org/one.jpg", "https://cdn.example.org/two.jpg"]
    thumbnails = ["https://cdn.example.org/one-thumb.jpg", "https://cdn.example.org/two-thumb.jpg"]
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Feed</title>
      <item><guid>multi-image</guid><title>Multiple images</title><link>https://example.org/entry</link>
      <description><![CDATA[<p><a href="{originals[1]}">View image</a></p>
        <p><a href="{originals[0]}">View image</a></p>]]></description>
      <media:content url="{originals[0]}" medium="image" type="image/jpeg">
        <media:thumbnail url="{thumbnails[0]}" width="320" height="200"/>
      </media:content>
      <media:content url="{originals[1]}" medium="image" type="image/jpeg">
        <media:thumbnail url="{thumbnails[1]}" width="640" height="400"/>
      </media:content></item></channel></rss>'''.encode()
    article = parse_xml(xml).articles[0]
    images = article_media(article)

    assert [(item.type, item.url) for item in images] == [("image", originals[1]), ("image", originals[0])]
    assert [item.attrs["thumbnail_url"] for item in images] == [thumbnails[1], thumbnails[0]]
    assert [item.attrs["thumbnail_width"] for item in images] == [640, 320]
    assert [item.attrs["thumbnail_height"] for item in images] == [400, 200]


def test_multiple_thumbnails_for_one_video_are_one_video_poster_resource():
    video = "https://cdn.example.org/clip.mp4"
    posters = ["https://cdn.example.org/poster-small.jpg", "https://cdn.example.org/poster-large.jpg"]
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Feed</title>
      <item><guid>video-multiple-posters</guid><title>Video</title><link>https://example.org/entry</link>
      <description><![CDATA[<p><a href="{video}">View video</a></p>]]></description>
      <media:content url="{video}" medium="video" type="video/mp4">
        <media:thumbnail url="{posters[0]}" width="320" height="180"/>
        <media:thumbnail url="{posters[1]}" width="640" height="360"/>
      </media:content></item></channel></rss>'''.encode()
    article = parse_xml(xml).articles[0]
    media = article_media(article)

    assert [(item.type, item.url) for item in media] == [("video", video)]
    assert media[0].attrs["poster"] == posters[0]
    assert [item["associated_url"] for item in article.metadata["media"] if item.get("role") == "thumbnail"] == [
        video,
        video,
    ]
    assert is_pure_video_article(article)


def test_unassociated_thumbnail_remains_an_independent_image():
    original = "https://cdn.example.org/photo.jpg"
    associated_thumbnail = "https://cdn.example.org/photo-small.jpg"
    standalone_thumbnail = "https://cdn.example.org/standalone.jpg"
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Feed</title>
      <item><guid>mixed-thumbnails</guid><title>Photo</title><link>https://example.org/entry</link>
      <description><![CDATA[<p><a href="{original}">View image</a></p>]]></description>
      <media:content url="{original}" medium="image" type="image/jpeg">
        <media:thumbnail url="{associated_thumbnail}"/>
      </media:content>
      <media:thumbnail url="{standalone_thumbnail}"/>
      </item></channel></rss>'''.encode()
    article = parse_xml(xml).articles[0]
    images = article_media(article)

    assert [(item.type, item.url) for item in images] == [
        ("image", original),
        ("image", standalone_thumbnail),
    ]
    assert images[0].attrs["thumbnail_url"] == associated_thumbnail
    standalone = next(item for item in article.metadata["media"] if item["url"] == standalone_thumbnail)
    assert "associated_url" not in standalone


def test_xml_only_image_video_and_enclosure_are_added_to_canonical_markdown_once():
    image = "https://cdn.example.org/source-photo.jpg"
    image_thumbnail = "https://cdn.example.org/source-photo-small.jpg"
    video = "https://cdn.example.org/source-clip.mp4"
    attachment = "https://cdn.example.org/source-report.pdf"
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Feed</title>
      <item><guid>resources-only</guid><title>Resources</title><link>https://example.org/entry</link>
      <description><![CDATA[<p>Body text.</p>]]></description>
      <media:content url="{image}" medium="image" type="image/jpeg">
        <media:thumbnail url="{image_thumbnail}" width="320" height="200"/>
      </media:content>
      <media:content url="{video}" medium="video" type="video/mp4"/>
      <enclosure url="{attachment}" type="application/pdf" length="10"/>
      </item></channel></rss>'''.encode()
    article = parse_xml(xml).articles[0]
    markdown = body(article)
    media = article_media(article)

    assert "Body text." in markdown
    assert f"![Image]({image})" in markdown
    assert f"[Video]({video})" in markdown
    assert f"[Attachment]({attachment})" in markdown
    assert image_thumbnail not in markdown
    assert [(item.type, item.url) for item in media] == [
        ("image", image),
        ("video", video),
        ("attachment", attachment),
    ]


def test_xml_media_already_referenced_in_html_is_not_appended_a_second_time():
    image = "https://cdn.example.org/photo.jpg"
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Feed</title>
      <item><guid>image-linked</guid><title>Photo</title><link>https://example.org/entry</link>
      <description><![CDATA[<p><img src="{image}" alt="Cover"></p>]]></description>
      <media:content url="{image}" medium="image" type="image/jpeg"/>
      </item></channel></rss>'''.encode()
    article = parse_xml(xml).articles[0]
    markdown = body(article)

    assert markdown.count(image) == 1
    assert [(item.type, item.url) for item in article_media(article)] == [("image", image)]


def test_media_tokens_keep_body_order_metadata_dimensions_and_url_identity():
    article = Article(
        "a",
        "f",
        "entry",
        "https://example.org/article",
        "Article",
        content_blocks=[
            ContentBlock(
                "markdown",
                text=(
                    "[Video](https://cdn.example.org/clip.mp4) "
                    "![Diagram](https://cdn.example.org/a%28b%29.png) "
                    "[Audio](https://cdn.example.org/sound.mp3)"
                ),
            )
        ],
        metadata={
            "media": [
                {"url": "https://cdn.example.org/a(b).png", "medium": "image", "width": 800, "height": 600},
                {"url": "https://cdn.example.org/clip.mp4", "medium": "video"},
                {"url": "https://cdn.example.org/sound.mp3", "medium": "audio"},
            ]
        },
    )
    media = article_media(article)

    assert [(item.type, item.url) for item in media] == [
        ("video", "https://cdn.example.org/clip.mp4"),
        ("image", "https://cdn.example.org/a(b).png"),
        ("audio", "https://cdn.example.org/sound.mp3"),
    ]
    assert media[1].attrs["alt"] == "Diagram"
    assert media[1].attrs["width"] == 800 and media[1].attrs["height"] == 600


def test_html_video_and_audio_tags_keep_source_mime_and_poster_metadata():
    resources = []
    markdown = html_to_markdown(
        '<video poster="/poster.jpg" title="Trailer"><source src="/clip.mp4" type="video/mp4"></video>'
        '<audio src="/audio.mp3" type="audio/mpeg" title="Theme"></audio>',
        "https://example.org/article",
        media=resources,
    )
    article = Article(
        "media-tags",
        "feed",
        "entry",
        "https://example.org/article",
        "Media tags",
        content_blocks=[ContentBlock("markdown", text=markdown)],
        metadata={"media": resources},
    )
    media = article_media(article)

    assert [(item.type, item.url) for item in media] == [
        ("video", "https://example.org/clip.mp4"),
        ("audio", "https://example.org/audio.mp3"),
    ]
    assert media[0].attrs["poster"] == "https://example.org/poster.jpg"
    assert media[0].attrs["type"] == "video/mp4"
    assert media[1].attrs["type"] == "audio/mpeg"


def test_html_lightbox_uses_original_image_and_retains_thumbnail_dimensions_once():
    media_items = []
    markdown = html_to_markdown(
        '<div class="lightbox-wrapper"><a class="lightbox" href="https://cdn.example.org/original.png">'
        '<img src="https://cdn.example.org/thumb.png" alt="Preview" width="90" height="60"></a></div>',
        "https://example.org/article",
        media=media_items,
    )
    article = Article(
        "a",
        "f",
        "entry",
        "https://example.org/article",
        "Article",
        content_blocks=[ContentBlock("markdown", text=markdown)],
        metadata={"media": media_items},
    )
    result = article_media(article)

    assert markdown == "![Preview](https://cdn.example.org/original.png)"
    assert len(media_items) == 1
    assert len(result) == 1 and result[0].url == "https://cdn.example.org/original.png"
    assert result[0].attrs["thumbnail_url"] == "https://cdn.example.org/thumb.png"
    assert result[0].attrs["thumbnail_width"] == 90
    assert result[0].attrs["thumbnail_height"] == 60


def test_legacy_cached_content_is_converted_once_and_business_hash_is_preserved():
    video = "https://cdn.example.org/clip.mp4"
    poster = "https://cdn.example.org/poster.jpg"
    cached = Article(
        "legacy",
        "feed",
        "entry",
        "https://example.org/item",
        "Cached",
        content_hash="business-hash-stays-the-same",
        content_blocks=[
            ContentBlock(
                "paragraph",
                children=[ContentBlock("link", url=video, children=[ContentBlock("text", "View video")])],
            ),
            ContentBlock("video", url=video, attrs={"poster": poster}),
            ContentBlock(
                "paragraph",
                children=[
                    ContentBlock("link", url="https://example.org/context", children=[ContentBlock("text", "Context")])
                ],
            ),
        ],
        metadata={"media": [{"url": video, "medium": "video", "poster": poster}]},
    )
    cleaned = clean_cached_article(cached)

    assert cleaned is not cached
    assert cleaned.content_hash == cached.content_hash
    assert [block.type for block in cleaned.content_blocks] == ["markdown"]
    assert article_markdown(cleaned).count(video) == 1
    assert "[Context](https://example.org/context)" in article_markdown(cleaned)
    assert article_media(cleaned)[0].attrs["poster"] == poster
    assert clean_cached_article(cleaned) is cleaned
    assert cached.content_blocks[0].children[0].url == video


def test_legacy_inline_code_uses_a_compact_commonmark_fence():
    cached = Article(
        "legacy-code",
        "feed",
        "entry",
        "https://example.org/item",
        "Cached",
        content_blocks=[ContentBlock("paragraph", children=[ContentBlock("inline_code", text="literal `code` value")])],
    )
    markdown = article_markdown(cached)
    tokens = list(inline_tokens(markdown))

    assert markdown.count("``") == 2
    assert [(token.type, token.content) for token in tokens if token.type == "code_inline"] == [
        ("code_inline", "literal `code` value")
    ]
