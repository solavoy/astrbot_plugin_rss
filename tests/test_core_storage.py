import asyncio
from functools import partial

import pytest
from astrbot_plugin_rss.core.models import Article, ContentBlock, now_ts
from astrbot_plugin_rss.storage import Database, Repository


async def test_database_refuses_unknown_newer_schema(tmp_path):
    import aiosqlite

    path = tmp_path / "future.sqlite3"
    connection = await aiosqlite.connect(path)
    await connection.execute("PRAGMA user_version=4")
    await connection.close()
    database = Database(path)
    with pytest.raises(RuntimeError, match="newer than supported"):
        await database.initialize()
    assert database.connection is None


@pytest.mark.asyncio
async def test_feed_translation_flag_defaults_off_and_persists_as_boolean(tmp_path):
    database = Database(tmp_path / "feed-translation.sqlite3")
    await database.initialize()
    try:
        assert (await database.fetchone("PRAGMA user_version"))[0] == 3
        column = await database.fetchone(
            "SELECT dflt_value FROM pragma_table_info('feeds') WHERE name='translation_enabled'"
        )
        assert column[0] == "0"
        repository = Repository(database)
        feed = await repository.create_feed("https://example.org/translation-default")
        assert feed.translation_enabled is False
        enabled = await repository.update_feed(feed.feed_id, translation_enabled=True)
        assert enabled.translation_enabled is True
        with pytest.raises(ValueError, match="translation_enabled must be a boolean"):
            await repository.update_feed(feed.feed_id, translation_enabled="true")
        assert (await repository.get_feed(feed.feed_id)).translation_enabled is True
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_cancelled_transaction_entry_rolls_back_before_releasing_lock(tmp_path, monkeypatch):
    database = Database(tmp_path / "cancelled-entry.sqlite3")
    await database.initialize()
    connection = database.connection
    original_execute = connection._execute
    begin_started = asyncio.Event()

    async def gated_execute(function, *args, **kwargs):
        if args and args[0] == "BEGIN IMMEDIATE":
            future = asyncio.get_running_loop().create_future()
            connection._tx.put_nowait((future, partial(function, *args, **kwargs)))
            begin_started.set()
            return await future
        return await original_execute(function, *args, **kwargs)

    monkeypatch.setattr(connection, "_execute", gated_execute)

    async def enter_transaction():
        async with database.transaction():
            pass

    task = asyncio.create_task(enter_transaction())
    await asyncio.wait_for(begin_started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not database.lock.locked()
    assert not connection._conn.in_transaction
    async with database.transaction() as conn:
        await conn.execute("INSERT INTO settings(key,value) VALUES('after-cancel','ok')")
    assert (await database.fetchone("SELECT value FROM settings WHERE key='after-cancel'"))[0] == "ok"
    await database.close()


@pytest.mark.asyncio
async def test_failed_commit_rolls_back_and_keeps_database_usable(tmp_path, monkeypatch):
    database = Database(tmp_path / "failed-commit.sqlite3")
    await database.initialize()
    connection = database.connection
    original_commit = connection.commit

    async def fail_commit():
        raise OSError("injected commit failure")

    monkeypatch.setattr(connection, "commit", fail_commit)
    with pytest.raises(OSError, match="injected commit failure"):
        async with database.transaction() as conn:
            await conn.execute("INSERT INTO settings(key,value) VALUES('rolled-back','no')")

    assert not connection._conn.in_transaction
    assert await database.fetchone("SELECT value FROM settings WHERE key='rolled-back'") is None
    monkeypatch.setattr(connection, "commit", original_commit)
    async with database.transaction() as conn:
        await conn.execute("INSERT INTO settings(key,value) VALUES('next-transaction','ok')")
    assert (await database.fetchone("SELECT value FROM settings WHERE key='next-transaction'"))[0] == "ok"
    await database.close()


@pytest.mark.asyncio
async def test_failed_rollback_invalidates_connection_even_when_close_raises(tmp_path, monkeypatch):
    database = Database(tmp_path / "failed-rollback.sqlite3")
    await database.initialize()
    connection = database.connection
    original_close = connection.close

    async def fail_rollback():
        raise OSError("injected rollback failure")

    async def close_then_fail():
        await original_close()
        raise OSError("injected close report failure")

    monkeypatch.setattr(connection, "rollback", fail_rollback)
    monkeypatch.setattr(connection, "close", close_then_fail)
    with pytest.raises(ValueError, match="injected body failure"):
        async with database.transaction() as conn:
            await conn.execute("INSERT INTO settings(key,value) VALUES('uncertain','no')")
            raise ValueError("injected body failure")

    assert database.connection is None
    assert not database.lock.locked()


@pytest.fixture
async def repo(tmp_path):
    db = Database(tmp_path / "rss.sqlite3")
    await db.initialize()
    yield Repository(db)
    await db.close()


async def add_article(repo, feed_id, key="a"):
    article = Article(
        "", feed_id, key, f"https://example.org/{key}", key, content_blocks=[ContentBlock("paragraph", text=key)]
    )
    await repo.save_articles(feed_id, [article])
    return article


async def test_shared_fetch_interval_is_minimum_of_active_paths(repo):
    feed = await repo.create_feed("https://example.org/rss", poll_interval=900)
    target = await repo.create_target("p", "o1", "test", "s1")
    await repo.add_direct_subscription(target.target_id, feed.feed_id, 300)
    l600 = await repo.create_list("slow", interval=600)
    await repo.add_list_feed(l600, feed.feed_id)
    await repo.subscribe_list(target.target_id, l600)
    l120 = await repo.create_list("fast", interval=120)
    await repo.add_list_feed(l120, feed.feed_id)
    await repo.subscribe_list(target.target_id, l120)
    unused = await repo.create_list("unused", interval=30)
    await repo.add_list_feed(unused, feed.feed_id)
    due = await repo.due_feeds(1, 300, 60)
    assert len(due) == 1 and due[0]["required_interval"] == 120


async def test_no_interval_uses_global_default_before_minimum(repo):
    feed = await repo.create_feed("https://example.org/no-interval")
    target = await repo.create_target("p", "o2", "test", "s2")
    await repo.add_direct_subscription(target.target_id, feed.feed_id)
    slow = await repo.create_list("slow", interval=600)
    await repo.add_list_feed(slow, feed.feed_id)
    await repo.subscribe_list(target.target_id, slow)
    assert (await repo.due_feeds(1, 300, 60))[0]["required_interval"] == 300


async def test_list_interval_inherits_global_not_feed_override(repo):
    feed = await repo.create_feed("https://example.org/list-global", poll_interval=900)
    target = await repo.create_target("p", "list-global", "test", "list-global")
    list_id = await repo.create_list("global interval", interval=0)
    await repo.add_list_feed(list_id, feed.feed_id)
    await repo.subscribe_list(target.target_id, list_id, interval=0)
    due = await repo.due_feeds(1, 300, 1)
    assert due[0]["required_interval"] == 300


async def test_explicit_list_interval_overrides_feed_interval(repo):
    feed = await repo.create_feed("https://example.org/list-explicit", poll_interval=900)
    target = await repo.create_target("p", "list-explicit", "test", "list-explicit")
    list_id = await repo.create_list("explicit interval", interval=600)
    await repo.add_list_feed(list_id, feed.feed_id)
    await repo.subscribe_list(target.target_id, list_id)
    due = await repo.due_feeds(1, 300, 60)
    assert due[0]["required_interval"] == 600


async def test_zero_and_legacy_zero_intervals_inherit_global_without_becoming_one_second(repo):
    feed = await repo.create_feed("https://example.org/zero-inherits", poll_interval=0)
    target = await repo.create_target("p", "zero-inherits", "test", "zero-inherits")
    list_id = await repo.create_list("zero inherits", interval=0)
    await repo.add_list_feed(list_id, feed.feed_id)
    await repo.subscribe_list(target.target_id, list_id, interval=0)
    await repo.add_direct_subscription(target.target_id, feed.feed_id, interval=0)
    async with repo.db.transaction() as conn:
        await conn.execute("UPDATE feeds SET poll_interval=0 WHERE feed_id=?", (feed.feed_id,))
        await conn.execute("UPDATE subscription_lists SET poll_interval=0 WHERE list_id=?", (list_id,))
        await conn.execute("UPDATE direct_subscriptions SET poll_interval=0 WHERE feed_id=?", (feed.feed_id,))
        await conn.execute("UPDATE list_subscriptions SET poll_interval=0 WHERE list_id=?", (list_id,))
    due = await repo.due_feeds(1, 300, 1)
    assert due[0]["required_interval"] == 300
    assert (await repo.db.fetchone("SELECT poll_interval FROM feeds WHERE feed_id=?", (feed.feed_id,)))[0] == 0


async def test_article_read_adds_actual_feed_title_for_legacy_metadata(repo):
    feed = await repo.create_feed("https://example.org/article-title", title="Actual Feed Title")
    original = await add_article(repo, feed.feed_id, "old-metadata")
    stored = await repo.article(original.article_id)
    assert stored.metadata["feed_title"] == "Actual Feed Title"


async def test_direct_feed_override_and_global_list_interval_take_minimum(repo):
    feed = await repo.create_feed("https://example.org/direct-list-min", poll_interval=900)
    target = await repo.create_target("p", "direct-list-min", "test", "direct-list-min")
    await repo.add_direct_subscription(target.target_id, feed.feed_id)
    list_id = await repo.create_list("global interval")
    await repo.add_list_feed(list_id, feed.feed_id)
    await repo.subscribe_list(target.target_id, list_id)
    due = await repo.due_feeds(1, 300, 60)
    assert due[0]["required_interval"] == 300


async def test_slow_list_cursor_keeps_articles_until_its_own_check(repo):
    feed = await repo.create_feed("https://example.org/slow-path")
    direct_target = await repo.create_target("p", "fast", "test", "fast")
    list_target = await repo.create_target("p", "slow", "test", "slow")
    await repo.add_direct_subscription(direct_target.target_id, feed.feed_id, 60)
    list_id = await repo.create_list("slow", interval=600)
    await repo.add_list_feed(list_id, feed.feed_id)
    await repo.subscribe_list(list_target.target_id, list_id)
    now = now_ts()
    await add_article(repo, feed.feed_id, "first")
    await repo.enqueue_due_paths(now + 1, 300, 60)
    second = await add_article(repo, feed.feed_id, "second")
    await repo.enqueue_due_paths(now + 61, 300, 60)
    assert not any(d["article_id"] == second.article_id for d in await repo.deliveries(target_id=list_target.target_id))
    await repo.enqueue_due_paths(now + 601, 300, 60)
    assert any(d["article_id"] == second.article_id for d in await repo.deliveries(target_id=list_target.target_id))
    assert any(d["article_id"] == second.article_id for d in await repo.deliveries(target_id=direct_target.target_id))


async def test_cancelled_path_continues_for_existing_path_but_new_subscriber_gets_no_history(repo):
    feed = await repo.create_feed("https://example.org/path")
    a = await repo.create_target("p", "a", "test", "a")
    b = await repo.create_target("p", "b", "test", "b")
    await repo.add_direct_subscription(a.target_id, feed.feed_id, 60, send_history=True)
    await repo.add_direct_subscription(b.target_id, feed.feed_id, 600, send_history=True)
    article = await add_article(repo, feed.feed_id)
    assert await repo.enqueue_due_paths(now_ts() + 1, 300, 60) == 2
    await repo.remove_direct_subscription(a.target_id, feed.feed_id)
    assert await repo.delivery_has_active_path(a.target_id, article.article_id) is False
    assert await repo.delivery_has_active_path(b.target_id, article.article_id) is True
    c = await repo.create_target("p", "c", "test", "c")
    await repo.add_direct_subscription(c.target_id, feed.feed_id, send_history=False)
    assert await repo.delivery_has_active_path(c.target_id, article.article_id) is False


async def test_same_target_delivery_survives_cancellation_of_one_qualifying_path(repo):
    feed = await repo.create_feed("https://example.org/shared-path")
    target = await repo.create_target("p", "shared", "test", "shared")
    await repo.add_direct_subscription(target.target_id, feed.feed_id, 60, send_history=True)
    list_id = await repo.create_list("already subscribed", interval=600)
    await repo.add_list_feed(list_id, feed.feed_id)
    await repo.subscribe_list(target.target_id, list_id, 600)
    article = await add_article(repo, feed.feed_id)
    assert await repo.enqueue_due_paths(now_ts() + 1, 300, 60) == 1
    await repo.remove_direct_subscription(target.target_id, feed.feed_id)
    assert await repo.delivery_has_active_path(target.target_id, article.article_id)
    delivery = (await repo.deliveries(target_id=target.target_id))[0]
    info = await repo.delivery_source(target.target_id, article.article_id, delivery["source_path"])
    assert info and info["is_list"] == 1


async def test_list_member_baseline_and_target_level_dedup(repo):
    feed = await repo.create_feed("https://example.org/list")
    old = await add_article(repo, feed.feed_id, "old")
    target = await repo.create_target("p", "target", "test", "target")
    sub1 = await repo.create_list("one")
    sub2 = await repo.create_list("two")
    await repo.add_list_feed(sub1, feed.feed_id)
    await repo.add_list_feed(sub2, feed.feed_id)
    await repo.subscribe_list(target.target_id, sub1)
    await repo.subscribe_list(target.target_id, sub2)
    assert await repo.delivery_has_active_path(target.target_id, old.article_id) is False
    new = await add_article(repo, feed.feed_id, "new")
    assert await repo.enqueue_due_paths(now_ts() + 1, 300, 60) == 1
    deliveries = await repo.deliveries(target_id=target.target_id)
    assert len(deliveries) == 1 and deliveries[0]["article_id"] == new.article_id


async def test_unknown_send_result_does_not_requeue_and_can_be_resolved(repo):
    feed = await repo.create_feed("https://example.org/unknown")
    target = await repo.create_target("p", "unknown", "test", "unknown")
    await repo.add_direct_subscription(target.target_id, feed.feed_id, send_history=True)
    await add_article(repo, feed.feed_id)
    await repo.enqueue_due_paths(now_ts() + 1, 300, 60)
    delivery = (await repo.deliveries(target_id=target.target_id))[0]
    await repo.mark_delivery(delivery["delivery_id"], "sending")
    assert await repo.retry_delivery(delivery["delivery_id"]) is False
    await repo.mark_delivery(delivery["delivery_id"], "unknown", error="uncertain")
    assert await repo.pending_deliveries(now_ts() + 1) == []
    assert await repo.resolve_unknown(delivery["delivery_id"], confirmed_sent=False)
    assert len(await repo.pending_deliveries(now_ts() + 1)) == 1


async def test_pending_and_claimed_deliveries_preserve_article_order(repo):
    feed = await repo.create_feed("https://example.org/order")
    target = await repo.create_target("p", "order", "test", "order")
    await repo.add_direct_subscription(target.target_id, feed.feed_id, send_history=True)
    articles = [
        Article("", feed.feed_id, f"entry-{index}", f"https://example.org/{index}", f"Article {index}")
        for index in range(3)
    ]
    await repo.save_articles(feed.feed_id, articles)
    await repo.enqueue_due_paths(now_ts() + 1, 300, 60)
    pending = await repo.pending_deliveries(now_ts() + 1)
    assert [row["article_id"] for row in pending] == [article.article_id for article in articles]
    claimed = await repo.claim_deliveries([row["delivery_id"] for row in pending], 5)
    assert [row["article_id"] for row in claimed] == [article.article_id for article in articles]


async def test_concurrent_workers_atomically_claim_a_delivery(repo):
    feed = await repo.create_feed("https://example.org/claim")
    target = await repo.create_target("p", "claim", "test", "claim")
    await repo.add_direct_subscription(target.target_id, feed.feed_id, send_history=True)
    await add_article(repo, feed.feed_id)
    await repo.enqueue_due_paths(now_ts() + 1, 300, 60)
    pending = await repo.pending_deliveries(now_ts() + 1)
    delivery_id = pending[0]["delivery_id"]
    left, right = await __import__("asyncio").gather(
        repo.claim_deliveries([delivery_id], 5),
        repo.claim_deliveries([delivery_id], 5),
    )
    assert len(left) + len(right) == 1
