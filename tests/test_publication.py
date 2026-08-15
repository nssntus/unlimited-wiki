import hashlib
import json

import pytest

import dynamic_categories as dc
from platform_store import PlatformStore
from publication import article_id_from_markdown, public_markdown, snapshot_fingerprint
from wiki_service import WikiService


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
        "> Generation: model\n> Updated: 2026-08-16\n> Tags: \n\nBody\n"
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


def test_public_projection_preserves_hard_breaks_and_their_fingerprint():
    hard_break = "# Title\n\n> Article-ID: " + "a" * 32 + "\n> Category: concepts\n\nFirst line  \nSecond line\n"
    changed = hard_break.replace("First line  \n", "First line \n")

    assert "First line  \n" in public_markdown(hard_break)
    assert snapshot_fingerprint(snapshot(hard_break)) != snapshot_fingerprint(snapshot(changed))


def test_public_projection_preserves_fenced_code_whitespace_exactly():
    markdown = (
        "# Title\n\n> Article-ID: " + "b" * 32 + "\n> Category: concepts\n\n"
        "```text\nline with spaces   \n\n\nnext line\n```\n"
    )
    projected = public_markdown(markdown)
    assert "```text\nline with spaces   \n\n\nnext line\n```" in projected
    changed = markdown.replace("line with spaces   \n", "line with spaces  \n")
    assert snapshot_fingerprint(snapshot(markdown)) != snapshot_fingerprint(snapshot(changed))


def test_public_projection_keeps_body_quotes_named_like_private_metadata():
    markdown = (
        "# Title\n\n> Article-ID: " + "c" * 32 + "\n> Category: concepts\n\n"
        "Discussion:\n\n> Updated: quoted source value\n> Archived: quoted archive value\n"
    )
    projected = public_markdown(markdown)
    assert "> Updated: quoted source value" in projected
    assert "> Archived: quoted archive value" in projected
    changed = markdown.replace("quoted source value", "different source value")
    assert snapshot_fingerprint(snapshot(markdown)) != snapshot_fingerprint(snapshot(changed))


def test_public_projection_filters_blank_separated_private_metadata_blocks():
    markdown = (
        "# Title\n\n> Article-ID: " + "c" * 32 + "\n"
        "> Category-ID: " + "d" * 32 + "\n> Classification: confirmed\n"
        "> Category: concepts\n\n"
        "> Generation: local+llm; task=" + "e" * 32 + "; state=succeeded\n> Updated: 2026-08-16\n"
        "> Sources: Public source\n\n## Body\n\n> Generation: quoted body value\n"
    )
    projected = public_markdown(markdown)
    assert "task=" + "e" * 32 not in projected
    assert "> Updated: 2026-08-16" not in projected
    assert "> Sources: Public source" in projected
    assert "> Generation: quoted body value" in projected


@pytest.mark.parametrize("field", ["Updated", "Archived", "Generation"])
def test_private_named_quote_immediately_after_canonical_header_is_visible_body(field: str):
    markdown = (
        "# Title\n\n> Article-ID: " + "a" * 32 + "\n"
        "> Category-ID: " + "b" * 32 + "\n> Classification: confirmed\n"
        "> Category: concepts\n\n"
        f"> {field}: quoted source, not metadata\n\nBody\n"
    )
    assert public_markdown(markdown).endswith(f"> {field}: quoted source, not metadata\n\nBody\n")
    changed = markdown.replace("quoted source", "changed source")
    assert snapshot_fingerprint(snapshot(markdown)) != snapshot_fingerprint(snapshot(changed))


def test_unanchored_leading_blockquote_is_visible_body_not_metadata():
    markdown = "# Title\n\n> Updated: quoted source value\n> Archived: quoted archive value\n\nBody\n"
    assert public_markdown(markdown) == markdown
    changed = markdown.replace("quoted source value", "different source value")
    assert snapshot_fingerprint(snapshot(markdown)) != snapshot_fingerprint(snapshot(changed))


@pytest.mark.parametrize("anchor", ["Category: quoted category", "Status: quoted status"])
def test_public_category_or_status_quote_is_not_a_private_metadata_anchor(anchor: str):
    markdown = f"# Title\n\n> {anchor}\n> Updated: quoted date\n\nBody\n"
    assert public_markdown(markdown) == markdown
    changed = markdown.replace("quoted date", "different date")
    assert snapshot_fingerprint(snapshot(markdown)) != snapshot_fingerprint(snapshot(changed))


def test_single_article_id_quote_is_not_a_private_metadata_anchor():
    markdown = "# Title\n\n> Article-ID: " + "a" * 32 + "\n> Updated: quoted date\n\nBody\n"
    assert public_markdown(markdown) == markdown
    assert article_id_from_markdown(markdown) is None
    changed = markdown.replace("quoted date", "different date")
    assert snapshot_fingerprint(snapshot(markdown)) != snapshot_fingerprint(snapshot(changed))


@pytest.mark.parametrize("prefix", ["\n\n", "\ufeff", "\ufeff\n\n"])
def test_private_metadata_is_filtered_after_leading_blank_lines_or_bom(prefix: str):
    markdown = (
        prefix + "# Title\n\n> Article-ID: " + "d" * 32 + "\n"
        "> Category: concepts\n> Category-ID: " + "e" * 32 + "\n"
        "> Classification: confirmed\n\nBody  \nNext\n"
    )
    projected = public_markdown(markdown)
    assert projected.startswith(prefix + "# Title")
    assert "Article-ID" not in projected and "Category-ID" not in projected and "Classification:" not in projected
    assert "Body  \nNext" in projected


def test_wiki_service_does_not_promote_body_article_id_example(tmp_path):
    (tmp_path / "wiki" / "concepts").mkdir(parents=True)
    (tmp_path / "raw").mkdir()
    example_id = "f" * 32
    path = tmp_path / "wiki" / "concepts" / "example.md"
    path.write_text(
        "# Example\n\n> Category: concepts\n> Status: 词条\n\n"
        "Body example:\n\n> Article-ID: " + example_id + "\n",
        encoding="utf-8",
    )
    service = WikiService(tmp_path, start_worker=False)
    try:
        value = service.read_article("concepts/example.md")
    finally:
        service.close()
    assert value["article_id"] and value["article_id"] != example_id
    assert f"> Article-ID: {example_id}" in value["markdown"]
    assert dc.article_id(value["markdown"]) == value["article_id"]


@pytest.mark.parametrize(
    ("markdown", "ending"),
    [("# Title", "\n"), ("\r\n# Title", "\r\n"), ("\ufeff# Title", "\n")],
)
def test_metadata_insertion_separates_h1_at_end_of_file(markdown: str, ending: str):
    article_id = "7" * 32
    updated = dc.ensure_article_metadata(
        markdown, category_id=None, status="pending", article_uuid=article_id,
    )
    assert f"# Title{ending}>" in updated
    assert dc.article_id(updated) == article_id
    assert public_markdown(updated).startswith(markdown + ending)


def test_article_identity_parser_only_accepts_stable_hex_id():
    assert article_id_from_markdown(
        "# Title\n\n> Article-ID: ABCDEF0123456789ABCDEF0123456789\n"
        "> Category-ID: " + "b" * 32 + "\n> Classification: confirmed\n"
    ) == "abcdef0123456789abcdef0123456789"
    assert article_id_from_markdown("# Title\n\n> Article-ID: not-an-id\n") is None
    assert article_id_from_markdown("# Title\n\nBody\n\n> Article-ID: " + "a" * 32 + "\n") is None


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
        db.execute(
            "UPDATE public_revisions SET publication_fingerprint=? WHERE entry_id=?",
            ("v1:" + "0" * 64, approved["public_entry_id"]),
        )

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
    assert state["publication_fingerprint"].startswith("v2:")
    with restarted.connect() as db:
        after = db.execute(
            "SELECT snapshot_json,content_hash,publication_fingerprint FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()
    assert (after["snapshot_json"], after["content_hash"]) == tuple(before)
    assert after["publication_fingerprint"].startswith("v2:")
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


def test_publication_pipeline_preserves_visible_markdown_bytes(tmp_path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    article_id = "9" * 32
    private_markdown = (
        f"# Exact Markdown\n\n> Article-ID: {article_id}\n> Category-ID: " + "8" * 32 + "\n"
        "> Category: concepts\n> Classification: confirmed\n\n"
        "> Generation: quoted source, not metadata\n\nFirst line  \nSecond line\n\n"
        "~~~text\ncode tail   \n\n\n> Updated: code value\n~~~\n\n"
        "> Updated: body quote\n"
    )
    value = article(article_id, private_markdown, title="Exact Markdown")
    approved = publish(platform, context, value)
    expected = public_markdown(private_markdown)

    with platform.connect() as db:
        row = db.execute(
            "SELECT snapshot_json,content_hash FROM public_revisions WHERE entry_id=?",
            (approved["public_entry_id"],),
        ).fetchone()
    stored = json.loads(row["snapshot_json"])
    canonical = json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert stored["markdown"] == expected
    assert "> Generation: quoted source, not metadata" in stored["markdown"]
    assert "First line  \n" in stored["markdown"]
    assert "code tail   \n\n\n> Updated: code value" in stored["markdown"]
    assert "> Updated: body quote" in stored["markdown"]
    assert row["content_hash"] == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_stale_admin_context_cannot_publish_after_role_revocation(tmp_path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    _token, stale_admin = platform.create_session(user["id"])
    value = article("8" * 32, "# Pending\n\n> Category: concepts\n> Status: 词条\n\nBody\n", title="Pending")
    public_snapshot = {
        "title": value["title"],
        "category": value["category"],
        "content_status": value["content_status"],
        "markdown": public_markdown(value["markdown"]),
        "summary": "summary",
        "attribution": "Owner",
        "source_summaries": [],
    }
    preview = platform.create_preview(
        stale_admin, "concepts/pending.md", "revision", value["article_id"],
        snapshot_fingerprint(public_snapshot), public_snapshot,
    )
    submission = platform.submit_preview(stale_admin, preview["preview_id"])
    platform.ai_decide(submission["id"], "pass", {"summary": "accepted"})
    platform.set_role(user["id"], "user")

    with pytest.raises(PermissionError, match="admin role required"):
        platform.admin_decide(stale_admin, submission["id"], "approve", "approved")

    with platform.connect() as db:
        assert db.execute("SELECT status FROM submissions WHERE id=?", (submission["id"],)).fetchone()[0] == "pending_admin"
        assert db.execute("SELECT COUNT(*) FROM public_entries").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM public_revisions").fetchone()[0] == 0


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


def test_admin_cannot_publish_a_legacy_submission_without_stable_identity(tmp_path):
    platform = PlatformStore(tmp_path)
    user, _ = platform.register("owner@example.com", "Owner", "correct-horse-123")
    _token, context = platform.create_session(user["id"])
    value = article("9" * 32, "# Legacy\n\n> Category: concepts\n> Status: 词条\n\nBody\n", title="Legacy")
    public_snapshot = {
        "title": value["title"], "category": value["category"],
        "content_status": value["content_status"], "markdown": public_markdown(value["markdown"]),
        "summary": "summary", "attribution": "Owner", "source_summaries": [],
    }
    preview = platform.create_preview(
        context, "concepts/legacy.md", "legacy-revision", value["article_id"],
        snapshot_fingerprint(public_snapshot), public_snapshot,
    )
    submission = platform.submit_preview(context, preview["preview_id"])
    platform.ai_decide(submission["id"], "pass", {"summary": "accepted"})
    with platform.connect() as db:
        db.execute("UPDATE submissions SET article_id=NULL WHERE id=?", (submission["id"],))
        before_audits = db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0]

    with pytest.raises(RuntimeError, match="legacy submission identity is unresolved"):
        platform.admin_decide(context, submission["id"], "approve", "approved")

    with platform.connect() as db:
        assert db.execute("SELECT status FROM submissions WHERE id=?", (submission["id"],)).fetchone()[0] == "pending_admin"
        assert db.execute("SELECT COUNT(*) FROM public_entries").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM public_revisions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] == before_audits
