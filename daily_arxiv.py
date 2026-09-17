"""Generate an ASR paper feed, adapted from TTS-arxiv-daily / cv-arxiv-daily.

The ASR adaptation uses structured records, scoped queries and a shared cache
for README, GitHub Pages and the optional WeChat export.
"""

import argparse
import datetime as dt
import html
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time

import arxiv
import requests
import yaml

LOG = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
GITHUB_REPO = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)", re.I)


def field_query(terms):
    """Search titles and abstracts, quoting phrases and spacing operators."""
    parts = []
    for term in terms:
        if not isinstance(term, str) or not term.strip() or '"' in term:
            raise ValueError("Search terms must be nonempty strings without quotes")
        parts.extend(f'{field}:"{term.strip()}"' for field in ("ti", "abs"))
    return "(" + " OR ".join(parts) + ")"


def build_query(topic):
    filters = topic.get("filters", [])
    if not isinstance(filters, list) or not filters:
        raise ValueError("Each topic needs a nonempty filters list")
    query = field_query(filters)
    abbreviations = topic.get("abbreviations", [])
    if abbreviations:
        context = topic.get("context", [])
        if not context:
            raise ValueError("Abbreviations require context terms")
        query = f"({query} OR ({field_query(abbreviations)} AND {field_query(context)}))"
    categories = topic.get("categories", [])
    if categories:
        if any(not re.fullmatch(r"[\w.-]+", cat) for cat in categories):
            raise ValueError("Invalid arXiv category")
        query += " AND (" + " OR ".join(f"cat:{cat}" for cat in categories) + ")"
    return query


def load_config(filename):
    path = Path(filename).resolve()
    with path.open(encoding="utf-8") as stream:
        config = yaml.safe_load(stream)
    if not isinstance(config, dict) or not config.get("keywords"):
        raise ValueError("Configuration needs a keywords mapping")
    if not isinstance(config.get("max_results"), int) or config["max_results"] < 1:
        raise ValueError("max_results must be a positive integer")
    if config.get("github_max_requests", 10) < 0 or config.get("request_timeout", 20) <= 0:
        raise ValueError("Invalid GitHub request budget or timeout")
    config["queries"] = {
        topic: build_query(settings) for topic, settings in config["keywords"].items()
    }
    for key in ("json_path", "md_readme_path", "md_gitpage_path", "md_wechat_path"):
        config[key] = path.parent / config[key]
    config["repository"] = os.environ.get("GITHUB_REPOSITORY") or config.get("repository", "")
    return config


def read_archive(path):
    if not path.exists():
        return {"last_fetched": None, "topics": {}}
    with path.open(encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get("topics"), dict):
        raise ValueError(f"Invalid archive: {path}")
    return data


def write_text(path, content):
    """Replace each output atomically and avoid touching unchanged files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def github_link(text):
    match = GITHUB_REPO.search(text or "")
    if not match:
        return None
    repo = match.group(1).rstrip(".-")
    if repo.endswith(".git"):
        repo = repo[:-4]
    return "https://github.com/" + repo


class CodeFinder:
    """Bound GitHub search work and stop cleanly when the API is unavailable."""

    def __init__(self, config):
        self.remaining = config.get("github_max_requests", 10)
        self.timeout = config.get("request_timeout", 20)
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json"})
        token = os.environ.get("GITHUB_TOKEN")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.interval = 2.1 if token else 6.1
        self.last_request = None
        self.cache = {}

    def find(self, paper_id):
        if paper_id in self.cache:
            return self.cache[paper_id]
        if self.remaining <= 0:
            return None
        if self.last_request is not None:
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
        self.remaining -= 1
        self.last_request = time.monotonic()
        try:
            response = self.session.get(
                "https://api.github.com/search/repositories",
                params={"q": f'"{paper_id}" in:readme', "sort": "stars", "per_page": 1},
                timeout=self.timeout,
            )
            response.raise_for_status()
            items = response.json().get("items", [])
            link = github_link(items[0].get("html_url", "")) if items else None
        except (requests.RequestException, ValueError, AttributeError, TypeError) as error:
            LOG.warning("GitHub search unavailable; keeping paper data: %s", error)
            self.remaining = 0
            link = None
        self.cache[paper_id] = link
        return link


def fetch_papers(query, max_results, client):
    search = arxiv.Search(query=query, max_results=max_results,
                          sort_by=arxiv.SortCriterion.SubmittedDate,
                          sort_order=arxiv.SortOrder.Descending)
    papers = {}
    for result in client.results(search):
        paper_id = re.sub(r"v\d+$", "", result.get_short_id())
        link = github_link(result.summary) or github_link(result.comment)
        papers[paper_id] = {
            "title": " ".join(result.title.split()),
            "authors": [str(author) for author in result.authors],
            "published": result.published.date().isoformat(),
            "updated": result.updated.date().isoformat(),
            "url": f"https://arxiv.org/abs/{paper_id}",
            "pdf_url": f"https://arxiv.org/pdf/{paper_id}",
            "code_url": link,
            "code_source": "paper" if link else None,
        }
    return papers


def merge_papers(existing, incoming):
    merged = dict(existing)
    for paper_id, paper in incoming.items():
        record = dict(paper)
        previous = existing.get(paper_id, {})
        if previous.get("code_checked_at"):
            record["code_checked_at"] = previous["code_checked_at"]
        if not record.get("code_url") and previous.get("code_url"):
            record["code_url"] = previous["code_url"]
            record["code_source"] = previous.get("code_source")
        merged[paper_id] = record
    return merged


def sorted_papers(papers):
    return sorted(papers.items(), key=lambda item: (item[1]["published"], item[0]),
                  reverse=True)


def markdown_text(value):
    value = html.escape(" ".join(str(value).split()), quote=False)
    value = value.replace("\\", "\\\\")
    for character in "`*_[]":
        value = value.replace(character, "\\" + character)
    return value.replace("|", "&#124;")


def render_markdown(archive, config, web=False, wechat=False):
    name = config.get("project_name", "ASR-arxiv-daily")
    guide = "./README.html#usage" if web else "./docs/README.md#usage"
    if wechat:
        guide = "./README.md#usage"
    lines = ["---", "layout: default", "---", ""] if web else []
    lines += [f"# {name}", "", config.get("description", ""), "",
              f"[使用与部署说明]({guide})", ""]
    repository = config.get("repository")
    if config.get("show_badge") and repository:
        lines += [f"[![Stars](https://img.shields.io/github/stars/{repository})]"
                  f"(https://github.com/{repository})", ""]
    last_fetched = archive.get("last_fetched")
    if last_fetched:
        lines += [f"> 最近成功抓取：{last_fetched}（UTC）", ""]
    else:
        lines += ["> 尚未抓取论文。运行 `python daily_arxiv.py` 或启动 GitHub Actions 后自动更新。", ""]
    lines += ["每 12 小时检索一次，按首次提交日期倒序排列，历史论文会持续保留。", ""]
    if config.get("show_links", True):
        lines += ["代码链接来自论文中的 GitHub 地址或按 arXiv ID 检索的候选仓库，未经人工确认。", ""]
    authors_enabled = config.get("show_authors", True)
    links_enabled = config.get("show_links", True)
    for topic, papers in archive["topics"].items():
        lines += [f"## {markdown_text(topic)}", ""]
        if not papers:
            lines += ["暂无论文。", ""]
            continue
        columns = ["首次提交", "更新日期", "标题"]
        if authors_enabled:
            columns.append("作者")
        columns += ["arXiv", "PDF"]
        if links_enabled:
            columns.append("代码")
        if not wechat:
            lines += ["| " + " | ".join(columns) + " |",
                      "| " + " | ".join("---" for _ in columns) + " |"]
        for paper_id, paper in sorted_papers(papers):
            cells = [paper["published"], paper["updated"], markdown_text(paper["title"])]
            if authors_enabled:
                authors = paper["authors"]
                cells.append(markdown_text(authors[0]) + (" et al." if len(authors) > 1 else "")
                             if authors else "—")
            cells += [f'[{paper_id}]({paper["url"]})', f'[PDF]({paper["pdf_url"]})']
            if links_enabled:
                label = "论文链接" if paper.get("code_source") == "paper" else "候选仓库"
                cells.append(f'[{label}]({paper["code_url"]})' if paper.get("code_url") else "—")
            lines.append("- " + " · ".join(cells) if wechat else "| " + " | ".join(cells) + " |")
        lines.append("")
    lines += ["参考 [TTS-arxiv-daily](https://github.com/liutaocode/TTS-arxiv-daily) 与 "
              "[cv-arxiv-daily](https://github.com/Vincentqyw/cv-arxiv-daily)，按 Apache-2.0 许可发布。", ""]
    return "\n".join(lines)


def run(config, update_links=False, render_only=False, skip_code_search=False):
    archive = read_archive(config["json_path"])
    for topic in config["queries"]:
        archive["topics"].setdefault(topic, {})
    if not render_only and not update_links:
        # Collect every topic before writing so an API failure preserves all outputs.
        client = arxiv.Client(page_size=100, delay_seconds=3, num_retries=3)
        for topic, query in config["queries"].items():
            LOG.info("Fetching %s: %s", topic, query)
            incoming = fetch_papers(query, config["max_results"], client)
            archive["topics"][topic] = merge_papers(archive["topics"][topic], incoming)
            LOG.info("Fetched %d papers for %s", len(incoming), topic)
        archive["last_fetched"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if not render_only and not skip_code_search and config.get("search_code", True):
        finder = CodeFinder(config)
        for papers in archive["topics"].values():
            # Weekly runs start with old records so a small budget can backfill links.
            ordered = sorted_papers(papers)
            if update_links:
                ordered.reverse()
                ordered.sort(key=lambda item: item[1].get("code_checked_at", ""))
            for paper_id, paper in ordered:
                if paper.get("code_url") or finder.remaining <= 0:
                    continue
                link = finder.find(paper_id)
                paper["code_checked_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                if link:
                    paper.update(code_url=link, code_source="github_search")
    outputs = []
    for switch, path, flags in (
        ("publish_readme", "md_readme_path", {}),
        ("publish_gitpage", "md_gitpage_path", {"web": True}),
        ("publish_wechat", "md_wechat_path", {"wechat": True}),
    ):
        if config.get(switch):
            outputs.append((config[path], render_markdown(archive, config, **flags)))
    # Build all markdown before writing the archive or pages.
    if not render_only:
        write_text(config["json_path"], json.dumps(archive, ensure_ascii=False, indent=2) + "\n")
    for path, content in outputs:
        write_text(path, content)
    LOG.info("Generated %d pages", len(outputs))


def main():
    parser = argparse.ArgumentParser(description="Update ASR papers from arXiv")
    parser.add_argument("--config_path", "--config-path", default=ROOT / "config.yaml")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--update_paper_links", "--update-paper-links", action="store_true",
                       help="Search for missing code links without fetching papers")
    modes.add_argument("--render-only", action="store_true", help="Render cached papers offline")
    parser.add_argument("--skip-code-search", action="store_true", help="Skip GitHub API searches")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        run(load_config(args.config_path), update_links=args.update_paper_links,
            render_only=args.render_only, skip_code_search=args.skip_code_search)
    except Exception:
        LOG.exception("Update failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
