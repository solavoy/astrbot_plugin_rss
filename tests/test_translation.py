import asyncio
import re
from dataclasses import replace
from types import SimpleNamespace

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Article, ContentBlock, RenderedMessage, now_ts
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.normalizers.markdown_content import blocks_to_markdown, markdown_tokens
from astrbot_plugin_rss.services.management import ManagementService
from astrbot_plugin_rss.services.translation import TranslationService


class TranslatingContext:
    replacements = {
        "A title": "一则标题",
        "Read ": "阅读",
        "the help page": "帮助页面",
        " now.": " 现在。",
        " after image.": " 图片之后。",
        "First paragraph": "第一段",
        "Second paragraph": "第二段",
        "continued paragraph": "继续段",
        "Original body": "原文正文",
    }

    def __init__(self, *, completion_transform=None, error=None):
        self.calls = []
        self.completion_transform = completion_transform
        self.error = error

    async def llm_generate(self, *, chat_provider_id, prompt, system_prompt):
        self.calls.append((chat_provider_id, prompt, system_prompt))
        if self.error:
            raise self.error
        section = re.compile(
            r"(\[\[RSS_[0-9a-f]{32}_U\d+_BEGIN\]\])\n(.*?)\n"
            r"(\[\[RSS_[0-9a-f]{32}_U\d+_END\]\])",
            re.DOTALL,
        )
        if self.completion_transform:
            return SimpleNamespace(completion_text=self.completion_transform(prompt, section))
        output = []
        for match in section.finditer(prompt):
            value = match.group(2)
            for source, translated in self.replacements.items():
                value = value.replace(source, translated)
            output.append(f"{match.group(1)}\n{value}\n{match.group(3)}")
        return SimpleNamespace(completion_text="\n".join(output))


class FakeFetcher:
    async def close(self):
        pass


class FakeParser:
    def parse(self, result):
        raise AssertionError("not used")


class CaptureRegistry:
    def __init__(self):
        self.calls = []

    async def render(self, articles, target, source_name, is_list=False, batch_id="", **kwargs):
        self.calls.append(list(articles))
        return [
            RenderedMessage(
                "capture",
                "text",
                {"title": articles[0].title, "body": _plain_blocks(articles[0].content_blocks)},
                [article.article_id for article in articles],
                batch_id,
            )
        ]


class SuccessSender:
    async def send(self, target, message, idempotency_key):
        return SimpleNamespace(platform_message_id="sent")


def article_with_inline_resources():
    return Article(
        "article-1",
        "feed-1",
        "entry-1",
        "https://example.org/article",
        "A title",
        author="Author",
        content_blocks=[
            ContentBlock(
                "paragraph",
                children=[
                    ContentBlock("text", text="Read "),
                    ContentBlock(
                        "link",
                        url="https://example.org/help?source=rss",
                        children=[ContentBlock("text", text="the help page")],
                    ),
                    ContentBlock("text", text=" now."),
                    ContentBlock("image", url="https://cdn.example.org/image.png", attrs={"alt": "photo"}),
                    ContentBlock("text", text=" after image."),
                ],
            )
        ],
    )


def _walk(blocks):
    for block in blocks:
        yield block
        yield from _walk(block.children)


def _plain_blocks(blocks):
    if len(blocks) == 1 and blocks[0].type.lower() == "markdown":
        tokens = markdown_tokens(blocks[0].text)
        parts = []
        for token in tokens:
            if token.type == "inline":
                parts.extend(child.content for child in token.children or [] if child.type in {"text", "code_inline"})
            elif token.type in {"fence", "code_block"}:
                parts.append(token.content)
        return "".join(parts)
    values = []
    for block in _walk(blocks):
        if block.type.lower() in {"text", "paragraph", "heading", "quote", "link"}:
            if block.text:
                values.append(block.text)
    return "".join(values)


def _markdown(blocks):
    if len(blocks) == 1 and blocks[0].type.lower() == "markdown":
        return blocks[0].text
    return blocks_to_markdown(blocks)


@pytest.mark.asyncio
@pytest.mark.parametrize("include_original", [True, False])
async def test_translation_preserves_nested_emphasis_links_and_inline_code(include_original):
    source = Article(
        "styled-article",
        "feed-1",
        "styled-entry",
        "https://example.org/styled",
        "A title",
        content_blocks=[
            ContentBlock(
                "paragraph",
                children=[
                    ContentBlock("text", text="Read "),
                    ContentBlock(
                        "strong",
                        children=[
                            ContentBlock("text", text="First paragraph "),
                            ContentBlock(
                                "link",
                                url="https://example.org/help?source=rss",
                                children=[
                                    ContentBlock("emphasis", children=[ContentBlock("text", text="the help page")])
                                ],
                            ),
                            ContentBlock("text", text=" "),
                            ContentBlock("inline_code", text="Original body **literal**"),
                        ],
                    ),
                    ContentBlock("text", text=" now."),
                ],
            )
        ],
    )
    original_blocks = [block.to_dict() for block in source.content_blocks]

    outcome = await TranslationService(TranslatingContext(), "provider-one").translate_article(
        source, include_original=include_original
    )

    assert outcome.succeeded is True
    assert [block.to_dict() for block in source.content_blocks] == original_blocks
    markdown = _markdown(outcome.article.content_blocks)
    assert len(outcome.article.content_blocks) == 1
    assert "第一段" in markdown
    assert "帮助页面" in markdown
    assert "**第一段" in markdown
    assert "https://example.org/help?source=rss" in markdown
    assert markdown.count("https://example.org/help?source=rss") == 1
    assert "Original body **literal**" in markdown
    assert markdown.count("Original body **literal**") == 1
    assert "[[RSS_" not in markdown


@pytest.mark.asyncio
async def test_markdown_translation_pairs_paragraphs_without_duplicate_resources_or_code():
    video_url = "https://cdn.example.org/video.mp4"
    image_url = "https://cdn.example.org/photo.png"
    source = Article(
        "markdown-resources",
        "feed-1",
        "markdown-resources-entry",
        "https://example.org/markdown",
        "A title",
        content_blocks=[
            ContentBlock(
                "markdown",
                text=(
                    f"First paragraph with **First paragraph** `**literal**` [watch]({video_url}) "
                    f"![photo]({image_url})\n\nSecond paragraph"
                ),
            )
        ],
    )
    source_hash = source.content_hash
    context = TranslatingContext()

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=True)

    assert outcome.succeeded is True
    markdown = _markdown(outcome.article.content_blocks)
    assert markdown.index("First paragraph") < markdown.index("第一段")
    assert markdown.index("Second paragraph") < markdown.index("第二段")
    assert markdown.count(video_url) == 1
    assert markdown.count(image_url) == 1
    assert markdown.index(video_url) < markdown.index(image_url) < markdown.index("Second paragraph")
    assert markdown.count("`**literal**`") == 1
    assert "**第一段**" in markdown
    assert "[[RSS_" not in markdown
    assert source.content_hash == source_hash
    assert source.content_blocks[0].text.startswith("First paragraph with **First paragraph**")


@pytest.mark.asyncio
@pytest.mark.parametrize("include_original", [True, False])
@pytest.mark.parametrize(
    ("markdown", "expected_structure", "expected_translations"),
    [
        ("> First paragraph\n> Second paragraph", "blockquote_open", ("第一段", "第二段")),
        (
            "- First paragraph\n  continued paragraph",
            "list_item_open",
            ("第一段", "继续段"),
        ),
    ],
)
async def test_multiline_markdown_translation_maps_quote_and_list_source_spans(
    include_original, markdown, expected_structure, expected_translations
):
    context = TranslatingContext()
    source = Article(
        "multiline-markdown",
        "feed-1",
        "multiline-markdown-entry",
        "https://example.org/multiline",
        "A title",
        content_blocks=[ContentBlock("markdown", text=markdown)],
    )

    outcome = await TranslationService(context, "provider-one").translate_article(
        source, include_original=include_original
    )

    assert context.calls
    assert outcome.succeeded is True
    result = _markdown(outcome.article.content_blocks)
    tokens = markdown_tokens(result)
    assert any(token.type == expected_structure for token in tokens)
    assert all(value in result for value in expected_translations)
    if include_original:
        assert "First paragraph" in result
        if expected_structure == "blockquote_open":
            assert "Second paragraph" in result
        else:
            assert "continued paragraph" in result
    else:
        assert "First paragraph" not in result
        assert "Second paragraph" not in result
        assert "continued paragraph" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "duplicate", "reordered"])
async def test_invalid_formatting_markers_fall_back_to_unmodified_article(damage):
    def damage_markers(prompt, section):
        output = []
        for match in section.finditer(prompt):
            text = match.group(2).replace("First paragraph", "第一段")
            pair = re.findall(r"\[\[RSS_[^\]]+_F\d+_(?:BEGIN|END)\]\]", text)
            if pair:
                if damage == "missing":
                    text = text.replace(pair[0], "")
                elif damage == "duplicate":
                    text = text.replace(pair[0], pair[0] * 2)
                else:
                    text = (
                        text.replace(pair[0], "TEMP_MARKER").replace(pair[1], pair[0]).replace("TEMP_MARKER", pair[1])
                    )
            output.append(f"{match.group(1)}\n{text}\n{match.group(3)}")
        return "\n".join(output)

    source = Article(
        "styled-fallback",
        "feed-1",
        "styled-fallback-entry",
        "https://example.org/styled",
        "A title",
        content_blocks=[
            ContentBlock(
                "paragraph",
                children=[ContentBlock("strong", children=[ContentBlock("text", text="First paragraph")])],
            )
        ],
    )

    outcome = await TranslationService(
        TranslatingContext(completion_transform=damage_markers), "provider-one"
    ).translate_article(source, include_original=False)

    assert outcome.succeeded is False
    assert outcome.article is source
    assert source.content_blocks[0].children[0].children[0].text == "First paragraph"


@pytest.mark.asyncio
async def test_default_configuration_does_not_call_translation_provider(tmp_path):
    context = TranslatingContext()
    article = article_with_inline_resources()
    runtime = RSSRuntime(PluginConfig.from_mapping({}), tmp_path, context=context)

    result = await runtime._articles_for_render([article])

    assert result[0] is article
    assert context.calls == []


@pytest.mark.asyncio
async def test_translation_provider_does_not_run_for_missing_or_disabled_source(tmp_path):
    context = TranslatingContext()
    runtime = RSSRuntime(PluginConfig(translation_provider_id="provider-one"), tmp_path, context=context)
    await runtime.initialize()
    missing_source_article = article_with_inline_resources()

    missing_result = await runtime._articles_for_render([missing_source_article])
    disabled_feed = await runtime.repository.create_feed("https://example.org/disabled")
    disabled_article = replace(missing_source_article, article_id="disabled-article", feed_id=disabled_feed.feed_id)
    disabled_result = await runtime._articles_for_render([disabled_article])

    assert missing_result[0] is missing_source_article
    assert disabled_result[0] is disabled_article
    assert context.calls == []
    await runtime.close()


@pytest.mark.asyncio
async def test_translation_uses_selected_provider_and_pairs_paragraphs_without_mutating_source():
    context = TranslatingContext()
    service = TranslationService(context, "provider-one")
    source = Article(
        "article-2",
        "feed-1",
        "entry-2",
        "https://example.org/article",
        "A title",
        author="Author",
        content_blocks=[
            ContentBlock(
                "paragraph",
                children=[
                    ContentBlock("paragraph", text="First paragraph"),
                    ContentBlock("paragraph", text="Second paragraph"),
                    ContentBlock("video", url="https://cdn.example.org/video.mp4"),
                ],
            )
        ],
    )
    source_hash = source.content_hash

    outcome = await service.translate_article(source, include_original=True)

    assert outcome.succeeded is True
    assert context.calls[0][0] == "provider-one"
    assert "[简体中文]" in context.calls[0][2]
    assert context.calls[0][2].count("[简体中文]") == 1
    assert "A title" not in context.calls[0][2]
    assert "A title" in context.calls[0][1]
    assert outcome.article is not source
    assert outcome.article.title == "A title\n一则标题"
    body = _markdown(outcome.article.content_blocks)
    assert body.index("First paragraph") < body.index("第一段")
    assert body.index("Second paragraph") < body.index("第二段")
    assert body.count("https://cdn.example.org/video.mp4") == 1
    assert source.title == "A title"
    assert source.content_hash == source_hash
    assert [child.text for child in source.content_blocks[0].children[:2]] == ["First paragraph", "Second paragraph"]


@pytest.mark.asyncio
async def test_include_original_false_translates_link_label_and_keeps_target_and_media():
    context = TranslatingContext()
    source = article_with_inline_resources()

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=False)

    assert outcome.succeeded is True
    assert outcome.article.title == "一则标题"
    assert len(outcome.article.content_blocks) == 1
    markdown = _markdown(outcome.article.content_blocks)
    assert "Read " not in markdown
    assert "the help page" not in markdown
    assert "[帮助页面](<https://example.org/help?source=rss>)" in markdown
    assert "![photo](<https://cdn.example.org/image.png>)" in markdown
    assert _plain_blocks(outcome.article.content_blocks) == "阅读帮助页面 现在。 图片之后。"


@pytest.mark.asyncio
async def test_title_urls_are_protected_and_restored_instead_of_leaking_markers():
    context = TranslatingContext()
    source = Article(
        "article-url-title",
        "feed-1",
        "entry-url-title",
        "https://example.org/article",
        "A title https://example.org/source",
    )

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=False)

    assert outcome.succeeded is True
    assert outcome.article.title == "一则标题 https://example.org/source"
    assert "[[RSS_" not in outcome.article.title


@pytest.mark.asyncio
async def test_title_is_plain_text_and_keeps_literal_markdown_characters():
    source = Article(
        "article-plain-title",
        "feed-1",
        "entry-plain-title",
        "https://example.org/article",
        "A title **literal** https://example.org/source",
        content_blocks=[ContentBlock("markdown", text="Original body")],
    )

    outcome = await TranslationService(TranslatingContext(), "provider-one").translate_article(
        source, include_original=False
    )

    assert outcome.succeeded is True
    assert outcome.article.title == "一则标题 **literal** https://example.org/source"


@pytest.mark.asyncio
async def test_translated_plain_markdown_punctuation_stays_literal_after_token_text_decoding():
    source = Article(
        "article-markdown-literals",
        "feed-1",
        "entry-markdown-literals",
        "https://example.org/article",
        "A title",
        content_blocks=[
            ContentBlock("markdown", text=r"Price \*literal\* and \[brackets\] plus \| label. First paragraph")
        ],
    )

    outcome = await TranslationService(TranslatingContext(), "provider-one").translate_article(
        source, include_original=False
    )

    assert outcome.succeeded is True
    markdown = _markdown(outcome.article.content_blocks)
    assert r"\*literal\*" in markdown
    assert r"\[brackets\]" in markdown
    assert r"\|" in markdown
    assert "第一段" in markdown
    tokens = markdown_tokens(markdown)
    inline = [child for token in tokens if token.type == "inline" for child in token.children or []]
    assert not any(token.type in {"link_open", "strong_open", "em_open"} for token in inline)


@pytest.mark.asyncio
async def test_unknown_rss_marker_after_expected_sections_falls_back_to_original():
    def append_unknown_marker(prompt, section):
        output = [f"{match.group(1)}\n{match.group(2)}\n{match.group(3)}" for match in section.finditer(prompt)]
        return "\n".join(output) + "\n[[RSS_UNEXPECTED]]"

    context = TranslatingContext(completion_transform=append_unknown_marker)
    source = article_with_inline_resources()

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=False)

    assert outcome.succeeded is False
    assert outcome.article is source
    assert outcome.article.title == "A title"
    assert _plain_blocks(outcome.article.content_blocks).startswith("Read the help page now.")


@pytest.mark.asyncio
async def test_object_only_response_for_prose_unit_falls_back_to_original():
    def remove_prose(prompt, section):
        output = []
        for match in section.finditer(prompt):
            value = match.group(2)
            if "Original body" in value:
                value = re.search(r"\[\[RSS_[^\]]+_O\d+\]\]", value).group(0)
            else:
                value = value.replace("A title", "一则标题")
            output.append(f"{match.group(1)}\n{value}\n{match.group(3)}")
        return "\n".join(output)

    context = TranslatingContext(completion_transform=remove_prose)
    source = Article(
        "article-prose-and-image",
        "feed-1",
        "entry-prose-and-image",
        "https://example.org/article",
        "A title",
        content_blocks=[
            ContentBlock(
                "paragraph",
                children=[
                    ContentBlock("text", text="Original body"),
                    ContentBlock("image", url="https://cdn.example.org/image.png"),
                ],
            )
        ],
    )

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=False)

    assert outcome.succeeded is False
    assert outcome.article is source
    assert _plain_blocks(outcome.article.content_blocks) == "Original body"


@pytest.mark.asyncio
async def test_marker_only_response_for_pure_url_unit_is_allowed():
    source = Article(
        "article-pure-url-title",
        "feed-1",
        "entry-pure-url-title",
        "https://example.org/article",
        "https://example.org/source",
    )

    outcome = await TranslationService(TranslatingContext(), "provider-one").translate_article(
        source, include_original=False
    )

    assert outcome.succeeded is True
    assert outcome.article.title == "https://example.org/source"


@pytest.mark.asyncio
@pytest.mark.parametrize("include_original", [True, False])
async def test_skip_keeps_target_language_title_and_resource_paragraph_once(include_original):
    def skip_every_unit(prompt, section):
        output = []
        for match in section.finditer(prompt):
            skip = re.sub(r"_BEGIN\]\]$", "_SKIP]]", match.group(1))
            output.append(f"{match.group(1)}\n{skip}\n{match.group(3)}")
        return "\n".join(output)

    context = TranslatingContext(completion_transform=skip_every_unit)
    prompt = "Translate into French; preserve meaning and tone."
    source = Article(
        "article-french",
        "feed-1",
        "entry-french",
        "https://example.org/article",
        "Bonjour",
        content_blocks=[
            ContentBlock(
                "paragraph",
                children=[
                    ContentBlock("text", text="Bonjour"),
                    ContentBlock(
                        "link",
                        url="https://example.org/help",
                        children=[ContentBlock("text", text="aide")],
                    ),
                    ContentBlock("image", url="https://cdn.example.org/image.png"),
                ],
            )
        ],
    )

    outcome = await TranslationService(context, "provider-one", prompt=prompt).translate_article(
        source, include_original=include_original
    )

    assert outcome.succeeded is True
    assert outcome.article.title == "Bonjour"
    assert _markdown(outcome.article.content_blocks) == blocks_to_markdown(source.content_blocks)
    assert "Translate into French" in context.calls[0][2]
    assert "简体中文" not in context.calls[0][2]
    skip_markers = re.findall(r"\[\[RSS_[0-9a-f]{32}_U\d+_SKIP\]\]", context.calls[0][1])
    assert len(skip_markers) == 2


@pytest.mark.asyncio
async def test_skip_and_translation_can_coexist_for_different_units():
    def skip_french_title_translate_body(prompt, section):
        output = []
        for match in section.finditer(prompt):
            value = match.group(2)
            if value == "Bonjour":
                value = re.sub(r"_BEGIN\]\]$", "_SKIP]]", match.group(1))
            else:
                value = "Corps original"
            output.append(f"{match.group(1)}\n{value}\n{match.group(3)}")
        return "\n".join(output)

    context = TranslatingContext(completion_transform=skip_french_title_translate_body)
    source = Article(
        "article-mixed-units",
        "feed-1",
        "entry-mixed-units",
        "https://example.org/article",
        "Bonjour",
        content_blocks=[ContentBlock("paragraph", text="Original body")],
    )

    outcome = await TranslationService(
        context, "provider-one", prompt="Translate into French; preserve existing French text."
    ).translate_article(source, include_original=False)

    assert outcome.succeeded is True
    assert outcome.article.title == "Bonjour"
    assert _plain_blocks(outcome.article.content_blocks) == "Corps original"
    assert "简体中文" not in context.calls[0][2]
    assert len(re.findall(r"\[\[RSS_[0-9a-f]{32}_U\d+_SKIP\]\]", context.calls[0][1])) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prompt", "source_title", "translated_title"),
    [
        ("Translate into French.", "Hello", "Bonjour"),
        ("Translate into English.", "Bonjour", "Hello"),
    ],
)
async def test_custom_prompt_controls_target_language_without_chinese_instruction(
    prompt, source_title, translated_title
):
    def translate_title(_prompt, section):
        return "\n".join(
            f"{match.group(1)}\n{match.group(2).replace(source_title, translated_title)}\n{match.group(3)}"
            for match in section.finditer(_prompt)
        )

    context = TranslatingContext(completion_transform=translate_title)
    source = Article(
        "article-custom-language",
        "feed-1",
        "entry-custom-language",
        "https://example.org/article",
        source_title,
    )

    outcome = await TranslationService(context, "provider-one", prompt=prompt).translate_article(
        source, include_original=False
    )

    assert outcome.succeeded is True
    assert outcome.article.title == translated_title
    assert prompt in context.calls[0][2]
    assert "简体中文" not in context.calls[0][2]
    assert source_title in context.calls[0][1]
    assert source_title not in context.calls[0][2]


@pytest.mark.asyncio
async def test_exact_source_response_is_treated_as_skip_even_when_original_is_hidden():
    context = TranslatingContext(
        completion_transform=lambda prompt, section: "\n".join(
            f"{match.group(1)}\n{match.group(2)}\n{match.group(3)}" for match in section.finditer(prompt)
        )
    )
    source = Article(
        "article-source-equal",
        "feed-1",
        "entry-source-equal",
        "https://example.org/article",
        "Already English",
        content_blocks=[
            ContentBlock(
                "paragraph",
                children=[
                    ContentBlock("text", text="Already English"),
                    ContentBlock("image", url="https://cdn.example.org/image.png"),
                ],
            )
        ],
    )

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=False)

    assert outcome.succeeded is True
    assert outcome.article.title == "Already English"
    assert _markdown(outcome.article.content_blocks) == blocks_to_markdown(source.content_blocks)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_skip", ["plus_text", "wrong_unit", "duplicate"])
async def test_malformed_or_misplaced_skip_marker_falls_back_to_original(invalid_skip):
    def invalid_response(prompt, section):
        matches = list(section.finditer(prompt))
        output = []
        for index, match in enumerate(matches):
            value = match.group(2).replace("A title", "一则标题").replace("Original body", "原文正文")
            if index == 0:
                own_skip = re.sub(r"_BEGIN\]\]$", "_SKIP]]", match.group(1))
                if invalid_skip == "plus_text":
                    value = own_skip + " unexpected"
                elif invalid_skip == "wrong_unit":
                    value = re.sub(r"_BEGIN\]\]$", "_SKIP]]", matches[1].group(1))
                else:
                    value = f"{own_skip}\n{own_skip}"
            output.append(f"{match.group(1)}\n{value}\n{match.group(3)}")
        return "\n".join(output)

    context = TranslatingContext(completion_transform=invalid_response)
    source = article_with_inline_resources()

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=False)

    assert outcome.succeeded is False
    assert outcome.article is source


@pytest.mark.asyncio
async def test_translation_cleans_discourse_footer_before_building_provider_prompt():
    context = TranslatingContext()
    canonical = "https://forum.example/t/topic/42/1"
    source = Article(
        "article-discourse",
        "feed-1",
        "entry-discourse",
        canonical,
        "A title",
        content_blocks=[
            ContentBlock("paragraph", text="Original body"),
            ContentBlock("paragraph", text="2 posts - 1 participants"),
            ContentBlock(
                "paragraph",
                children=[
                    ContentBlock(
                        "link",
                        url=canonical,
                        children=[ContentBlock("text", text="Read full topic")],
                    )
                ],
            ),
        ],
        metadata={"source_adapter": "discourse"},
    )

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=False)

    assert outcome.succeeded is True
    assert "participants" not in context.calls[0][1]
    assert "Read full topic" not in context.calls[0][1]
    assert _plain_blocks(outcome.article.content_blocks) == "原文正文"


@pytest.mark.asyncio
async def test_cached_media_cleanup_prevents_view_video_label_from_being_translated():
    context = TranslatingContext()
    video_url = "https://cdn.example.org/video.mp4"
    source = Article(
        "article-media-label",
        "feed-1",
        "entry-media-label",
        "https://example.org/article",
        "A title",
        content_blocks=[
            ContentBlock("link", text="View video", url=video_url),
            ContentBlock("video", url=video_url),
        ],
        metadata={"media": [{"url": video_url, "medium": "video"}]},
    )

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=False)

    assert outcome.succeeded is True
    assert "View video" not in context.calls[0][1]
    markdown = _markdown(outcome.article.content_blocks)
    assert markdown.count(video_url) == 1
    assert "View video" not in markdown


@pytest.mark.asyncio
async def test_list_item_and_table_cell_translation_preserve_container_counts():
    context = TranslatingContext()
    source = Article(
        "article-structures",
        "feed-1",
        "entry-structures",
        "https://example.org/article",
        "A title",
        content_blocks=[
            ContentBlock(
                "list",
                children=[
                    ContentBlock("list_item", text="First paragraph"),
                    ContentBlock("list_item", text="Second paragraph"),
                ],
            ),
            ContentBlock(
                "table",
                children=[
                    ContentBlock(
                        "table_row",
                        children=[
                            ContentBlock("table_cell", text="First paragraph"),
                            ContentBlock("table_cell", text="Second paragraph"),
                        ],
                    )
                ],
            ),
        ],
    )

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=True)

    assert outcome.succeeded is True
    markdown = _markdown(outcome.article.content_blocks)
    tokens = markdown_tokens(markdown)
    assert sum(token.type == "list_item_open" for token in tokens) == 2
    assert markdown.count("第一段") == 2
    assert markdown.count("第二段") == 2
    assert "| First paragraph | Second paragraph |" in markdown
    assert r"\| 第一段 \| 第二段 \|" in markdown


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exception", "empty", "malformed", "duplicate"])
async def test_provider_failures_fall_back_to_original_even_when_originals_are_hidden(failure):
    if failure == "exception":
        context = TranslatingContext(error=RuntimeError("provider offline"))
    elif failure == "empty":
        context = TranslatingContext(completion_transform=lambda _prompt, _section: "")
    elif failure == "duplicate":

        def duplicate_marker(prompt, section):
            output = []
            for match in section.finditer(prompt):
                value = match.group(2)
                token = re.search(r"\[\[RSS_[^\]]+\]\]", value)
                if token:
                    value = value[: token.end()] + token.group(0) + value[token.end() :]
                output.append(f"{match.group(1)}\n{value}\n{match.group(3)}")
            return "\n".join(output)

        context = TranslatingContext(completion_transform=duplicate_marker)
    else:
        context = TranslatingContext(completion_transform=lambda prompt, section: section.sub("", prompt))
    source = article_with_inline_resources()

    outcome = await TranslationService(context, "provider-one").translate_article(source, include_original=False)

    assert outcome.succeeded is False
    assert outcome.article is source
    assert outcome.article.title == "A title"
    assert _plain_blocks(outcome.article.content_blocks).startswith("Read the help page now.")


@pytest.mark.asyncio
async def test_provider_timeout_returns_original_without_waiting_for_long_timeout():
    class HangingContext:
        async def llm_generate(self, **kwargs):
            await asyncio.Future()

    source = article_with_inline_resources()
    outcome = await TranslationService(HangingContext(), "provider-one", timeout_seconds=0.001).translate_article(
        source, include_original=False
    )

    assert outcome.succeeded is False
    assert outcome.article is source


@pytest.mark.asyncio
async def test_formal_delivery_and_test_push_use_translation_cache_and_preserve_database_article(tmp_path):
    context = TranslatingContext()
    registry = CaptureRegistry()
    config = PluginConfig.from_mapping({"translation": {"provider_id": "provider-one", "include_original": False}})
    runtime = RSSRuntime(
        config,
        tmp_path,
        context=context,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=registry,
        sender=SuccessSender(),
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/feed", title="Example", translation_enabled=True)
    target = await runtime.repository.create_target("platform", "instance", "private", "session")
    source = Article(
        "",
        feed.feed_id,
        "entry-1",
        "https://example.org/article",
        "A title",
        content_blocks=[ContentBlock("paragraph", text="Original body")],
    )
    await runtime.repository.save_articles(feed.feed_id, [source])

    pushed = await runtime.test_push(feed.feed_id, target.target_id)
    assert pushed["status"] == "success"
    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    rows = await runtime.repository.pending_deliveries(now_ts() + 1, 10, 5)
    await runtime._deliver_group(rows, asyncio.Semaphore(1))

    assert len(registry.calls) == 2
    assert all(call[0].title == "一则标题" for call in registry.calls)
    assert all("原文正文" in _plain_blocks(call[0].content_blocks) for call in registry.calls)
    assert len(context.calls) == 1
    stored = await runtime.repository.article(source.article_id)
    assert stored.title == "A title"
    assert stored.content_blocks[0].text == "Original body"
    assert stored.content_hash == source.content_hash
    assert (await runtime.repository.deliveries(target_id=target.target_id))[0]["status"] == "success"
    await runtime.close()


@pytest.mark.asyncio
async def test_translation_cache_reuses_text_but_keeps_current_article_metadata(tmp_path):
    context = TranslatingContext()
    runtime = RSSRuntime(
        PluginConfig(translation_provider_id="provider-one"),
        tmp_path,
        context=context,
    )
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/cache", translation_enabled=True)
    source = Article(
        "article-cache",
        feed.feed_id,
        "entry-cache",
        "https://example.org/article",
        "A title",
        author="Original author",
        published_at=1,
        content_blocks=[ContentBlock("paragraph", text="Original body")],
        metadata={"feed_title": "Old feed title"},
    )

    first = (await runtime._articles_for_render([source]))[0]
    updated_source = replace(source, author="Updated author", published_at=2, metadata={"feed_title": "New feed title"})
    second = (await runtime._articles_for_render([updated_source]))[0]

    assert len(context.calls) == 1
    assert second.title == first.title
    assert second.content_blocks == first.content_blocks
    assert second.author == "Updated author"
    assert second.published_at == 2
    assert second.metadata == {"feed_title": "New feed title"}

    await runtime.repository.update_feed(feed.feed_id, translation_enabled=False)
    disabled = (await runtime._articles_for_render([updated_source]))[0]
    assert disabled is updated_source
    assert disabled.title == "A title"
    assert len(context.calls) == 1

    await runtime.repository.update_feed(feed.feed_id, translation_enabled=True)
    reenabled = (await runtime._articles_for_render([updated_source]))[0]
    assert reenabled.title == second.title
    assert len(context.calls) == 1

    runtime.config.translation_prompt = "Translate into French."
    french = (await runtime._articles_for_render([updated_source]))[0]
    assert len(context.calls) == 2
    assert "Translate into French" in context.calls[1][2]
    assert "简体中文" not in context.calls[1][2]
    assert french.title == second.title
    await runtime.close()


@pytest.mark.asyncio
async def test_mixed_source_list_translates_only_enabled_sources_and_preview_stays_raw(tmp_path):
    context = TranslatingContext()
    registry = CaptureRegistry()
    runtime = RSSRuntime(
        PluginConfig(translation_provider_id="provider-one", translation_include_original=False),
        tmp_path,
        context=context,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=registry,
        sender=SuccessSender(),
    )
    await runtime.initialize()
    enabled_feed = await runtime.repository.create_feed("https://example.org/enabled", translation_enabled=True)
    disabled_feed = await runtime.repository.create_feed("https://example.org/disabled-list")
    target = await runtime.repository.create_target("platform", "list", "private", "session")
    list_id = await runtime.repository.create_list("mixed sources", interval=60)
    await runtime.repository.add_list_feed(list_id, enabled_feed.feed_id)
    await runtime.repository.add_list_feed(list_id, disabled_feed.feed_id)
    await runtime.repository.subscribe_list(target.target_id, list_id)
    enabled_article = Article(
        "enabled-article",
        enabled_feed.feed_id,
        "enabled-entry",
        "https://example.org/enabled/article",
        "A title",
        content_blocks=[ContentBlock("paragraph", text="Original body")],
    )
    disabled_article = Article(
        "disabled-article",
        disabled_feed.feed_id,
        "disabled-entry",
        "https://example.org/disabled/article",
        "Disabled title",
        content_blocks=[ContentBlock("paragraph", text="Disabled body")],
    )
    await runtime.repository.save_articles(enabled_feed.feed_id, [enabled_article])
    await runtime.repository.save_articles(disabled_feed.feed_id, [disabled_article])
    async with runtime.database.transaction() as conn:
        await conn.execute("UPDATE list_feeds SET baseline_seq=0 WHERE list_id=?", (list_id,))
        await conn.execute(
            "UPDATE list_subscriptions SET baseline_seq=0,last_seq=0,next_check_at=0 WHERE list_id=?", (list_id,)
        )

    preview = await ManagementService(runtime)._preview_list(list_id, None)
    assert preview["preview_only"] is True
    assert context.calls == []
    assert {article.title for article in registry.calls[-1]} == {"A title", "Disabled title"}

    registry.calls.clear()
    feed_lookups = {enabled_feed.feed_id: 0, disabled_feed.feed_id: 0}
    get_feed = runtime.repository.get_feed

    async def counted_get_feed(feed_id):
        feed_lookups[feed_id] += 1
        return await get_feed(feed_id)

    runtime.repository.get_feed = counted_get_feed
    await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60)
    rows = await runtime.repository.pending_deliveries(now_ts() + 1, 10, 5)
    await runtime._deliver_group(rows, asyncio.Semaphore(1))

    assert feed_lookups == {enabled_feed.feed_id: 1, disabled_feed.feed_id: 1}
    assert len(context.calls) == 1
    rendered = {article.article_id: article for article in registry.calls[0]}
    assert rendered[enabled_article.article_id].title == "一则标题"
    assert rendered[disabled_article.article_id].title == "Disabled title"
    assert (await runtime.repository.article(enabled_article.article_id)).title == "A title"
    await runtime.close()
