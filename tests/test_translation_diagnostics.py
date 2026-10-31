from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from collections import deque
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Article, ContentBlock
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.normalizers.markdown_content import article_markdown, clean_cached_article
from astrbot_plugin_rss.services.translation import TranslationService

_SECTIONS = re.compile(
    r"(\[\[RSS_[0-9a-f]{32}_U\d+_BEGIN\]\])\n(.*?)\n"
    r"(\[\[RSS_[0-9a-f]{32}_U\d+_END\]\])",
    re.DOTALL,
)


class ScriptedContext:
    def __init__(self, responder=None):
        self.responder = responder
        self.calls: list[dict] = []

    async def llm_generate(self, *, chat_provider_id, prompt, system_prompt):
        self.calls.append({"provider": chat_provider_id, "prompt": prompt, "system_prompt": system_prompt})
        response = self.responder(prompt, len(self.calls)) if self.responder else _translated_response(prompt)
        if asyncio.iscoroutine(response):
            response = await response
        return SimpleNamespace(completion_text=response)


def _translated_response(prompt: str) -> str:
    output = []
    for match in _SECTIONS.finditer(prompt):
        value = match.group(2)
        for source, translated in (
            ("A title", "一则标题"),
            ("First paragraph", "第一段"),
            ("Second paragraph", "第二段"),
            ("Good body", "成功正文"),
            ("Body to damage", "损坏正文"),
            ("Timed body", "超时后的正文"),
            ("First chunk", "第一块"),
            ("Second chunk", "第二块"),
        ):
            value = value.replace(source, translated)
        output.append(f"{match.group(1)}\n{value}\n{match.group(3)}")
    return "\n".join(output)


def _article(article_id: str, title: str = "A title", body: str = "First paragraph") -> Article:
    return Article(
        article_id,
        "feed-1",
        f"entry-{article_id}",
        f"https://example.org/{article_id}",
        title,
        content_blocks=[ContentBlock("markdown", text=body)],
    )


def _single_unit_request_limit(article: Article) -> int:
    """Find the input size that fits each current title/body unit on its own."""
    probe = TranslationService(None, "provider")
    nonce = "a" * 32
    normalized = clean_cached_article(article)
    units = []
    title = probe._make_unit(
        [SimpleNamespace(type="text", content=normalized.title, attrs={}, markup="")],
        nonce,
        0,
        plain_text=True,
    )
    if title is not None:
        units.append(title)
    _patches, body_units = probe._collect_body_units(article_markdown(normalized), nonce, len(units))
    units.extend(body_units)
    return max(len(probe._input_prompt([unit], nonce)) for unit in units)


@pytest.mark.asyncio
async def test_bad_marker_in_one_short_unit_keeps_other_translations():
    def damage_only_body(prompt, _call_number):
        output = []
        for match in _SECTIONS.finditer(prompt):
            value = match.group(2)
            if "Body to damage" in value:
                value = "[[RSS_UNKNOWN]]"
            else:
                value = value.replace("A title", "一则标题")
            output.append(f"{match.group(1)}\n{value}\n{match.group(3)}")
        return "\n".join(output)

    source = _article("short-bad-marker", body="Body to damage")
    context = ScriptedContext(damage_only_body)
    outcome = await TranslationService(context, "provider").translate_article(source, include_original=False)

    assert outcome.status == "partial"
    assert outcome.succeeded is False
    assert outcome.article.title == "一则标题"
    assert outcome.article.content_blocks[0].text == "Body to damage"
    assert outcome.diagnostic()["reason"] == "invalid_response"
    assert outcome.diagnostic()["segments_translated"] == 1
    assert outcome.diagnostic()["segments_failed"] == 1


@pytest.mark.asyncio
async def test_provider_error_on_one_bounded_chunk_preserves_other_chunks():
    source = _article(
        "partial-provider-error",
        body="First paragraph with enough text\n\nSecond paragraph with enough text",
    )

    def fail_second_chunk(prompt, call_number):
        if "Second paragraph" in prompt:
            raise RuntimeError("provider unavailable")
        return _translated_response(prompt)

    context = ScriptedContext(fail_second_chunk)
    limit = _single_unit_request_limit(source)
    outcome = await TranslationService(context, "provider", max_segment_chars=limit, max_requests=8).translate_article(
        source, include_original=False
    )

    assert outcome.status == "partial"
    assert outcome.diagnostic()["reason"] == "provider_error"
    assert outcome.diagnostic()["segments_translated"] == 2
    assert outcome.diagnostic()["segments_failed"] == 1
    assert "一则标题" == outcome.article.title
    assert "First paragraph" not in outcome.article.content_blocks[0].text
    assert "Second paragraph with enough text" in outcome.article.content_blocks[0].text
    assert len(context.calls) == 3
    assert all(len(call["prompt"]) <= limit for call in context.calls)


@pytest.mark.asyncio
async def test_timeout_keeps_timed_out_unit_and_continues_without_retry():
    source = _article("timeout-one-unit", body="Timed body")
    limit = _single_unit_request_limit(source)

    class FirstRequestHangs(ScriptedContext):
        async def llm_generate(self, *, chat_provider_id, prompt, system_prompt):
            self.calls.append({"provider": chat_provider_id, "prompt": prompt, "system_prompt": system_prompt})
            if len(self.calls) == 1:
                await asyncio.Future()
            return SimpleNamespace(completion_text=_translated_response(prompt))

    context = FirstRequestHangs()
    outcome = await TranslationService(
        context,
        "provider",
        timeout_seconds=0.01,
        total_timeout_seconds=1,
        max_segment_chars=limit,
    ).translate_article(source, include_original=False)

    assert outcome.status == "partial"
    assert outcome.diagnostic()["reason"] == "timeout"
    assert outcome.diagnostic()["segments_failed"] == 1
    assert outcome.article.title == "A title"
    assert outcome.article.content_blocks[0].text == "超时后的正文"
    assert len(context.calls) == 2


@pytest.mark.asyncio
async def test_request_limit_and_total_deadline_are_reported_and_bound_work():
    source = _article(
        "request-limit",
        body="First paragraph with enough text\n\nSecond paragraph with enough text",
    )
    limit = _single_unit_request_limit(source)
    context = ScriptedContext()
    limited = await TranslationService(context, "provider", max_segment_chars=limit, max_requests=1).translate_article(
        source, include_original=False
    )

    assert limited.status == "partial"
    assert limited.diagnostic()["reason"] == "request_limit"
    assert limited.diagnostic()["segments_failed"] == 2
    assert len(context.calls) == 1
    assert len(context.calls[0]["prompt"]) <= limit

    class FirstRequestHangs(ScriptedContext):
        async def llm_generate(self, *, chat_provider_id, prompt, system_prompt):
            self.calls.append({"provider": chat_provider_id, "prompt": prompt, "system_prompt": system_prompt})
            await asyncio.Future()

    timeout_context = FirstRequestHangs()
    timed = await TranslationService(
        timeout_context,
        "provider",
        timeout_seconds=1,
        total_timeout_seconds=0.01,
        max_segment_chars=limit,
    ).translate_article(source, include_original=False)

    assert timed.status == "failed"
    assert timed.diagnostic()["reason"] == "total_timeout"
    assert timed.diagnostic()["segments_failed"] == 3
    assert len(timeout_context.calls) == 1


@pytest.mark.asyncio
async def test_long_plain_segment_is_split_and_every_request_respects_limit():
    source = _article("long-plain", body="word " * 400)

    def translate_chunks(prompt, _call_number):
        output = []
        for index, match in enumerate(_SECTIONS.finditer(prompt)):
            value = match.group(2)
            value = "一则标题" if value == "A title" else f"译文{index}"
            output.append(f"{match.group(1)}\n{value}\n{match.group(3)}")
        return "\n".join(output)

    limit = 400
    context = ScriptedContext(translate_chunks)
    outcome = await TranslationService(
        context,
        "provider",
        max_segment_chars=limit,
        max_requests=64,
        total_timeout_seconds=2,
    ).translate_article(source, include_original=False)

    assert outcome.succeeded is True
    assert len(context.calls) > 2
    assert all(len(call["prompt"]) <= limit for call in context.calls)
    translated_body = outcome.article.content_blocks[0].text
    assert "译文" in translated_body
    assert "word" not in translated_body
    assert outcome.diagnostic()["segments_total"] == 2


@pytest.mark.asyncio
async def test_oversized_markdown_unit_is_retained_with_specific_diagnostic():
    source = Article(
        "large-markdown-unit",
        "feed-1",
        "entry-large-markdown-unit",
        "https://example.org/large-markdown-unit",
        "A title",
        content_blocks=[ContentBlock("markdown", text=f"See [{'label ' * 100}](https://example.org/resource)")],
    )
    context = ScriptedContext()
    outcome = await TranslationService(context, "provider", max_segment_chars=400).translate_article(
        source, include_original=False
    )

    assert outcome.status == "partial"
    assert outcome.diagnostic()["reason"] == "segment_too_large"
    assert outcome.diagnostic()["segments_failed"] == 1
    assert "label label" in outcome.article.content_blocks[0].text
    assert len(context.calls) == 1
    assert len(context.calls[0]["prompt"]) <= 400


@pytest.mark.asyncio
async def test_unchanged_model_output_is_diagnostic_failure_and_is_not_cached(tmp_path):
    def echo_response(prompt, _call_number):
        return "\n".join(
            f"{match.group(1)}\n{match.group(2)}\n{match.group(3)}" for match in _SECTIONS.finditer(prompt)
        )

    context = ScriptedContext(echo_response)
    runtime = RSSRuntime(PluginConfig(translation_provider_id="provider"), tmp_path, context=context)
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/unchanged", translation_enabled=True)
    source = Article(
        "unchanged-cache",
        feed.feed_id,
        "unchanged-cache-entry",
        "https://example.org/unchanged-cache",
        "Already English",
        content_blocks=[ContentBlock("markdown", text="Still English")],
    )

    first_diagnostics: list[dict] = []
    second_diagnostics: list[dict] = []
    first = await runtime._articles_for_render([source], diagnostics=first_diagnostics)
    second = await runtime._articles_for_render([source], diagnostics=second_diagnostics)

    assert first[0] is source and second[0] is source
    assert first_diagnostics[0]["status"] == "skipped"
    assert first_diagnostics[0]["reason"] == "unchanged_response"
    assert len(context.calls) == 2
    assert not runtime._translation_cache
    await runtime.close()


@pytest.mark.asyncio
async def test_partial_translation_is_retried_on_next_render_instead_of_cached(tmp_path):
    def bad_body_response(prompt, _call_number):
        output = []
        for match in _SECTIONS.finditer(prompt):
            value = "[[RSS_UNKNOWN]]" if "First paragraph" in match.group(2) else "一则标题"
            output.append(f"{match.group(1)}\n{value}\n{match.group(3)}")
        return "\n".join(output)

    context = ScriptedContext(bad_body_response)
    runtime = RSSRuntime(PluginConfig(translation_provider_id="provider"), tmp_path, context=context)
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/partial-retry", translation_enabled=True)
    source = Article(
        "partial-not-cached",
        feed.feed_id,
        "partial-not-cached-entry",
        "https://example.org/partial-not-cached",
        "A title",
        content_blocks=[ContentBlock("markdown", text="First paragraph")],
    )

    first_diagnostics: list[dict] = []
    second_diagnostics: list[dict] = []
    await runtime._articles_for_render([source], diagnostics=first_diagnostics)
    first_call_count = len(context.calls)
    await runtime._articles_for_render([source], diagnostics=second_diagnostics)

    assert first_diagnostics[0]["status"] == second_diagnostics[0]["status"] == "partial"
    assert first_diagnostics[0]["reason"] == second_diagnostics[0]["reason"] == "invalid_response"
    assert first_call_count > 1
    assert len(context.calls) == first_call_count * 2
    assert not runtime._translation_cache
    await runtime.close()


@pytest.mark.asyncio
async def test_translation_preserves_source_metadata_hash_and_nested_values():
    source = _article("metadata-copy", body="First paragraph")
    source.metadata = {"source": {"id": 7}, "labels": ["original"]}
    original_metadata = deepcopy(source.metadata)
    original_hash = source.content_hash

    outcome = await TranslationService(ScriptedContext(), "provider").translate_article(source, include_original=False)

    assert outcome.succeeded is True
    assert outcome.article is not source
    assert outcome.article.metadata == original_metadata
    assert source.metadata == original_metadata
    assert outcome.article.content_hash == original_hash
    assert source.content_hash == original_hash
    assert source.title == "A title"
    assert source.content_blocks[0].text == "First paragraph"


@pytest.mark.asyncio
async def test_concurrent_translation_is_deduplicated_and_runtime_close_cancels_it(tmp_path):
    class GatedContext(ScriptedContext):
        def __init__(self):
            super().__init__()
            self.gates: deque[tuple[asyncio.Event, asyncio.Event]] = deque()
            self.cancelled = 0

        def gate_next_call(self):
            started, release = asyncio.Event(), asyncio.Event()
            self.gates.append((started, release))
            return started, release

        async def llm_generate(self, *, chat_provider_id, prompt, system_prompt):
            self.calls.append({"provider": chat_provider_id, "prompt": prompt, "system_prompt": system_prompt})
            if self.gates:
                started, release = self.gates.popleft()
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise
            return SimpleNamespace(completion_text=_translated_response(prompt))

    context = GatedContext()
    runtime = RSSRuntime(PluginConfig(translation_provider_id="provider"), tmp_path, context=context)
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/concurrent", translation_enabled=True)
    source = Article(
        "concurrent-translation",
        feed.feed_id,
        "concurrent-entry",
        "https://example.org/concurrent-translation",
        "A title",
        content_blocks=[ContentBlock("markdown", text="First paragraph")],
    )

    started, release = context.gate_next_call()
    first = asyncio.create_task(runtime._articles_for_render([source]))
    second = asyncio.create_task(runtime._articles_for_render([source]))
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert len(context.calls) == 1
    release.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result[0].title == second_result[0].title == "A title\n一则标题"
    assert len(context.calls) == 1

    close_source = replace(source, article_id="cancel-on-close", entry_id="cancel-on-close")
    close_started, _close_release = context.gate_next_call()
    waiting = asyncio.create_task(runtime._articles_for_render([close_source]))
    await asyncio.wait_for(close_started.wait(), timeout=1)
    await runtime.close()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert context.cancelled == 1
    assert not runtime._translation_tasks
    assert not runtime._translation_cache


@pytest.mark.asyncio
async def test_cancelling_one_waiter_does_not_cancel_shared_translation(tmp_path):
    class GatedContext(ScriptedContext):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.cancelled = False

        async def llm_generate(self, *, chat_provider_id, prompt, system_prompt):
            self.calls.append({"provider": chat_provider_id, "prompt": prompt, "system_prompt": system_prompt})
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return SimpleNamespace(completion_text=_translated_response(prompt))

    context = GatedContext()
    runtime = RSSRuntime(PluginConfig(translation_provider_id="provider"), tmp_path, context=context)
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/waiters", translation_enabled=True)
    source = Article(
        "shared-waiters",
        feed.feed_id,
        "shared-waiters-entry",
        "https://example.org/shared-waiters",
        "A title",
        content_blocks=[ContentBlock("markdown", text="First paragraph")],
    )

    get_feed = runtime.repository.get_feed
    feed_lookups = 0
    feed_lookups_ready = asyncio.Event()

    async def wait_for_both_feed_lookups(feed_id):
        nonlocal feed_lookups
        feed = await get_feed(feed_id)
        feed_lookups += 1
        if feed_lookups == 2:
            feed_lookups_ready.set()
        await feed_lookups_ready.wait()
        return feed

    runtime.repository.get_feed = wait_for_both_feed_lookups
    cancelled_waiter = asyncio.create_task(runtime._articles_for_render([source]))
    surviving_waiter = asyncio.create_task(runtime._articles_for_render([source]))
    await asyncio.wait_for(context.started.wait(), timeout=1)
    await asyncio.sleep(0)
    cancelled_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_waiter
    assert not context.cancelled
    assert len(context.calls) == 1
    context.release.set()

    result = await surviving_waiter
    assert result[0].title == "A title\n一则标题"
    assert not context.cancelled
    assert len(context.calls) == 1
    assert len(runtime._translation_cache) == 1
    await runtime.close()


@pytest.mark.asyncio
async def test_translation_cache_evicts_least_recently_used_entry(tmp_path):
    context = ScriptedContext()
    runtime = RSSRuntime(PluginConfig(translation_provider_id="provider"), tmp_path, context=context)
    await runtime.initialize()
    feed = await runtime.repository.create_feed("https://example.org/lru", translation_enabled=True)
    articles = [
        Article(
            f"lru-{index}",
            feed.feed_id,
            f"lru-entry-{index}",
            f"https://example.org/lru/{index}",
            "A title",
            content_blocks=[ContentBlock("markdown", text="First paragraph")],
        )
        for index in range(256)
    ]

    await runtime._articles_for_render(articles)
    assert len(runtime._translation_cache) == 256
    await runtime._articles_for_render([articles[0]])  # Promote the oldest item.
    newest = Article(
        "lru-256",
        feed.feed_id,
        "lru-entry-256",
        "https://example.org/lru/256",
        "A title",
        content_blocks=[ContentBlock("markdown", text="First paragraph")],
    )
    await runtime._articles_for_render([newest])

    assert len(runtime._translation_cache) == 256
    assert any(key[3] == articles[0].article_id for key in runtime._translation_cache)
    assert not any(key[3] == articles[1].article_id for key in runtime._translation_cache)
    await runtime._articles_for_render([articles[1]])
    assert len(context.calls) == 258
    assert any(key[3] == articles[1].article_id for key in runtime._translation_cache)
    await runtime.close()


def test_translation_diagnostic_and_error_labels_in_node_vm():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is needed for the Plugin View VM smoke test")
    app_path = Path(__file__).resolve().parents[1] / "views" / "rss" / "app.js"
    script = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const sandbox = {
  PetiteVue: { createApp: () => ({ mount: () => {} }) },
  window: { addEventListener: () => {} },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sandbox);
(async () => {
  const app = sandbox.App();
  const record = vm.runInContext("({ translation_diagnostics: [{ status: 'partial', reason: 'timeout', segments_total: 3, segments_translated: 1, segments_skipped: 0, segments_unchanged: 0, segments_failed: 1 }] })", sandbox);
  await app.perform(async () => { throw new Error('backend diagnostic is visible'); });
  const performError = app.error;
  app.ready = true;
  app.route = '/feeds/41';
  app.load = async () => {};
  let postCount = 0;
  app.post = async () => {
    postCount++;
    if (postCount === 1) return { status: 'error', error: 'feed diagnostic preserved' };
    return { operation_id: 'test-operation', status: 'failed', error: 'send failed', translation_diagnostics: record.translation_diagnostics };
  };
  app.notice = '检查完成，新增 0 篇文章';
  await app.feedAction('refresh');
  const refreshError = app.error;
  const refreshNotice = app.notice;
  app.error = '';
  app.targetId = 'test-target';
  await app.feedAction('test');
  console.log(JSON.stringify({
    error: performError,
    summary: app.translationSummary(record),
    counts: app.translationCounts(record.translation_diagnostics[0]),
    refreshError,
    refreshNotice,
    testError: app.error,
  }));
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run([node, "-e", script, str(app_path)], capture_output=True, text=True, check=True, timeout=10)
    payload = json.loads(result.stdout)

    assert payload["error"] == "backend diagnostic is visible"
    assert payload["summary"] == "部分翻译 · 分段请求超时"
    assert payload["counts"] == "含译文 1 段 · 明确跳过 0 · 原样返回 0 · 含失败 1 段（总 3 段；分段可有重叠）"
    assert payload["refreshError"] == "feed diagnostic preserved"
    assert payload["refreshNotice"] == ""
    assert "部分翻译 · 分段请求超时" in payload["testError"]
