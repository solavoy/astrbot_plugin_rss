import asyncio
import json
import re
import shutil
import sqlite3
from types import SimpleNamespace

import pytest
from astrbot_plugin_rss.core.config import PluginConfig
from astrbot_plugin_rss.core.models import Article, ContentBlock, now_ts
from astrbot_plugin_rss.core.runtime import RSSRuntime
from astrbot_plugin_rss.services.management import ManagementService

_SECTION = re.compile(
    r"(\[\[RSS_[0-9a-f]{32}_U\d+_BEGIN\]\])\n(.*?)\n"
    r"(\[\[RSS_[0-9a-f]{32}_U\d+_END\]\])",
    re.DOTALL,
)


class StubProvider:
    """A deterministic provider that exercises TranslationService's marker protocol."""

    def __init__(self):
        self.calls = []

    async def llm_generate(self, *, chat_provider_id, prompt, system_prompt):
        self.calls.append((chat_provider_id, prompt, system_prompt))
        sections = []
        for match in _SECTION.finditer(prompt):
            text = match.group(2).replace("Source headline", "译文标题").replace("Source paragraph", "译文正文")
            sections.append(f"{match.group(1)}\n{text}\n{match.group(3)}")
        return SimpleNamespace(completion_text="\n".join(sections))


class FakeFetcher:
    async def close(self):
        pass


class FakeParser:
    pass


class RaisingRegistry:
    async def render(self, *args, **kwargs):
        raise RuntimeError("renderer failed")


def _runtime(data_dir, *, context=None, registry=None, provider_id="provider-one"):
    config = PluginConfig.from_mapping({"translation": {"provider_id": provider_id, "include_original": False}})
    return RSSRuntime(
        config,
        data_dir,
        context=context,
        fetcher=FakeFetcher(),
        parser=FakeParser(),
        registry=registry,
    )


async def _save_article(runtime, feed, entry_id="entry-1"):
    article = Article(
        "",
        feed.feed_id,
        entry_id,
        f"https://example.org/{entry_id}",
        "Source headline",
        content_blocks=[ContentBlock("paragraph", text="Source paragraph")],
    )
    await runtime.repository.save_articles(feed.feed_id, [article])
    return article


def _diagnostic(article_id, feed_id):
    return {
        "article_id": article_id,
        "feed_id": feed_id,
        "status": "translated",
        "reason": None,
        "reasons": [],
        "segments_total": 2,
        "segments_translated": 2,
        "segments_skipped": 0,
        "segments_unchanged": 0,
        "segments_failed": 0,
    }


@pytest.mark.asyncio
async def test_delivery_and_test_push_save_translation_diagnostics_before_renderer_failure_and_after_restart(tmp_path):
    provider = StubProvider()
    runtime = _runtime(tmp_path / "data", context=provider, registry=RaisingRegistry())
    await runtime.initialize()
    feed = await runtime.repository.create_feed(
        "https://example.org/history-feed", title="History", translation_enabled=True
    )
    target = await runtime.repository.create_target("platform", "instance", "private", "session")
    article = await _save_article(runtime, feed)

    operation = await runtime.test_push(feed.feed_id, target.target_id)
    assert operation["status"] == "failed"
    assert operation["translation_diagnostics"][0]["status"] == "translated"
    assert operation["translation_diagnostics"][0]["segments_translated"] == 2

    await runtime.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    assert await runtime.repository.enqueue_due_paths(now_ts() + 1, 300, 60, force=True) == 1
    pending = await runtime.repository.pending_deliveries(now_ts() + 1, 10, 5)
    await runtime._deliver_group(pending, asyncio.Semaphore(1))
    delivery = await runtime.database.fetchone(
        "SELECT delivery_id,translation_diagnostics FROM deliveries WHERE article_id=?", (article.article_id,)
    )
    assert delivery is not None
    raw_diagnostics = json.loads(delivery["translation_diagnostics"])
    assert raw_diagnostics[0]["status"] == "translated"
    assert raw_diagnostics[0]["segments_translated"] == 2
    assert len(provider.calls) == 1  # the real TranslationService result is reused from cache

    await runtime.close()
    restarted = _runtime(tmp_path / "data", context=provider, registry=RaisingRegistry())
    await restarted.initialize()
    management = ManagementService(restarted)
    try:
        feed_detail = await management.get(f"feeds/{feed.public_id}", {})
        operation_history = feed_detail["test_operations"]
        assert operation_history[0]["operation_id"] == operation["operation_id"]
        assert operation_history[0]["translation_diagnostics"][0]["status"] == "translated"

        delivery_page = await management.get("deliveries", {"page": 1, "size": 20})
        listed = next(row for row in delivery_page["items"] if row["delivery_id"] == delivery["delivery_id"])
        assert listed["translation_diagnostics"][0]["segments_translated"] == 2
        delivery_detail = await management.get(f"deliveries/{delivery['delivery_id']}", {})
        assert delivery_detail["delivery"]["translation_diagnostics"][0]["status"] == "translated"
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_translation_history_distinguishes_missing_provider_from_disabled_feed(tmp_path):
    runtime = _runtime(tmp_path / "data", provider_id="")
    await runtime.initialize()
    try:
        enabled = await runtime.repository.create_feed(
            "https://example.org/enabled-no-provider", translation_enabled=True
        )
        disabled = await runtime.repository.create_feed("https://example.org/disabled")
        enabled_article = await _save_article(runtime, enabled)
        disabled_article = await _save_article(runtime, disabled)
        diagnostics = []

        await runtime._articles_for_render([enabled_article, disabled_article], diagnostics=diagnostics)

        by_feed = {item["feed_id"]: item for item in diagnostics}
        assert by_feed[enabled.feed_id]["status"] == "provider_missing"
        assert by_feed[enabled.feed_id]["reason"] == "provider_not_configured"
        assert by_feed[disabled.feed_id]["status"] == "disabled"
        assert by_feed[disabled.feed_id]["reason"] == "feed_translation_disabled"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_new_and_legacy_v3_backups_restore_translation_history(tmp_path):
    source = RSSRuntime({}, tmp_path / "source")
    await source.initialize()
    feed = await source.repository.create_feed("https://example.org/backup-history", translation_enabled=True)
    target = await source.repository.create_target("platform", "instance", "private", "session")
    article = await _save_article(source, feed)
    await source.repository.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    await source.repository.enqueue_due_paths(now_ts() + 1, 300, 60, force=True)
    delivery = await source.database.fetchone(
        "SELECT delivery_id FROM deliveries WHERE article_id=?", (article.article_id,)
    )
    await source.repository.set_delivery_translation_diagnostics(
        delivery["delivery_id"], _diagnostic(article.article_id, feed.feed_id)
    )
    operation_id = "backup-operation"
    await source.repository.create_test_operation(operation_id, target.target_id, feed.feed_id, [article.article_id])
    await source.repository.set_test_operation_translation_diagnostics(
        operation_id, [_diagnostic(article.article_id, feed.feed_id)]
    )
    new_v3 = tmp_path / "new-v3.sqlite3"
    await source.backup(new_v3)
    await source.close()

    old_v3 = tmp_path / "old-v3.sqlite3"
    shutil.copyfile(new_v3, old_v3)
    db = sqlite3.connect(old_v3)
    try:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        db.execute("ALTER TABLE deliveries DROP COLUMN translation_diagnostics")
        db.execute("ALTER TABLE test_operations DROP COLUMN translation_diagnostics")
        db.commit()
    finally:
        db.close()

    restored = RSSRuntime({}, tmp_path / "restored")
    await restored.initialize()
    service = ManagementService(restored)
    try:
        await service._restore_sqlite(new_v3)
        new_delivery = await service.runtime.database.fetchone(
            "SELECT translation_diagnostics FROM deliveries WHERE delivery_id=?", (delivery["delivery_id"],)
        )
        new_operation = await service.runtime.database.fetchone(
            "SELECT translation_diagnostics FROM test_operations WHERE operation_id=?", (operation_id,)
        )
        assert json.loads(new_delivery["translation_diagnostics"])[0]["status"] == "translated"
        assert json.loads(new_operation["translation_diagnostics"])[0]["segments_translated"] == 2
        assert service.runtime._started
    finally:
        await service.runtime.close()

    restored_old = RSSRuntime({}, tmp_path / "restored-old")
    await restored_old.initialize()
    old_service = ManagementService(restored_old)
    try:
        await old_service._restore_sqlite(old_v3)
        old_delivery = await old_service.runtime.database.fetchone(
            "SELECT translation_diagnostics FROM deliveries WHERE delivery_id=?", (delivery["delivery_id"],)
        )
        old_operation = await old_service.runtime.database.fetchone(
            "SELECT translation_diagnostics FROM test_operations WHERE operation_id=?", (operation_id,)
        )
        assert old_delivery["translation_diagnostics"] == "[]"
        assert old_operation["translation_diagnostics"] == "[]"
        assert old_service.runtime._started
    finally:
        await old_service.runtime.close()


@pytest.mark.parametrize("bad_column", ["translation_diagnostics INTEGER NOT NULL DEFAULT '[]'"])
@pytest.mark.parametrize("corrupt_json", [False, True])
@pytest.mark.asyncio
async def test_restore_rejects_malformed_optional_translation_history_without_replacing_runtime(
    tmp_path, bad_column, corrupt_json
):
    runtime = RSSRuntime({}, tmp_path / "data")
    await runtime.initialize()
    service = ManagementService(runtime)
    original_runtime = service.runtime
    feed = await runtime.repository.create_feed("https://example.org/keep-existing")
    valid = tmp_path / "valid.sqlite3"
    await runtime.backup(valid)
    malformed = tmp_path / f"malformed-{corrupt_json}.sqlite3"
    shutil.copyfile(valid, malformed)
    with sqlite3.connect(malformed) as db:
        if corrupt_json:
            # Keep a row so the invalid optional JSON is actually inspected.
            now = now_ts()
            feed_id = db.execute("SELECT feed_id FROM feeds LIMIT 1").fetchone()[0]
            target = db.execute("SELECT target_id FROM targets LIMIT 1").fetchone()
            if target is None:
                target_id = "target-for-corrupt-json"
                db.execute(
                    "INSERT INTO targets(target_id,platform_instance,unified_origin,platform,session_id,session_type,label,routing_key) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (target_id, "instance", "private", "platform", "session", "", "target", "routing"),
                )
            else:
                target_id = target[0]
            article = db.execute("SELECT article_id FROM articles WHERE feed_id=? LIMIT 1", (feed_id,)).fetchone()
            if article is None:
                article_id = "article-for-corrupt-json"
                db.execute(
                    "INSERT INTO articles(article_id,feed_id,entry_id,first_seen_at) VALUES(?,?,?,?)",
                    (article_id, feed_id, article_id, now),
                )
            else:
                article_id = article[0]
            db.execute(
                "INSERT INTO test_operations(operation_id,target_id,feed_id,article_ids,status,error,translation_diagnostics,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                ("corrupt-json", target_id, feed_id, json.dumps([article_id]), "failed", None, "{", now, now),
            )
        else:
            db.execute("ALTER TABLE deliveries DROP COLUMN translation_diagnostics")
            db.execute(f"ALTER TABLE deliveries ADD COLUMN {bad_column}")
        db.commit()

    try:
        with pytest.raises(ValueError, match="translation diagnostics|translation_diagnostics"):
            await service._restore_sqlite(malformed)
        assert service.runtime is original_runtime
        assert await service.runtime.repository.get_feed(feed.feed_id) is not None
        assert (await service.runtime.database.fetchone("PRAGMA quick_check"))[0] == "ok"
    finally:
        await service.runtime.close()
