import json

import pytest

from platform_store import PlatformStore
from publication import article_id_from_markdown, public_markdown, snapshot_fingerprint


def snapshot(markdown: str, **overrides) -> dict:
    return {
        "title": "Cafe\u0301",
        "category": "concepts",
        "content_status": "词条",
        "markdown": markdown,
        **overrides,
    }


def test_public_projection_ignores_private_runtime_metadata_and_equivalent_bytes():
    before = snapshot(
        "# Café\r\n\r\n> Category: concepts\r\n> Status: 词条\r\n\r\nBody\r\n"
    )
    after = snapshot(
        "# Cafe\u0301\n\n> Article-ID: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "> Category: concepts\n> Category-ID: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        "> Status: 词条\n> Classification: confirmed\n"
        "> Classification-Updated: 2026-08-16T10:00:00Z\n"
        "> Generation: model\n> Updated: 2026-08-16\n> Tags: \n\nBody\n\n"
    )
    assert snapshot_fingerprint(before) == snapshot_fingerprint(after)
    projected = public_markdown(after["markdown"])
    assert "Article-ID" not in projected
    assert "Category-ID" not in projected
    assert "Classification-Updated" not in projected
    assert "> Category: concepts" in projected


def test_public_projection_keeps_public_content_changes():
    base = snapshot("# Café\n\n> Category: concepts\n> Status: 词条\n\nBody\n\n## See Also\n")
    changes = [
        snapshot(base["markdown"] + "\nNew body.\n"),
        snapshot(base["markdown"].replace("> Status: 词条", "> Status: 草稿")),
        snapshot(base["markdown"] + "\n> Tags: public-tag\n"),
        snapshot(base["markdown"] + "\n[Related](related.md)\n"),
    ]
    assert all(snapshot_fingerprint(base) != snapshot_fingerprint(item) for item in changes)


def test_article_identity_parser_only_accepts_stable_hex_id():
    assert article_id_from_markdown("> Article-ID: ABCDEF0123456789ABCDEF0123456789\n") == "abcdef0123456789abcdef0123456789"
    assert article_id_from_markdown("> Article-ID: not-an-id\n") is None


def article(article_id: str, markdown: str, *, title: str = "Same title") -> dict:
    return {
        "article_id": article_id,
        "title": title,
        "category": "concepts",
        "content_status": "词条",
        "markdown": markdown,
    }


def publish(platform: PlatformStore, context, value: dict) -> dict:
    public_snapshot = {
        "title": value["title"],
        "category": value["category"],
        "content_status": value["content_status"],
        "markdown": public_markdown(value["markdown"]),
        "summary": "summary",
        "attribution": "Owner",
        "source_summaries": [],
    }
    fingerprint = snapshot_fingerprint(public_snapshot)
    preview = platform.create_preview(
        context,
        f"concepts/{value['article_id']}.md",
        f"revision-{value['article_id']}",
        value["article_id"],
        fingerprint,
        public_snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    platform.ai_decide(submission["id"], "pass", {"summary": "accepted"})
    return platform.admin_decide(context, submission["id"], "approve", "approved")


def test_publication_state_survives_restart_and_ignores_internal_metadata(tmp_path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    article_id = "a" * 32
    original = article(
        article_id,
        "# Same title\n\n> Category: concepts\n> Status: 词条\n\nBody\n",
    )
    approved = publish(platform, context, original)
    with platform.connect() as db:
        before = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()

    migrated = article(
        article_id,
        "# Same title\n\n> Article-ID: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "> Category: concepts\n> Category-ID: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        "> Status: 词条\n> Classification: confirmed\n"
        "> Classification-Updated: 2026-08-16T12:00:00Z\n\nBody\n",
    )
    restarted = PlatformStore(tmp_path)
    state = restarted.article_publication(context, migrated)
    assert state["state"] == "published"
    assert state["publication_fingerprint"].startswith("v1:")
    with restarted.connect() as db:
        after = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()
    assert tuple(after) == tuple(before)
    assert "Article-ID" not in json.loads(after["snapshot_json"])["markdown"]

    changed = {**migrated, "markdown": migrated["markdown"] + "\nVisible update.\n"}
    assert restarted.article_publication(context, changed)["state"] == "update_available"


def test_same_title_articles_keep_distinct_public_identities(tmp_path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    first = article("a" * 32, "# Same title\n\nFirst body.\n")
    second = article("b" * 32, "# Same title\n\nSecond body.\n")

    first_public = publish(platform, context, first)
    assert platform.article_publication(context, first)["state"] == "published"
    assert platform.article_publication(context, second)["state"] == "not_published"

    second_public = publish(platform, context, second)
    assert first_public["public_entry_id"] != second_public["public_entry_id"]
    assert platform.article_publication(context, first)["public_entry_id"] == first_public["public_entry_id"]
    assert platform.article_publication(context, second)["public_entry_id"] == second_public["public_entry_id"]


def test_ambiguous_legacy_entries_do_not_break_startup_or_rewrite_snapshots(tmp_path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    article_id = "c" * 32
    legacy_snapshot = {
        "title": "Legacy title",
        "category": "concepts",
        "content_status": "词条",
        "markdown": f"# Legacy title\n\n> Article-ID: {article_id}\n> Category: concepts\n\nBody\n",
        "summary": "Body",
        "attribution": "Owner",
        "source_summaries": [],
    }
    fingerprint = snapshot_fingerprint(legacy_snapshot)
    submission_ids: list[str] = []
    for index in range(2):
        preview = platform.create_preview(
            context, f"concepts/legacy-{index}.md", f"revision-{index}", article_id, fingerprint, legacy_snapshot,
        )
        submission_ids.append(platform.submit_preview(context, preview["preview_id"])["id"])

    with platform.connect() as db:
        snapshots: list[tuple[str, str]] = []
        for index, submission_id in enumerate(submission_ids):
            entry_id = f"{index + 1:032x}"
            revision_id = f"{index + 11:032x}"
            row = db.execute(
                "SELECT snapshot_json,content_hash FROM submissions WHERE id=?", (submission_id,),
            ).fetchone()
            snapshots.append((row["snapshot_json"], row["content_hash"]))
            db.execute(
                "INSERT INTO public_entries(id,author_id,status,current_revision_id,created_at,updated_at) VALUES(?,?,'published',?,?,?)",
                (entry_id, user["id"], revision_id, "2026-08-16T00:00:00Z", "2026-08-16T00:00:00Z"),
            )
            db.execute(
                "INSERT INTO public_revisions(id,entry_id,submission_id,version,snapshot_json,content_hash,published_at) VALUES(?,?,?,?,?,?,?)",
                (revision_id, entry_id, submission_id, 1, row["snapshot_json"], row["content_hash"], "2026-08-16T00:00:00Z"),
            )
            db.execute(
                "UPDATE submissions SET status='approved',public_entry_id=? WHERE id=?", (entry_id, submission_id),
            )

    restarted = PlatformStore(tmp_path)
    with restarted.connect() as db:
        entries = db.execute(
            "SELECT source_workspace_id,source_article_id FROM public_entries ORDER BY id",
        ).fetchall()
        after = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions ORDER BY entry_id",
        ).fetchall()
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert all(row["source_workspace_id"] is None and row["source_article_id"] is None for row in entries)
    assert [(row["snapshot_json"], row["content_hash"]) for row in after] == snapshots


def test_unique_legacy_publication_is_bound_to_the_only_matching_article(tmp_path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    value = article("d" * 32, "# Same title\n\n> Category: concepts\n> Status: 词条\n\nBody\n")
    approved = publish(platform, context, value)
    with platform.connect() as db:
        before = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()
        db.execute(
            "UPDATE public_entries SET source_workspace_id=NULL,source_article_id=NULL WHERE id=?",
            (approved["public_entry_id"],),
        )
        db.execute(
            "UPDATE submissions SET article_id=NULL WHERE public_entry_id=?",
            (approved["public_entry_id"],),
        )

    result = platform.backfill_workspace_publication_sources(context, [value])
    assert result == {"public_entries": 1, "submissions": 1}
    assert platform.article_publication(context, value)["state"] == "published"
    with platform.connect() as db:
        entry = db.execute(
            "SELECT source_workspace_id,source_article_id FROM public_entries WHERE id=?",
            (approved["public_entry_id"],),
        ).fetchone()
        after = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()
    assert entry["source_workspace_id"] == context.workspace_id
    assert entry["source_article_id"] == value["article_id"]
    assert tuple(after) == tuple(before)


def test_ambiguous_private_articles_never_claim_one_legacy_public_lineage(tmp_path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    first = article("e" * 32, "# Same title\n\n> Category: concepts\n> Status: 词条\n\nFirst body.\n")
    second = article("f" * 32, "# Same title\n\n> Category: concepts\n> Status: 词条\n\nSecond body.\n")
    approved = publish(platform, context, first)
    with platform.connect() as db:
        before_count = db.execute("SELECT COUNT(*) FROM public_revisions").fetchone()[0]
        db.execute(
            "UPDATE public_entries SET source_workspace_id=NULL,source_article_id=NULL WHERE id=?",
            (approved["public_entry_id"],),
        )
        db.execute(
            "UPDATE submissions SET article_id=NULL WHERE public_entry_id=?",
            (approved["public_entry_id"],),
        )

    result = platform.backfill_workspace_publication_sources(context, [first, second])
    assert result == {"public_entries": 0, "submissions": 0}
    assert platform.article_publication(context, first)["state"] == "not_published"
    assert platform.article_publication(context, second)["state"] == "not_published"

    public_snapshot = {
        "title": second["title"],
        "category": second["category"],
        "content_status": second["content_status"],
        "markdown": public_markdown(second["markdown"]),
        "summary": "summary",
        "attribution": "Owner",
        "source_summaries": [],
    }
    preview = platform.create_preview(
        context,
        "concepts/second.md",
        "second-revision",
        second["article_id"],
        snapshot_fingerprint(public_snapshot),
        public_snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    platform.ai_decide(submission["id"], "pass", {"summary": "accepted"})
    with pytest.raises(RuntimeError, match="legacy public entry identity is unresolved"):
        platform.admin_decide(context, submission["id"], "approve", "approved")

    with platform.connect() as db:
        entry = db.execute(
            "SELECT source_workspace_id,source_article_id FROM public_entries WHERE id=?",
            (approved["public_entry_id"],),
        ).fetchone()
        after_count = db.execute("SELECT COUNT(*) FROM public_revisions").fetchone()[0]
        status = db.execute("SELECT status FROM submissions WHERE id=?", (submission["id"],)).fetchone()[0]
    assert entry["source_workspace_id"] is None and entry["source_article_id"] is None
    assert after_count == before_count
    assert status == "pending_admin"
