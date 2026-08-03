#!/usr/bin/env python3
"""GitHub Trending Scouter — standalone cron script.
Fetches top 15 repos created in last 7 days, syncs to Notion,
outputs only NEW repos to stdout. Silent when zero new items.
Designed for hermes-agent no_agent cron mode.
Requires: NOTION_TOKEN (or NOTION_API_KEY), GITHUB_TOKEN (or gh auth token).
"""

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict

# ── Config ──
NOTION_TOKEN = None
GITHUB_TOKEN = None
DATABASE_ID = "2f855a34-9949-8020-83b5-cc37c2f54df5"
DATA_SOURCE_ID = "2f855a34-9949-806b-888c-000bf8c77d79"
CATEGORY = "Github"
DAYS_WINDOW = 7
TOP_N = 15

# ── Secrets: pull from Bitwarden if env vars are empty ──────────
_NEEDED_SECRETS = [
    "NOTION_TOKEN",
    "NOTION_API_KEY",
    "GITHUB_TOKEN",
]
BWS_BIN = os.path.expanduser("~/.hermes/bin/bws")


def _fetch_secrets_from_bitwarden() -> None:
    """If any needed API key is missing from env, fetch all from Bitwarden."""
    missing = [k for k in _NEEDED_SECRETS if not os.environ.get(k)]
    if not missing:
        return  # all keys already present

    token = os.environ.get("BWS_ACCESS_TOKEN", "")
    if not token:
        print("  [WARN] BWS_ACCESS_TOKEN not set, skipping Bitwarden fetch", file=sys.stderr)
        return

    try:
        result = subprocess.run(
            [BWS_BIN, "secret", "list"],
            capture_output=True, text=True,
            env={**os.environ, "BWS_ACCESS_TOKEN": token},
            timeout=15,
        )
        if result.returncode != 0:
            print(f"  [WARN] bws failed (rc={result.returncode}): {result.stderr[:200]}", file=sys.stderr)
            return

        secrets = json.loads(result.stdout)
        injected = 0
        for s in secrets:
            key = s.get("key", "")
            if key in missing:
                os.environ[key] = s["value"]
                injected += 1

        if injected:
            print(f"  [bws] Injected {injected}/{len(missing)} missing secrets from Bitwarden", file=sys.stderr)
    except Exception as e:
        print(f"  [WARN] bws error: {e}", file=sys.stderr)


def _get_notion_token() -> str:
    global NOTION_TOKEN
    if NOTION_TOKEN:
        return NOTION_TOKEN
    NOTION_TOKEN = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
    return NOTION_TOKEN


def _get_github_token() -> str:
    global GITHUB_TOKEN
    if GITHUB_TOKEN:
        return GITHUB_TOKEN
    GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
    if GITHUB_TOKEN:
        return GITHUB_TOKEN
    try:
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            token = result.stdout.strip()
            if token:
                GITHUB_TOKEN = token
                return token
    except Exception:
        pass
    return None


# ── Notion Client ──

class NotionClient:
    def __init__(self, token: str):
        self.token = token
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2025-09-03",
            "Content-Type": "application/json",
        }

    def _request(self, url: str, method: str = "POST", data: Dict = None) -> Dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode() if data else None,
            headers=self.headers,
            method=method,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())

    def get_all_existing_repos(self) -> Dict[str, str]:
        """Return {repo_url: page_id} for all repos in the Notion database."""
        url = f"https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}/query"
        existing: Dict[str, str] = {}
        has_more = True
        next_cursor = None
        while has_more:
            payload: Dict[str, Any] = {"page_size": 100}
            if next_cursor:
                payload["start_cursor"] = next_cursor
            data = self._request(url, data=payload)
            for page in data.get("results", []):
                repo_url = page["properties"].get("URL", {}).get("url")
                if repo_url:
                    existing[repo_url] = page["id"]
            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")
        return existing

    def create_page(self, repo: Dict) -> Dict:
        name = repo["full_name"]
        stars = repo["stargazers_count"]
        desc = repo.get("description") or "No description"
        link = repo["html_url"]
        lang = repo.get("language") or "N/A"
        payload = {
            "parent": {"database_id": DATABASE_ID},
            "properties": {
                "Goal name": {"title": [{"text": {"content": f"{name} ⭐ {stars}"}}]},
                "Category": {"select": {"name": CATEGORY}},
                "Insert_date": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}},
                "URL": {"url": link},
            },
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"text": {"content": f"📌 {desc}"}}]},
                },
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"text": {"content": f"⭐ {stars} | 💻 {lang}"}}]
                    },
                },
            ],
        }
        return self._request("https://api.notion.com/v1/pages", method="POST", data=payload)

    def update_page(self, page_id: str, repo: Dict) -> Dict:
        name = repo["full_name"]
        stars = repo["stargazers_count"]
        payload = {
            "properties": {
                "Goal name": {"title": [{"text": {"content": f"{name} ⭐ {stars}"}}]},
                "Insert_date": {"date": {"start": datetime.now().strftime("%Y-%m-%d")}},
            }
        }
        return self._request(
            f"https://api.notion.com/v1/pages/{page_id}", method="PATCH", data=payload
        )


# ── GitHub Fetcher ──

def fetch_github_trending() -> list:
    """Fetch top repos created in the last N days, sorted by stars."""
    days_ago = (datetime.now() - timedelta(days=DAYS_WINDOW)).strftime("%Y-%m-%d")
    query = f"created:>{days_ago}"
    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://api.github.com/search/repositories"
        f"?q={encoded_query}&sort=stars&order=desc&per_page={TOP_N}"
    )
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = _get_github_token()
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode()).get("items", [])


# ── Main ──

def main():
    _fetch_secrets_from_bitwarden()
    notion_token = _get_notion_token()
    if not notion_token:
        print("[FATAL] NOTION_TOKEN or NOTION_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    notion = NotionClient(notion_token)

    # 1. Get existing repos from Notion
    existing_repos = notion.get_all_existing_repos()

    # 2. Fetch trending from GitHub
    repos = fetch_github_trending()

    # 3. Upsert to Notion, track new items
    new_items = []
    for repo in repos:
        repo_url = repo["html_url"]
        if repo_url in existing_repos:
            page_id = existing_repos[repo_url]
            notion.update_page(page_id, repo)
        else:
            notion.create_page(repo)
            new_items.append(repo)
        time.sleep(0.3)  # Notion rate limit

    # 4. Output — only new items, silent otherwise
    if not new_items:
        return  # silent — nothing delivered

    print(f"🔥 **GitHub 7日热门 — {len(new_items)} 个新项目**\n")

    for i, repo in enumerate(new_items, 1):
        name = repo["full_name"]
        stars = repo["stargazers_count"]
        link = repo["html_url"]
        desc = repo.get("description") or "—"
        lang = repo.get("language") or "N/A"

        print(f"**{i}. [{name}]({link})**  ⭐ {stars}  |  💻 {lang}")
        print(f"   {desc}")
        print()


if __name__ == "__main__":
    main()
