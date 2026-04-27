#!/usr/bin/env python3
"""
GitHub Trending Scouter - Hermes Tool
抓取 GitHub 过去 20 天热门项目，记录到 Notion 并返回列表。
"""
import json
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Dict

# ====== CONFIG ======
NOTION_TOKEN = None  # lazy-loaded from env
GITHUB_TOKEN = None  # lazy-loaded: env var first, then gh auth token
DATABASE_ID = "2f855a34-9949-8020-83b5-cc37c2f54df5"
DATA_SOURCE_ID = "2f855a34-9949-806b-888c-000bf8c77d79"
CATEGORY = "Github"


def _get_env(key: str, fallback: str = None) -> str:
    import os
    return os.environ.get(key, fallback)


def _get_github_token() -> str:
    global GITHUB_TOKEN
    if GITHUB_TOKEN:
        return GITHUB_TOKEN
    GITHUB_TOKEN = _get_env("GITHUB_TOKEN")
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


def _get_notion_token() -> str:
    global NOTION_TOKEN
    if NOTION_TOKEN:
        return NOTION_TOKEN
    NOTION_TOKEN = _get_env("NOTION_TOKEN") or _get_env("NOTION_API_KEY")
    return NOTION_TOKEN


# ------------------------------------------------------------------
# Notion Client
# ------------------------------------------------------------------

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
        """分页获取数据库中所有 Repo URL -> Page ID 映射"""
        url = f"https://api.notion.com/v1/data_sources/{DATA_SOURCE_ID}/query"
        existing_map: Dict[str, str] = {}
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
                    existing_map[repo_url] = page["id"]
            has_more = data.get("has_more", False)
            next_cursor = data.get("next_cursor")
        return existing_map

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


# ------------------------------------------------------------------
# GitHub Fetcher
# ------------------------------------------------------------------

def fetch_github_trending() -> list:
    """获取最近 20 天内创建的、Star 最多的 15 个项目"""
    days_ago = (datetime.now() - timedelta(days=20)).strftime("%Y-%m-%d")
    query = f"created:>{days_ago}"
    encoded_query = urllib.parse.quote(query)
    url = f"https://api.github.com/search/repositories?q={encoded_query}&sort=stars&order=desc&per_page=15"
    headers = {"Accept": "application/vnd.github.v3+json"}
    token = _get_github_token()
    if token:
        headers["Authorization"] = f"token {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode()).get("items", [])


# ------------------------------------------------------------------
# Tool Handler
# ------------------------------------------------------------------

def _handle_github_scouter(args: Dict[str, Any], **kwargs) -> str:
    """抓取 GitHub Trending，写入 Notion，返回项目列表"""
    try:
        notion_token = _get_notion_token()
        if not notion_token:
            return json.dumps({"error": "Missing NOTION_TOKEN / NOTION_API_KEY env var"})

        notion = NotionClient(notion_token)

        # 1. 获取 Notion 中已有的 URL 映射
        existing_repos = notion.get_all_existing_repos()

        # 2. 获取 GitHub Trending
        repos = fetch_github_trending()

        # 3. Upsert
        new_count = 0
        update_count = 0
        results = []

        for repo in repos:
            repo_url = repo["html_url"]
            repo_name = repo["full_name"]
            stars = repo["stargazers_count"]
            desc = repo.get("description") or ""
            lang = repo.get("language") or "N/A"
            link = repo["html_url"]

            result_item = {
                "name": repo_name,
                "stars": stars,
                "description": desc,
                "language": lang,
                "url": link,
                "action": None,
            }

            if repo_url in existing_repos:
                page_id = existing_repos[repo_url]
                notion.update_page(page_id, repo)
                result_item["action"] = "updated"
                update_count += 1
            else:
                notion.create_page(repo)
                result_item["action"] = "new"
                new_count += 1

            results.append(result_item)
            time.sleep(0.3)  # 避免触发 Notion 速率限制

        return json.dumps(
            {
                "repos": results,
                "new": new_count,
                "updated": update_count,
                "total": len(results),
            },
            ensure_ascii=False,
        )

    except Exception as e:
        return json.dumps({"error": str(e)})


# ------------------------------------------------------------------
# Schema & Registration
# ------------------------------------------------------------------

GITHUB_SCOUTER_SCHEMA = {
    "name": "github_scouter",
    "description": "抓取 GitHub 过去 20 天热门项目（按 star 排序），记录到 Notion，返回项目列表。",
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


from tools.registry import registry

registry.register(
    name="github_scouter",
    toolset="github_scouter",
    schema=GITHUB_SCOUTER_SCHEMA,
    handler=_handle_github_scouter,
)
