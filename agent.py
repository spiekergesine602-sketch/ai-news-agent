import argparse
import email.utils
import html
import json
import os
import re
import smtplib
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
ENV_PATH = ROOT / ".env"
STATE_PATH = ROOT / "data" / "state.json"


@dataclass
class NewsItem:
    id: str
    source: str
    title: str
    url: str
    summary: str
    published_at: str
    score: int
    category: str


class GitHubTrendingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_article = False
        self.depth = 0
        self.current_links: list[str] = []
        self.current_text: list[str] = []
        self.articles: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "article":
            self.in_article = True
            self.depth = 1
            self.current_links = []
            self.current_text = []
            return
        if not self.in_article:
            return
        self.depth += 1
        if tag == "a" and attrs_dict.get("href"):
            self.current_links.append(attrs_dict["href"] or "")

    def handle_endtag(self, tag: str) -> None:
        if not self.in_article:
            return
        self.depth -= 1
        if tag == "article" or self.depth <= 0:
            text = " ".join(part.strip() for part in self.current_text if part.strip())
            self.articles.append({"links": self.current_links, "text": re.sub(r"\s+", " ", text)})
            self.in_article = False

    def handle_data(self, data: str) -> None:
        if self.in_article:
            self.current_text.append(data)


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def request_bytes(url: str, headers: dict[str, str] | None = None, timeout: int = 45) -> bytes:
    request_headers = {
        "User-Agent": "AIHotNewsEmailAgent/1.0 (+local digest bot)",
        **(headers or {}),
    }
    req = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        detail = detail[:500]
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc


def request_text(url: str, headers: dict[str, str] | None = None) -> str:
    return request_bytes(url, headers=headers).decode("utf-8", errors="replace")


def request_json(url: str, headers: dict[str, str] | None = None, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    data = None
    request_headers = {
        "User-Agent": "AIHotNewsEmailAgent/1.0 (+local digest bot)",
        **(headers or {}),
    }
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=request_headers, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        detail = detail[:500]
        raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc


def parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).astimezone(timezone.utc)
    except Exception:
        return None


def strip_markup(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def keyword_match(text: str, keywords: list[str]) -> bool:
    lower_text = text.lower()
    for keyword in keywords:
        normalized = keyword.lower().strip()
        if not normalized:
            continue
        if len(normalized) <= 3:
            if re.search(rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", lower_text):
                return True
        elif normalized in lower_text:
            return True
    return False


def find_xml_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node.iter():
        local_name = child.tag.rsplit("}", 1)[-1].lower()
        if local_name in names and child.text:
            return child.text.strip()
    return ""


def find_xml_link(node: ET.Element) -> str:
    for child in node.iter():
        local_name = child.tag.rsplit("}", 1)[-1].lower()
        if local_name == "link":
            href = child.attrib.get("href")
            if href:
                return href.strip()
            if child.text:
                return child.text.strip()
    return ""


def fetch_rss_items(config: dict[str, Any], seen_ids: set[str]) -> list[NewsItem]:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=int(config.get("lookback_hours", 24)))
    max_items = int(config.get("max_items_per_feed", 8))
    items: list[NewsItem] = []

    for feed in config.get("rss_feeds", []):
        source = feed["name"]
        category = feed.get("category", "official_blog")
        try:
            root = ET.fromstring(request_bytes(feed["url"]))
        except Exception as exc:
            print(f"WARN: failed to fetch RSS {source}: {exc}", file=sys.stderr)
            continue

        entries = [
            node
            for node in root.iter()
            if node.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}
        ]
        for entry in entries[: max_items * 2]:
            title = find_xml_text(entry, ("title",))
            url = find_xml_link(entry)
            published = find_xml_text(entry, ("published", "updated", "pubdate"))
            published_dt = parse_datetime(published)
            item_id = find_xml_text(entry, ("id", "guid")) or url or title
            if not title or not url or item_id in seen_ids:
                continue
            if published_dt and published_dt < cutoff:
                continue
            summary = find_xml_text(entry, ("summary", "description", "content"))
            items.append(
                NewsItem(
                    id=f"rss:{item_id}",
                    source=source,
                    title=strip_markup(title),
                    url=url,
                    summary=strip_markup(summary)[:900],
                    published_at=published,
                    score=50,
                    category=category,
                )
            )
            if len([item for item in items if item.source == source]) >= max_items:
                break

    return items


def fetch_hn_items(config: dict[str, Any], seen_ids: set[str]) -> list[NewsItem]:
    hn_config = config.get("hacker_news", {})
    if not hn_config.get("enabled", True):
        return []
    cutoff_ts = int((datetime.now(timezone.utc) - timedelta(hours=int(config.get("lookback_hours", 24)))).timestamp())
    min_points = int(hn_config.get("min_points", 40))
    max_items = int(hn_config.get("max_items", 12))
    queries = hn_config.get("queries", ["AI", "LLM", "OpenAI", "Claude", "agents"])
    keywords = hn_config.get("keywords", queries)
    collected: dict[str, NewsItem] = {}

    for query in queries:
        params = urllib.parse.urlencode(
            {
                "query": query,
                "tags": "story",
                "hitsPerPage": max_items,
                "numericFilters": f"created_at_i>{cutoff_ts},points>{min_points}",
            }
        )
        url = f"https://hn.algolia.com/api/v1/search_by_date?{params}"
        try:
            payload = request_json(url)
        except Exception as exc:
            print(f"WARN: failed to fetch Hacker News query {query}: {exc}", file=sys.stderr)
            continue
        for hit in payload.get("hits", []):
            object_id = str(hit.get("objectID", ""))
            item_id = f"hn:{object_id}"
            if not object_id or item_id in seen_ids:
                continue
            title = strip_markup(hit.get("title") or hit.get("story_title") or "")
            target_url = hit.get("url") or f"https://news.ycombinator.com/item?id={object_id}"
            haystack = " ".join(
                [
                    title,
                    target_url,
                    strip_markup(hit.get("story_text") or ""),
                    strip_markup(hit.get("comment_text") or ""),
                ]
            )
            if not keyword_match(haystack, keywords):
                continue
            comments_url = f"https://news.ycombinator.com/item?id={object_id}"
            points = int(hit.get("points") or 0)
            num_comments = int(hit.get("num_comments") or 0)
            collected[item_id] = NewsItem(
                id=item_id,
                source="Hacker News",
                title=title,
                url=target_url,
                summary=f"HN comments: {num_comments}; discussion: {comments_url}",
                published_at=hit.get("created_at", ""),
                score=points + num_comments * 2,
                category="hot_discussion",
            )

    return sorted(collected.values(), key=lambda item: item.score, reverse=True)[:max_items]


def fetch_github_trending(config: dict[str, Any], seen_ids: set[str]) -> list[NewsItem]:
    gh_config = config.get("github_trending", {})
    if not gh_config.get("enabled", True):
        return []
    languages = gh_config.get("languages", ["python", "typescript"])
    since = gh_config.get("since", "daily")
    max_items = int(gh_config.get("max_items_per_language", 8))
    keywords = gh_config.get("keywords", [])
    items: list[NewsItem] = []

    for language in languages:
        url = f"https://github.com/trending/{urllib.parse.quote(language)}?since={urllib.parse.quote(since)}"
        try:
            page = request_text(url, {"Accept": "text/html"})
        except Exception as exc:
            print(f"WARN: failed to fetch GitHub Trending {language}: {exc}", file=sys.stderr)
            continue
        parser = GitHubTrendingParser()
        parser.feed(page)
        added_for_language = 0
        for article in parser.articles:
            repo_path = next(
                (
                    link.strip("/")
                    for link in article["links"]
                    if re.fullmatch(r"/?[^/\s]+/[^/\s]+", link.strip())
                    and not link.strip("/").startswith(("sponsors/", "topics/", "collections/", "trending/"))
                ),
                "",
            )
            if not repo_path:
                continue
            text = strip_markup(article["text"])
            title = repo_path
            if keywords and not keyword_match(f"{title} {text}", keywords):
                continue
            stars_today_match = re.search(r"([\d,]+)\s+stars?\s+today", text, re.I)
            stars_today = int(stars_today_match.group(1).replace(",", "")) if stars_today_match else 0
            item_id = f"github:{repo_path}:{since}"
            if item_id in seen_ids:
                continue
            description = text.replace(repo_path.replace("/", " / "), "").strip()
            items.append(
                NewsItem(
                    id=item_id,
                    source=f"GitHub Trending {language}",
                    title=title,
                    url=f"https://github.com/{repo_path}",
                    summary=description[:900],
                    published_at="",
                    score=stars_today,
                    category="github_trending",
                )
            )
            added_for_language += 1
            if added_for_language >= max_items:
                break

    return sorted(items, key=lambda item: item.score, reverse=True)


def fetch_all_items(config: dict[str, Any], seen_ids: set[str]) -> list[NewsItem]:
    items = []
    items.extend(fetch_rss_items(config, seen_ids))
    items.extend(fetch_hn_items(config, seen_ids))
    items.extend(fetch_github_trending(config, seen_ids))
    return sorted(items, key=lambda item: item.score, reverse=True)[: int(config.get("max_total_items", 40))]


def summarize(items: list[NewsItem], config: dict[str, Any]) -> str:
    if not items:
        return "过去 24 小时没有抓取到满足条件的 AI 热点内容。"

    api_key = require_env("OPENAI_API_KEY")
    base_url = (os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("OPENAI_MODEL") or "gpt-4.1-mini"
    source_text = "\n\n".join(
        (
            f"- [{item.category}] {item.title}\n"
            f"  source: {item.source}\n"
            f"  score: {item.score}\n"
            f"  url: {item.url}\n"
            f"  published: {item.published_at or 'unknown'}\n"
            f"  summary: {item.summary or 'No summary provided.'}"
        )
        for item in items
    )
    prompt = f"""
You are an AI news editor writing for a Chinese reader.
Create a concise morning digest in Simplified Chinese from the source items below.

Rules:
1. Start with "今日最值得看" and list up to 5 high-signal items.
2. Then group the remaining items into these sections when relevant:
   "模型/产品", "研究/论文", "开发者工具", "GitHub 热门项目", "热门讨论", "商业/行业".
3. Every bullet must include the source name and URL.
4. Do not invent facts. If details are missing, say "原文未说明".
5. End with "行动建议" in no more than 3 short bullets.
6. Keep the whole digest skimmable.

Source items:
{source_text}
""".strip()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Extract high-signal AI news and produce a concise Chinese digest."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
    }
    payload = request_json(
        f"{base_url}/chat/completions",
        {"Authorization": f"Bearer {api_key}"},
        method="POST",
        body=body,
    )
    return payload["choices"][0]["message"]["content"].strip()


def markdown_to_basic_html(markdown: str) -> str:
    lines = []
    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        escaped = html.escape(line)
        if not line:
            lines.append("<br>")
        elif line.startswith("#"):
            text = html.escape(line.lstrip("#").strip())
            lines.append(f"<h3>{text}</h3>")
        elif line.startswith("- "):
            lines.append(f"<p>{escaped}</p>")
        else:
            lines.append(f"<p>{escaped}</p>")
    return "\n".join(lines)


def send_email(subject: str, markdown: str) -> None:
    smtp_host = require_env("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT") or "587")
    smtp_use_ssl = os.getenv("SMTP_USE_SSL", "false").strip().lower() in {"1", "true", "yes", "y"}
    smtp_user = require_env("SMTP_USER")
    smtp_password = require_env("SMTP_PASSWORD")
    email_from = os.getenv("EMAIL_FROM") or smtp_user
    email_to = require_env("EMAIL_TO")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(markdown)
    msg.add_alternative(
        f"""\
<html>
  <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; line-height: 1.65;">
    {markdown_to_basic_html(markdown)}
  </body>
</html>
""",
        subtype="html",
    )

    context = ssl.create_default_context()
    if smtp_use_ssl:
        with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=45, context=context) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=45) as server:
            server.starttls(context=context)
            server.login(smtp_user, smtp_password)
            server.send_message(msg)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="Daily AI hot news email agent")
    parser.add_argument("--dry-run", action="store_true", help="Print digest without sending email or updating state")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    config = load_json(CONFIG_PATH, {})
    state = load_json(STATE_PATH, {"sent_item_ids": []})
    seen_ids = set(state.get("sent_item_ids", []))
    items = fetch_all_items(config, seen_ids)

    if not items and not bool(config.get("send_when_empty", True)):
        print("No qualifying items found; email skipped.")
        return 0

    digest = summarize(items, config)
    tz = ZoneInfo(os.getenv("AGENT_TIMEZONE") or "Asia/Shanghai")
    today = datetime.now(tz).strftime("%Y-%m-%d")
    subject = f"AI 热点资讯早报 - {today}"

    if args.dry_run:
        print(f"Subject: {subject}\n")
        print(digest)
        return 0

    send_email(subject, digest)
    state["sent_item_ids"] = sorted((seen_ids | {item.id for item in items}))[-3000:]
    save_json(STATE_PATH, state)
    print(f"Sent digest email with {len(items)} source items.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
