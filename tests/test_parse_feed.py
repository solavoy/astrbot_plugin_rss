from pathlib import Path

import pytest
from astrbot_plugin_rss.core.models import Article, ContentBlock, FetchResult
from astrbot_plugin_rss.normalizers.discourse import clean_cached_article
from astrbot_plugin_rss.normalizers.html_content import normalize_html
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


def walk(blocks):
    for block in blocks:
        yield block
        yield from walk(block.children)


def test_discourse_content_order_links_media_and_nested_blocks():
    parsed = FeedParser().parse(result("discourse_feedback.xml"))
    article = parsed.articles[0]
    assert parsed.metadata["source_adapter"] == "discourse"
    assert article.title == "测试反馈 😀"
    assert article.author == "作者"
    assert article.canonical_url == "https://linux.do/t/topic/42/1"
    assert article.published_at == 1792382400.0
    blocks = list(walk(article.content_blocks))
    types = [block.type for block in blocks]
    assert "quote" in types and "code" in types and "list" in types and "image" in types
    link = next(block for block in blocks if block.type == "link")
    assert link.url == "https://linux.do/help"
    image = next(block for block in blocks if block.type == "image")
    assert image.url == "https://linux.do/uploads/photo.png"
    assert [block.type for block in article.content_blocks[0].children] == [
        "paragraph",
        "quote",
        "paragraph",
        "code",
        "list",
        "paragraph",
    ]


def test_atom_relative_link_and_date():
    parsed = FeedParser().parse(result("atom_sample.xml"))
    article = parsed.articles[0]
    assert article.canonical_url == "https://linux.do/entry/1"
    assert article.published_at == 1792368000.0
    assert article.content_blocks[0].children[0].text == "Summary"


def test_rss10_rdf_and_rsshub_generator_adapter():
    rdf = b"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel rdf:about="https://example.org/"><title>Example</title><link>https://example.org/</link>
      <description>RDF channel</description><items><rdf:Seq><rdf:li rdf:resource="https://example.org/a"/></rdf:Seq></items></channel>
      <item rdf:about="https://example.org/a"><title>RDF item</title><link>https://example.org/a</link><dc:creator>Writer</dc:creator></item>
      </rdf:RDF>"""
    rsshub = b"""<rss version="2.0"><channel><title>Hub</title><link>https://rsshub.example/</link>
      <description>Generated</description><generator>RSSHub</generator><item><guid>hub-1</guid><title>Entry</title>
      <link>https://rsshub.example/item/1</link><description>Body</description></item></channel></rss>"""
    parsed_rdf = FeedParser().parse(FetchResult("rdf", "https://example.org/rss", status_code=200, raw_xml=rdf))
    parsed_hub = FeedParser().parse(FetchResult("hub", "https://rsshub.example/feed", status_code=200, raw_xml=rsshub))
    assert parsed_rdf.articles[0].title == "RDF item"
    assert parsed_rdf.articles[0].author == "Writer"
    assert parsed_hub.metadata["source_adapter"] == "rsshub"
    assert parsed_hub.articles[0].canonical_url == "https://rsshub.example/item/1"


@pytest.mark.parametrize(
    "xml",
    [
        b"<html><body>not a feed</body></html>",
        b"<!DOCTYPE rss [<!ENTITY x 'x'>]><rss><channel/></rss>",
        b"<rss><channel>",
    ],
)
def test_rejects_unsafe_or_invalid_xml(xml):
    value = FetchResult("f", "https://example.org/feed", status_code=200, raw_xml=xml)
    with pytest.raises(FeedParseError):
        FeedParser().parse(value)


def test_html_sanitization_keeps_order_and_drops_active_nodes():
    value = b'<div><p>A<script>alert(1)</script><img src="/i.png"></p><blockquote>Q</blockquote><pre><code>x &lt; 2</code></pre><ol><li>one</li></ol></div>'
    parsed = FeedParser().parse(
        FetchResult(
            "f",
            "https://example.org/feed",
            "https://example.org/feed",
            200,
            "application/rss+xml",
            b"<rss><channel><item><guid>x</guid><description><![CDATA["
            + value
            + b"]]></description></item></channel></rss>",
        )
    )
    text = repr([block.to_dict() for block in parsed.articles[0].content_blocks])
    assert parsed.metadata["source_adapter"] == "generic"
    assert "alert(1)" not in text
    assert all(kind in text for kind in ("image", "quote", "code", "list"))


def test_invalid_html_urls_are_ignored_without_failing_the_feed():
    xml = b"""<rss><channel><title>Feed</title><item><guid>invalid-url</guid><link>https://example.org/item</link>
      <description><![CDATA[<p>Readable text</p><img src="http://[">]]></description></item></channel></rss>"""
    parsed = FeedParser().parse(FetchResult("f", "https://example.org/feed", status_code=200, raw_xml=xml))
    blocks = list(walk(parsed.articles[0].content_blocks))
    assert any(block.text == "Readable text" for block in blocks)
    assert not any(block.type == "image" for block in blocks)
    assert _clean_link("http://[", "https://example.org/feed") == ""


def test_normalize_html_preserves_normal_text_and_image_order():
    blocks = normalize_html(
        '<p>before<img src="/one.png">between<img src="/two.png">after</p>',
        "https://example.org/feed",
    )
    assert len(blocks) == 1
    assert [(block.type, block.text, block.url) for block in blocks[0].children] == [
        ("text", "before", ""),
        ("image", "", "https://example.org/one.png"),
        ("text", "between", ""),
        ("image", "", "https://example.org/two.png"),
        ("text", "after", ""),
    ]


@pytest.mark.parametrize("depth", [900, 2000])
def test_normalize_html_flattens_deep_input_with_bounded_block_depth(depth):
    value = "<div>" * depth + 'before<img src="/deep.png">after<script>hidden</script>' + "</div>" * depth
    blocks = normalize_html(value, "https://example.org/feed")
    pending = [(block, 1) for block in reversed(blocks)]
    ordered = []
    maximum_depth = 0
    while pending:
        block, block_depth = pending.pop()
        ordered.append(block)
        maximum_depth = max(maximum_depth, block_depth)
        pending.extend((child, block_depth + 1) for child in reversed(block.children))
    leaves = [block for block in ordered if not block.children]
    assert maximum_depth <= 64
    assert [(block.type, block.text) for block in leaves if block.type == "text"] == [
        ("text", "before"),
        ("text", "after"),
    ]
    assert [block.url for block in leaves if block.type == "image"] == ["https://example.org/deep.png"]
    assert not any("hidden" in block.text for block in ordered)


def test_discourse_footer_removed_for_generatorless_feed_but_body_text_kept():
    xml = """<rss><channel><title>Source title</title><item><guid>x</guid><title>Topic</title>
      <link>https://forum.example/t/example-topic/42/1</link><description><![CDATA[
      <div><p>正文中也有 Read full topic</p><blockquote><p>3 个帖子 - 3 位参与者</p></blockquote>
      <p>11 posts - 10 participants</p><p><a href="https://forum.example/t/example-topic/42">Read full topic</a></p>
      </div>]]></description></item></channel></rss>""".encode()
    parsed = FeedParser().parse(FetchResult("f", "https://forum.example/feed", status_code=200, raw_xml=xml))
    article = parsed.articles[0]
    assert parsed.metadata["source_adapter"] == "discourse"
    body = repr([block.to_dict() for block in article.content_blocks])
    assert "11 posts - 10 participants" not in body
    assert "Read full topic" in body
    assert "3 个帖子 - 3 位参与者" in body
    assert article.metadata["feed_title"] == "Source title"


def test_discourse_footer_precise_and_same_topic_route_variants():
    from astrbot_plugin_rss.normalizers.discourse import strip_discourse_footer
    from astrbot_plugin_rss.normalizers.html_content import normalize_html

    canonical = "https://forum.example/t/3048407"
    blocks = normalize_html(
        "<div><p>正文</p><p><small>3 个帖子 - 3 位参与者</small></p>"
        '<p><a href="https://forum.example/t/3048407/1">阅读完整话题</a></p></div>',
        canonical,
    )
    cleaned = strip_discourse_footer(blocks, canonical)
    assert cleaned[0].children[0].children[0].text == "正文"
    cleaned_text = repr([block.to_dict() for block in cleaned])
    assert "3 个帖子" not in cleaned_text and "阅读完整话题" not in cleaned_text
    for topic_url, post_url in (
        ("https://forum.example/t/topic/3048407", "https://forum.example/t/topic/3048407/1"),
        ("https://forum.example/t/3048407", "https://forum.example/t/3048407/1"),
    ):
        footer = normalize_html(f'<p>3 个帖子 - 3 位参与者</p><p><a href="{post_url}">阅读完整话题</a></p>', topic_url)
        without_footer = strip_discourse_footer(footer, topic_url)
        assert "3 个帖子" not in repr([block.to_dict() for block in without_footer])
        assert "阅读完整话题" not in repr([block.to_dict() for block in without_footer])
    extra = normalize_html(
        "<div><p>正文</p><p>3 个帖子 - 3 位参与者</p>"
        '<p>其他文字 <a href="https://forum.example/t/3048407">阅读完整话题</a></p></div>',
        canonical,
    )
    assert strip_discourse_footer(extra, canonical) is extra
    wrong_topic = normalize_html(
        '<p>3 个帖子 - 3 位参与者</p><p><a href="https://forum.example/t/9">阅读完整话题</a></p>', canonical
    )
    assert strip_discourse_footer(wrong_topic, canonical) is wrong_topic
    earlier_wrapper = normalize_html(
        "<div><p>3 个帖子 - 3 位参与者</p>"
        '<p><a href="https://forum.example/t/3048407">阅读完整话题</a></p></div><p>后续正文</p>',
        canonical,
    )
    assert strip_discourse_footer(earlier_wrapper, canonical) is earlier_wrapper
    with_media = normalize_html(
        '<p>3 个帖子 - 3 位参与者</p><p><a href="https://forum.example/t/3048407">阅读完整话题</a>'
        '<img src="https://forum.example/image.png"></p>',
        canonical,
    )
    assert strip_discourse_footer(with_media, canonical) is with_media


def test_discourse_images_and_link_order_normalize_without_touching_article_links():
    from astrbot_plugin_rss.normalizers.discourse import normalize_image_links
    from astrbot_plugin_rss.normalizers.html_content import normalize_html

    blocks = normalize_image_links(
        normalize_html(
            '<p>前<img src="/uploads/default/original/2X/first.png" alt="第一图">中</p>'
            '<p><a href="/uploads/default/original/2X/second.jpg">image</a></p>'
            '<p>再<a class="lightbox-wrapper" href="/uploads/default/original/2X/third.png">'
            '<img src="/uploads/default/optimized/2X/third.png" alt="第三图"></a>后</p>'
            '<p><a href="https://example.org/article/1">普通文章</a></p>'
            '<p><a href="https://example.org/image">image</a></p>'
            '<p><a class="stripped-secure-view-upload" href="https://example.org/uploads/secret.png">'
            "View protected image</a></p>",
            "https://forum.example/t/topic/3048407",
        )
    )
    found = []

    def walk(items):
        for item in items:
            found.append(item)
            walk(item.children)

    walk(blocks)
    images = [item for item in found if item.type == "image"]
    assert [item.url for item in images] == [
        "https://forum.example/uploads/default/original/2X/first.png",
        "https://forum.example/uploads/default/original/2X/second.jpg",
        "https://forum.example/uploads/default/original/2X/third.png",
    ]
    links = [item for item in found if item.type == "link"]
    assert [item.url for item in links] == [
        "https://example.org/article/1",
        "https://example.org/image",
        "https://example.org/uploads/secret.png",
    ]


def test_feed_parser_normalizes_discourse_image_anchor_to_image_block():
    xml = """<rss><channel><title>Forum</title><generator>Discourse</generator><item><guid>x</guid>
      <title>Topic</title><link>https://forum.example/t/topic/42</link><description><![CDATA[
      <p><a href="https://forum.example/uploads/default/original/2X/picture.png">image</a></p>
      ]]></description></item></channel></rss>""".encode()
    parsed = FeedParser().parse(FetchResult("f", "https://forum.example/feed", status_code=200, raw_xml=xml))
    image_blocks = [block for block in walk(parsed.articles[0].content_blocks) if block.type == "image"]
    assert [block.url for block in image_blocks] == ["https://forum.example/uploads/default/original/2X/picture.png"]


def test_lightbox_uses_original_url_but_plain_image_anchor_keeps_img_src():
    from astrbot_plugin_rss.normalizers.discourse import normalize_image_links
    from astrbot_plugin_rss.normalizers.html_content import normalize_html

    html = normalize_html(
        '<p><a class="lightbox-wrapper" href="https://forum.example/uploads/default/original/2X/full.png">'
        '<img src="https://forum.example/uploads/default/optimized/2X/thumb.png" alt="图"></a></p>'
        '<p><a href="https://forum.example/uploads/default/original/2X/linked.png">'
        '<img src="https://forum.example/uploads/default/optimized/2X/plain-thumb.jpg" alt="普通图"></a></p>',
        "https://forum.example/t/topic/42",
    )
    blocks = normalize_image_links(html, recognize_image_anchors=False)
    images = [block for block in walk(blocks) if block.type == "image"]
    assert [block.url for block in images] == [
        "https://forum.example/uploads/default/original/2X/full.png",
        "https://forum.example/uploads/default/optimized/2X/plain-thumb.jpg",
    ]

    old_cached = ContentBlock(
        "link",
        url="https://forum.example/uploads/default/original/2X/old-full.png",
        children=[
            ContentBlock(
                "image", url="https://forum.example/uploads/default/optimized/2X/old-thumb.png", attrs={"alt": "旧图"}
            )
        ],
        attrs={"class": ["lightbox-wrapper"]},
    )
    [old_image] = [
        block
        for block in walk(normalize_image_links([old_cached], recognize_image_anchors=False))
        if block.type == "image"
    ]
    assert old_image.url.endswith("/old-full.png")


def test_feed_parser_uses_original_href_from_discourse_lightbox_anchor():
    original = "https://cdn3.ldstatic.com/original/4X/ab/cd/example.png"
    thumbnail = "https://cdn3.ldstatic.com/optimized/4X/ab/cd/example_2_10x10.png"
    xml = f'''<rss><channel><title>Forum</title><generator>Discourse</generator><item><guid>x</guid>
      <title>Topic</title><link>https://forum.example/t/topic/42</link><description><![CDATA[
      <div class="lightbox-wrapper"><a class="lightbox" href="{original}" data-download-href="{original}?dl=1" title="image">
      <img src="{thumbnail}" alt="示例图片"></a></div>
      ]]></description></item></channel></rss>'''.encode()
    parsed = FeedParser().parse(FetchResult("f", "https://forum.example/feed", status_code=200, raw_xml=xml))
    images = [block for block in walk(parsed.articles[0].content_blocks) if block.type == "image"]
    assert len(images) == 1
    assert images[0].url == original
    assert images[0].url != thumbnail


def test_discourse_lightbox_without_img_uses_explicit_original_image_href():
    from astrbot_plugin_rss.normalizers.discourse import normalize_image_links
    from astrbot_plugin_rss.normalizers.html_content import normalize_html

    original = "https://cdn3.ldstatic.com/original/4X/ab/cd/example.png"
    blocks = normalize_html(
        f'<div class="lightbox-wrapper"><a class="lightbox" href="{original}" title="image"></a></div>',
        "https://forum.example/t/topic/42",
    )
    images = [
        block for block in walk(normalize_image_links(blocks, recognize_image_anchors=False)) if block.type == "image"
    ]

    assert len(images) == 1
    assert images[0].url == original
    assert images[0].attrs["alt"] == "image"


def test_clean_cached_article_normalizes_lightbox_without_changing_business_hash_or_source():
    from astrbot_plugin_rss.normalizers.discourse import clean_cached_article

    original = "https://cdn3.ldstatic.com/original/4X/ab/cd/cached.png"
    thumbnail = "https://cdn3.ldstatic.com/optimized/4X/ab/cd/cached_2_10x10.png"
    old_block = ContentBlock(
        "link",
        url=original,
        children=[ContentBlock("image", url=thumbnail, attrs={"alt": "缓存图片"})],
        attrs={"class": ["lightbox"]},
    )
    old = Article(
        "cached-image",
        "feed",
        "entry",
        "https://forum.example/t/topic/42",
        "文章",
        published_at=1792382400,
        content_hash="business-hash-stays-the-same",
        content_blocks=[old_block],
        metadata={"source_adapter": "discourse"},
    )
    cleaned = clean_cached_article(old)
    [image] = [block for block in walk(cleaned.content_blocks) if block.type == "image"]
    assert image.url == original
    assert cleaned is not old
    assert cleaned.content_hash == old.content_hash == "business-hash-stays-the-same"
    assert cleaned.published_at == old.published_at
    assert old.content_blocks[0].url == original
    assert old.content_blocks[0].children[0].url == thumbnail


def test_discourse_heading_anchors_are_removed_in_headings_and_paragraphs_without_losing_text():
    xml = """<rss><channel><title>Forum</title><generator>Discourse</generator><item><guid>x</guid>
      <title>Topic</title><link>https://linux.do/t/topic/42</link><description><![CDATA[
      <h2>标题<a name="p-23913342-h-1" class="anchor"
        href="https://linux.do#p-23913342-h-1" aria-label="标题链接">¶</a></h2>
      <p>正文<a name="p-23913342-h-2" class="anchor"
        href="https://linux.do#p-23913342-h-2" aria-label="标题链接"></a></p>
      <h3><a name="p-23913342-h-3" class="anchor"
        href="https://linux.do#p-23913342-h-3">真实标题文字</a></h3>
      ]]></description></item></channel></rss>""".encode()
    article = (
        FeedParser().parse(FetchResult("f", "https://forum.example/feed", status_code=200, raw_xml=xml)).articles[0]
    )
    blocks = list(walk(article.content_blocks))

    assert [block.url for block in blocks if block.type == "link"] == []
    assert [block.text for block in blocks if block.type == "text"] == ["标题", "正文", "真实标题文字"]
    assert article.content_blocks[0].attrs["level"] == 2
    assert article.content_blocks[2].attrs["level"] == 3


def test_linux_do_generated_heading_anchor_is_removed_without_generator():
    xml = """<rss><channel><title>Linux.do</title><item><guid>x</guid><title>Topic</title>
      <link>https://linux.do/t/topic/23913342</link><description><![CDATA[
      <p>正文<a name="p-23913342-h-1" class="anchor"
        href="https://linux.do#p-23913342-h-1" aria-label="标题链接">#</a></p>
      ]]></description></item></channel></rss>""".encode()
    parsed = FeedParser().parse(FetchResult("f", "https://linux.do/feed", status_code=200, raw_xml=xml))
    article = parsed.articles[0]

    assert article.metadata["source_adapter"] == "generic"
    assert [block.url for block in walk(article.content_blocks) if block.type == "link"] == []
    assert [block.text for block in walk(article.content_blocks) if block.type == "text"] == ["正文"]


def test_cached_linux_do_anchor_without_name_is_removed_but_hash_links_remain():
    old = Article(
        "cached-anchor",
        "feed",
        "entry",
        "https://linux.do/t/topic/23913342",
        "Topic",
        content_hash="business-hash-stays-the-same",
        content_blocks=[
            ContentBlock(
                "heading",
                children=[
                    ContentBlock("text", text="标题"),
                    ContentBlock(
                        "link",
                        url="https://linux.do#p-23913342-h-1",
                        text="¶",
                        attrs={"class": ["anchor"]},
                    ),
                    ContentBlock(
                        "link",
                        url="https://forum.example#p-23913342-h-2",
                        text="外站链接",
                        attrs={"class": ["anchor"]},
                    ),
                ],
                attrs={"level": 2},
            ),
            ContentBlock(
                "paragraph",
                children=[ContentBlock("link", url="https://linux.do/#section", text="正文中的跳转")],
            ),
        ],
        metadata={"source_adapter": "generic"},
    )
    cleaned = clean_cached_article(old)
    blocks = list(walk(cleaned.content_blocks))

    assert cleaned is not old
    assert cleaned.content_hash == old.content_hash == "business-hash-stays-the-same"
    assert [block.url for block in blocks if block.type == "link"] == [
        "https://forum.example#p-23913342-h-2",
        "https://linux.do/#section",
    ]
    assert [block.text for block in blocks if block.type == "text"] == ["标题"]
    assert [block.text for block in blocks if block.type == "link"] == ["外站链接", "正文中的跳转"]
    assert old.content_blocks[0].children[1].url == "https://linux.do#p-23913342-h-1"


def test_html_normalizer_retains_anchor_name_attribute():
    blocks = normalize_html(
        '<h2><a class="anchor" name="section" href="#section">标题锚点</a></h2>',
        "https://example.org/article",
    )
    link = next(block for block in walk(blocks) if block.type == "link")
    assert link.attrs["name"] == "section"


def test_generic_heading_hash_link_is_preserved():
    xml = """<rss><channel><title>Example</title><item><guid>x</guid><title>Topic</title>
      <link>https://example.org/article/1</link><description><![CDATA[
      <h2><a class="anchor" id="section" href="https://example.org/article/1#section">有意义的标题链接</a></h2>
      ]]></description></item></channel></rss>""".encode()
    article = FeedParser().parse(FetchResult("f", "https://example.org/feed", status_code=200, raw_xml=xml)).articles[0]
    links = [block for block in walk(article.content_blocks) if block.type == "link"]

    assert len(links) == 1
    assert links[0].url == "https://example.org/article/1#section"
    assert [block.text for block in walk(links[0].children) if block.type == "text"] == ["有意义的标题链接"]


def test_media_content_deduplicates_view_image_anchor_and_keeps_thumbnail_as_metadata():
    original = "https://cdn.example.org/photo.jpg"
    thumbnail = "https://cdn.example.org/photo-small.jpg"
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Truth</title>
      <item><guid>image-entry</guid><title>Image entry</title><link>https://trumpstruth.org/entry/1</link>
      <description><![CDATA[<p>Caption</p><p><a href="{original}">View image</a></p>]]></description>
      <media:content url="{original}" medium="image" type="image/jpeg">
        <media:thumbnail url="{thumbnail}" width="640" height="360"/>
      </media:content>
      </item></channel></rss>'''.encode()

    article = (
        FeedParser().parse(FetchResult("f", "https://trumpstruth.org/feed", status_code=200, raw_xml=xml)).articles[0]
    )
    blocks = list(walk(article.content_blocks))
    images = [block for block in blocks if block.type == "image"]
    assert [block.url for block in images] == [original]
    assert images[0].attrs["thumbnail_url"] == thumbnail
    assert not any(block.type == "link" and block.url == original for block in blocks)
    assert article.metadata["media"][1]["associated_url"] == original
    assert any(block.text == "Caption" for block in blocks)


def test_media_video_thumbnail_and_matching_anchor_make_article_pure_video():
    video = "https://truth-archive.us-iad-1.linodeobjects.com/attachments/18653/clip.mp4"
    poster = "https://truth-archive.us-iad-1.linodeobjects.com/attachments/18653/poster.jpg"
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Truth</title>
      <item><guid>video-entry</guid><title>Video entry</title><link>https://trumpstruth.org/entry/2</link>
      <description><![CDATA[<p></p><p><a href="{video}">View video</a></p>]]></description>
      <media:content url="{video}" medium="video" type="video/mp4">
        <media:thumbnail url="{poster}" width="640" height="360"/>
      </media:content>
      </item></channel></rss>'''.encode()

    article = (
        FeedParser().parse(FetchResult("f", "https://trumpstruth.org/feed", status_code=200, raw_xml=xml)).articles[0]
    )
    blocks = list(walk(article.content_blocks))
    videos = [block for block in blocks if block.type == "video"]
    assert len(videos) == 1
    assert videos[0].url == video
    assert videos[0].attrs["poster"] == poster
    assert not any(block.type == "image" for block in blocks)
    assert not any(block.type == "link" and block.url == video for block in blocks)
    assert is_pure_video_article(article)


def test_nested_media_thumbnails_are_matched_for_multiple_originals():
    originals = ["https://cdn.example.org/one.jpg", "https://cdn.example.org/two.jpg"]
    thumbnails = ["https://cdn.example.org/one-thumb.jpg", "https://cdn.example.org/two-thumb.jpg"]
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Feed</title>
      <item><guid>multi-image</guid><title>Multiple images</title><link>https://example.org/entry</link>
      <description><![CDATA[<p><a href="{originals[0]}">View image</a></p>
        <p><a href="{originals[1]}">View image</a></p>]]></description>
      <media:content url="{originals[0]}" medium="image" type="image/jpeg">
        <media:thumbnail url="{thumbnails[0]}"/>
      </media:content>
      <media:content url="{originals[1]}" medium="image" type="image/jpeg">
        <media:thumbnail url="{thumbnails[1]}"/>
      </media:content></item></channel></rss>'''.encode()

    article = FeedParser().parse(FetchResult("f", "https://example.org/feed", status_code=200, raw_xml=xml)).articles[0]

    images = [block for block in walk(article.content_blocks) if block.type == "image"]
    assert [block.url for block in images] == originals
    assert [block.attrs["thumbnail_url"] for block in images] == thumbnails
    assert [
        item["associated_url"] for item in article.metadata["media"] if item.get("role") == "thumbnail"
    ] == originals


def test_multiple_thumbnails_for_one_original_are_all_deduplicated():
    video = "https://cdn.example.org/clip.mp4"
    posters = ["https://cdn.example.org/poster-small.jpg", "https://cdn.example.org/poster-large.jpg"]
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Feed</title>
      <item><guid>video-multiple-posters</guid><title>Video</title><link>https://example.org/entry</link>
      <description><![CDATA[<p><a href="{video}">View video</a></p>]]></description>
      <media:content url="{video}" medium="video" type="video/mp4">
        <media:thumbnail url="{posters[0]}"/>
        <media:thumbnail url="{posters[1]}"/>
      </media:content></item></channel></rss>'''.encode()

    article = FeedParser().parse(FetchResult("f", "https://example.org/feed", status_code=200, raw_xml=xml)).articles[0]
    blocks = list(walk(article.content_blocks))

    assert [block.type for block in article.content_blocks] == ["video"]
    assert article.content_blocks[0].attrs["poster"] == posters[0]
    assert not any(block.type == "image" for block in blocks)
    assert [item["associated_url"] for item in article.metadata["media"] if item.get("role") == "thumbnail"] == [
        video,
        video,
    ]


def test_unassociated_thumbnail_is_retained_alongside_explicitly_associated_thumbnail():
    original = "https://cdn.example.org/photo.jpg"
    associated_thumbnail = "https://cdn.example.org/photo-small.jpg"
    standalone_thumbnail = "https://cdn.example.org/standalone.jpg"
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Feed</title>
      <item><guid>mixed-thumbnails</guid><title>Photo</title><link>https://example.org/entry</link>
      <description>Body</description>
      <media:content url="{original}" medium="image" type="image/jpeg">
        <media:thumbnail url="{associated_thumbnail}"/>
      </media:content>
      <media:thumbnail url="{standalone_thumbnail}"/>
      </item></channel></rss>'''.encode()

    article = FeedParser().parse(FetchResult("f", "https://example.org/feed", status_code=200, raw_xml=xml)).articles[0]

    images = [block for block in walk(article.content_blocks) if block.type == "image"]
    assert [block.url for block in images] == [original, standalone_thumbnail]
    thumbnails = [item for item in article.metadata["media"] if item.get("role") == "thumbnail"]
    assert thumbnails[0]["associated_url"] == original
    assert "associated_url" not in thumbnails[1]


def test_thumbnail_without_matching_media_original_is_retained_as_an_image():
    thumbnail = "https://cdn.example.org/standalone-thumbnail.jpg"
    xml = f'''<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Feed</title>
      <item><guid>thumbnail-only</guid><title>Preview</title><link>https://example.org/entry</link>
      <description>Body</description><media:thumbnail url="{thumbnail}"/></item>
      </channel></rss>'''.encode()

    article = FeedParser().parse(FetchResult("f", "https://example.org/feed", status_code=200, raw_xml=xml)).articles[0]

    images = [block for block in walk(article.content_blocks) if block.type == "image"]
    assert [block.url for block in images] == [thumbnail]


def test_clean_cached_media_removes_redundant_media_links_but_keeps_ordinary_links():
    video = "https://cdn.example.org/clip.mp4"
    poster = "https://cdn.example.org/poster.jpg"
    article = Article(
        "cached-media",
        "feed",
        "entry",
        "https://trumpstruth.org/entry/3",
        "Video",
        content_blocks=[
            ContentBlock("paragraph"),
            ContentBlock(
                "paragraph", children=[ContentBlock("link", url=video, children=[ContentBlock("text", "View video")])]
            ),
            ContentBlock("link", url="https://example.org/context", children=[ContentBlock("text", "Context")]),
            ContentBlock("video", url=video),
            ContentBlock("image", url=poster, attrs={"role": "thumbnail"}),
        ],
        metadata={
            "media": [
                {"url": video, "medium": "video", "type": "video/mp4"},
                {"url": poster, "role": "thumbnail", "width": 640, "height": 360},
            ]
        },
    )

    cleaned = clean_cached_article(article)
    blocks = list(walk(cleaned.content_blocks))
    assert not any(block.type == "link" and block.url == video for block in blocks)
    assert any(block.type == "link" and block.url == "https://example.org/context" for block in blocks)
    assert not any(block.type == "image" for block in blocks)
    assert next(block for block in blocks if block.type == "video").attrs["poster"] == poster
    assert not is_pure_video_article(cleaned)
