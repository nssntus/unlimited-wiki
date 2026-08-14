from __future__ import annotations

import time
from pathlib import Path

import pytest

from wiki_service import WikiService


@pytest.mark.performance
def test_1000_article_list_and_first_read_under_two_seconds(tmp_path: Path):
    (tmp_path / "wiki").mkdir()
    (tmp_path / "raw").mkdir()
    for category_index in range(10):
        folder = tmp_path / "wiki" / f"category-{category_index}"
        folder.mkdir()
        for article_index in range(100):
            number = category_index * 100 + article_index
            (folder / f"article-{number}.md").write_text(
                f"# Article {number}\n\n> Category: concepts\n> Status: 词条\n\n## 它做什么\n" + "A" * 7800 + "\n\n## 怎么用\nB\n\n## 例子\nC\n",
                encoding="utf-8",
            )
    started = time.perf_counter()
    service = WikiService(tmp_path, start_worker=False)
    try:
        rows = service.articles()
        first = service.read_article(rows[0]["path"])
    finally:
        service.close()
    elapsed = time.perf_counter() - started
    assert len(rows) == 1000 and first["title"]
    assert elapsed <= 2.0
