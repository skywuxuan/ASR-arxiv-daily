import datetime as dt
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

import daily_arxiv as daily


def paper(title="Test ASR paper", published="2026-01-02", code_url=None):
    return {
        "title": title,
        "authors": ["Alice", "Bob"],
        "published": published,
        "updated": "2026-02-01",
        "url": "https://arxiv.org/abs/2601.00001",
        "pdf_url": "https://arxiv.org/pdf/2601.00001",
        "code_url": code_url,
        "code_source": "paper" if code_url else None,
    }


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = daily.load_config(daily.ROOT / "config.yaml")
        for key in ("json_path", "md_readme_path", "md_gitpage_path", "md_wechat_path"):
            self.config[key] = self.root / self.config[key].relative_to(daily.ROOT)
        self.config["search_code"] = False

    def archive(self, papers=None):
        return {"last_fetched": "2026-01-02T00:00:00+00:00", "topics": {"ASR": papers or {}}}

    def save(self, archive):
        daily.write_text(self.config["json_path"], json.dumps(archive))

    def test_query_scopes_phrases_and_abbreviations(self):
        query = self.config["queries"]["ASR"]
        self.assertIn('ti:"Speech Recognition" OR abs:"Speech Recognition"', query)
        self.assertIn('(ti:"ASR" OR abs:"ASR") AND (ti:"speech"', query)
        self.assertTrue(query.endswith("AND (cat:cs.CL OR cat:cs.SD OR cat:eess.AS)"))
        self.assertNotIn('"OR', query)

    def test_ambiguous_abbreviation_needs_context(self):
        with self.assertRaisesRegex(ValueError, "context"):
            daily.build_query({"filters": ["speech"], "abbreviations": ["ASR"]})

    def test_fetch_normalizes_versions_and_records_metadata(self):
        result = SimpleNamespace(
            get_short_id=lambda: "2601.00001v3", title="Speech\n Recognition",
            summary="Code: https://github.com/example/asr.git.", comment=None,
            authors=["Alice"], published=dt.datetime(2026, 1, 2),
            updated=dt.datetime(2026, 2, 1),
        )
        client = Mock()
        client.results.return_value = iter([result])
        records = daily.fetch_papers("speech", 5, client)
        self.assertEqual(list(records), ["2601.00001"])
        record = records["2601.00001"]
        self.assertEqual(record["title"], "Speech Recognition")
        self.assertEqual(record["code_url"], "https://github.com/example/asr")
        self.assertEqual(record["published"], "2026-01-02")
        self.assertEqual(record["updated"], "2026-02-01")
        self.assertEqual(record["pdf_url"], "https://arxiv.org/pdf/2601.00001")

    def test_merge_retains_history_and_discovered_code(self):
        previous = {"old": paper(), "same": paper(code_url="https://github.com/example/asr")}
        previous["same"]["code_checked_at"] = "2026-01-01"
        merged = daily.merge_papers(previous, {"same": paper(title="Updated title"), "new": paper()})
        self.assertEqual(set(merged), {"old", "same", "new"})
        self.assertEqual(merged["same"]["title"], "Updated title")
        self.assertEqual(merged["same"]["code_url"], previous["same"]["code_url"])
        self.assertEqual(merged["same"]["code_checked_at"], "2026-01-01")
        self.assertEqual(previous["same"]["title"], "Test ASR paper")

    def test_sort_uses_published_date_instead_of_updated_date_or_id(self):
        records = {"z": paper(published="2025-12-31"), "a": paper(published="2026-01-02")}
        records["z"]["updated"] = "2026-03-01"
        self.assertEqual([key for key, _ in daily.sorted_papers(records)], ["a", "z"])

    def test_markdown_escapes_table_and_html_characters(self):
        record = paper(title="A | B\n<script>alert(1)</script> [x] *bold*")
        text = daily.render_markdown(self.archive({"2601.00001": record}), self.config)
        self.assertIn("A &#124; B &lt;script&gt;", text)
        self.assertNotIn("<script>", text)
        row = next(line for line in text.splitlines() if "2601.00001" in line)
        self.assertEqual(row.count("|"), 8)
        self.assertIn("\\[x\\] \\*bold\\*", text)

    def test_display_switches_and_page_formats(self):
        self.config.update(show_authors=False, show_links=False)
        archive = self.archive({"2601.00001": paper()})
        web = daily.render_markdown(archive, self.config, web=True)
        self.assertTrue(web.startswith("---\nlayout: default\n---"))
        self.assertIn("./README.html#usage", web)
        self.assertNotIn("| 作者 |", web)
        self.assertNotIn("| 代码 |", web)
        wechat = daily.render_markdown(archive, self.config, wechat=True)
        self.assertIn("- 2026-01-02", wechat)
        self.assertNotIn("| ---", wechat)

    @patch.object(daily, "fetch_papers")
    def test_first_run_creates_outputs_and_second_run_deduplicates(self, fetch):
        fetch.return_value = {"2601.00001": paper()}
        self.config["publish_wechat"] = True
        daily.run(self.config)
        fetch.return_value = {"2601.00001": paper(title="Revised"), "2601.00002": paper()}
        daily.run(self.config)
        stored = daily.read_archive(self.config["json_path"])
        self.assertEqual(len(stored["topics"]["ASR"]), 2)
        self.assertEqual(stored["topics"]["ASR"]["2601.00001"]["title"], "Revised")
        self.assertIsNotNone(stored["last_fetched"])
        for key in ("md_readme_path", "md_gitpage_path", "md_wechat_path"):
            self.assertIn("Revised", self.config[key].read_text())

    @patch.object(daily, "CodeFinder")
    @patch.object(daily, "fetch_papers")
    def test_offline_render_leaves_archive_and_fetch_time_unchanged(self, fetch, finder):
        self.config["search_code"] = True
        self.save(self.archive({"2601.00001": paper()}))
        before = self.config["json_path"].read_bytes()
        daily.run(self.config, render_only=True)
        first_stat = self.config["md_readme_path"].stat().st_mtime_ns
        daily.run(self.config, render_only=True)
        self.assertEqual(self.config["json_path"].read_bytes(), before)
        self.assertEqual(self.config["md_readme_path"].stat().st_mtime_ns, first_stat)
        fetch.assert_not_called()
        finder.assert_not_called()

    @patch.object(daily, "fetch_papers")
    def test_late_topic_failure_preserves_archive_and_pages(self, fetch):
        self.save(self.archive({"old": paper()}))
        daily.write_text(self.config["md_readme_path"], "Existing README")
        before = self.config["json_path"].read_bytes()
        self.config["queries"]["Streaming ASR"] = "streaming"
        fetch.side_effect = [{"new": paper()}, RuntimeError("arXiv unavailable")]
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            daily.run(self.config)
        self.assertEqual(self.config["json_path"].read_bytes(), before)
        self.assertEqual(self.config["md_readme_path"].read_text(), "Existing README")

    @patch.object(daily, "fetch_papers")
    @patch.object(daily.CodeFinder, "find")
    def test_weekly_refresh_prioritizes_unchecked_and_oldest_checked(self, find, fetch):
        records = {"old": paper(published="2025-01-01"), "new": paper(), "checked": paper()}
        records["old"]["code_checked_at"] = "2026-01-01"
        records["checked"]["code_checked_at"] = "2026-02-01"
        self.save(self.archive(records))
        self.config["search_code"] = True
        find.return_value = "https://github.com/example/asr"
        daily.run(self.config, update_links=True)
        self.assertEqual([call.args[0] for call in find.call_args_list], ["new", "old", "checked"])
        fetch.assert_not_called()
        saved = daily.read_archive(self.config["json_path"])
        self.assertEqual(saved["last_fetched"], "2026-01-02T00:00:00+00:00")
        self.assertTrue(all(p["code_source"] == "github_search" for p in saved["topics"]["ASR"].values()))

    def test_github_rate_limit_stops_search_without_losing_papers(self):
        finder = daily.CodeFinder(self.config)
        finder.session = Mock()
        finder.session.get.side_effect = requests.HTTPError("403 rate limit")
        with self.assertLogs(daily.LOG, level="WARNING"):
            self.assertIsNone(finder.find("2601.00001"))
        self.assertIsNone(finder.find("2601.00002"))
        self.assertEqual(finder.session.get.call_count, 1)
        self.assertEqual(finder.remaining, 0)

    def test_corrupt_cache_is_not_overwritten(self):
        daily.write_text(self.config["json_path"], "broken json")
        with self.assertRaises(json.JSONDecodeError):
            daily.run(self.config, render_only=True)
        self.assertEqual(self.config["json_path"].read_text(), "broken json")


if __name__ == "__main__":
    unittest.main()
