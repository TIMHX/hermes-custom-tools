#!/usr/bin/env python3
"""
Daily Briefing with model fallback: MiniMax → DeepSeek → Gemini.
Uses SearXNG (primary) + Tavily (fallback) for news search, writes to Notion.
"""

import os, sys, json, time, re, subprocess
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

# ─── Secrets: pull from Bitwarden if env vars are empty ──────────
_NEEDED_SECRETS = [
    "TAVILY_API_KEY",
    "MINIMAX_CN_API_KEY",
    "MINIMAX_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "NOTION_API_KEY",
]

def _fetch_secrets_from_bitwarden() -> None:
    """If any needed API key is missing from env, fetch all from Bitwarden."""
    missing = [k for k in _NEEDED_SECRETS if not os.environ.get(k)]
    if not missing:
        return  # all keys already present

    token = os.environ.get("BWS_ACCESS_TOKEN", "")
    if not token:
        print("  [WARN] BWS_ACCESS_TOKEN not set, skipping Bitwarden fetch", file=sys.stderr)
        return

    bws_bin = os.path.expanduser("~/.hermes/bin/bws")
    try:
        result = subprocess.run(
            [bws_bin, "secret", "list"],
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
        else:
            print(f"  [WARN] bws returned no matching secrets for: {missing}", file=sys.stderr)

    except FileNotFoundError:
        print(f"  [WARN] bws binary not found at {bws_bin}", file=sys.stderr)
    except Exception as e:
        print(f"  [WARN] Bitwarden fetch failed: {e}", file=sys.stderr)

_fetch_secrets_from_bitwarden()

# ─── API KEYS from env ───────────────────────────────────────────
TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")
MINIMAX_KEY = os.environ.get("MINIMAX_CN_API_KEY") or os.environ.get("MINIMAX_API_KEY", "")
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
GEMINI_KEY = os.environ.get("GOOGLE_API_KEY", "")
NOTION_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DB = "34655a349949804fa72bdacdaf1e8080"
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://127.0.0.1:8888")

# ─── LLM Fallback Chain ──────────────────────────────────────────
MODEL_USED = None  # set by generate_briefing

def minimax_prepare(system_msg, user_msg):
    url = "https://api.minimax.chat/v1/text/chatcompletion_v2"
    headers = {
        "Authorization": f"Bearer {MINIMAX_KEY}",
        "Content-Type": "application/json",
    }
    # MiniMax uses OpenAI-compatible endpoint at minimax.chat
    messages = []
    if system_msg:
        messages.append({"role": "system", "content": system_msg})
    messages.append({"role": "user", "content": user_msg})
    body = json.dumps({
        "model": "MiniMax-M2.7",
        "max_tokens": 8192,
        "temperature": 0.3,
        "messages": messages,
    }).encode()
    return url, headers, body

def deepseek_prepare(system_msg, user_msg):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json",
    }
    messages = []
    if system_msg:
        messages.append({"role": "system", "content": system_msg})
    messages.append({"role": "user", "content": user_msg})
    body = json.dumps({
        "model": "deepseek-chat",
        "max_tokens": 8192,
        "temperature": 0.3,
        "messages": messages,
    }).encode()
    return url, headers, body

def gemini_prepare(system_msg, user_msg):
    # Combine system+user into single prompt for Gemini
    full_text = user_msg
    if system_msg:
        full_text = system_msg + "\n\n" + user_msg
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-pro:generateContent?key={GEMINI_KEY}"
    headers = {"Content-Type": "application/json"}
    body = json.dumps({
        "contents": [{"parts": [{"text": full_text}]}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
    }).encode()
    return url, headers, body

def gemini_extract_text(resp_json):
    """Extract text from Gemini response."""
    try:
        return resp_json["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        if "error" in resp_json:
            raise RuntimeError(f"Gemini API error: {resp_json['error']}")
        raise

def minimax_extract_text(resp_json):
    """Extract text from MiniMax OpenAI-compatible response."""
    try:
        # MiniMax chatcompletion_v2 returns choices[0].message.content
        return resp_json["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        try:
            # Fallback: anthropic-style content array
            content = resp_json.get("content", [])
            if isinstance(content, list):
                return "".join(block.get("text", "") for block in content)
            return str(content)
        except Exception:
            if "error" in resp_json:
                raise RuntimeError(f"MiniMax error: {resp_json['error']}")
            if "base_resp" in resp_json and resp_json["base_resp"].get("status_code") != 0:
                raise RuntimeError(f"MiniMax error: {resp_json['base_resp'].get('status_msg', 'unknown')}")
            raise

LLM_CHAIN = [
    {
        "name": "minimax",
        "prepare": minimax_prepare,
        "extract": minimax_extract_text,
        "filter_error_patterns": ["output new_sensitive", "1027"],
    },
    {
        "name": "deepseek",
        "prepare": deepseek_prepare,
        "extract": lambda r: r["choices"][0]["message"]["content"],
        "filter_error_patterns": [],
    },
    {
        "name": "gemini",
        "prepare": gemini_prepare,
        "extract": gemini_extract_text,
        "filter_error_patterns": [],
    },
]

# ─── Search News ─────────────────────────────────────────────────
def search_searxng(queries: list[str], max_results: int = 8) -> list[dict]:
    """Search news using SearXNG (free, self-hosted). Primary search backend."""
    all_results = []
    seen_urls = set()

    for query in queries[:8]:
        try:
            encoded_query = urllib.parse.quote(query)
            api_url = (
                f"{SEARXNG_URL}/search"
                f"?q={encoded_query}&format=json&categories=news"
                f"&time_range=week&limit={max_results}&engines=google,brave,duckduckgo,bing,startpage"
            )
            req = urllib.request.Request(api_url, headers={"User-Agent": "Hermes-DailyBriefing/1.0"})
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            for r in data.get("results", []):
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append({
                        "title": r.get("title", ""),
                        "url": url,
                        "snippet": r.get("content", ""),
                        "published_date": r.get("published_date", ""),
                    })
            time.sleep(0.3)  # be gentle to our own instance
        except Exception as e:
            print(f"  [WARN] SearXNG '{query[:50]}...' failed: {e}", file=sys.stderr)

    return _filter_old_results(all_results)[:max_results]


def search_news(queries: list[str], max_results: int = 8) -> list[dict]:
    """Search news: SearXNG primary → Tavily fallback if insufficient results."""
    # 1. Try SearXNG first (free, unlimited)
    print(f"  [SearXNG] Searching {len(queries)} queries...", file=sys.stderr)
    results = search_searxng(queries, max_results=max_results)

    if len(results) < 3:
        print(f"  [WARN] SearXNG only returned {len(results)} results, falling back to Tavily...", file=sys.stderr)
        # 2. Fallback to Tavily for critical queries
        tavily_results = _search_tavily(queries, max_results=max_results, seen_urls=set(r["url"] for r in results))
        results = results + tavily_results

    return results[:max_results]


def _search_tavily(queries: list[str], max_results: int = 8, seen_urls: set = None) -> list[dict]:
    all_results = []
    if seen_urls is None:
        seen_urls = set()

    for query in queries[:8]:
        try:
            body = json.dumps({
                "query": query,
                "search_depth": "advanced",
                "topic": "news",
                "include_answer": False,
                "max_results": 8,
                "days": 7,
            }).encode()
            req = urllib.request.Request(
                "https://api.tavily.com/search",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {TAVILY_KEY}",
                },
            )
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            for r in data.get("results", []):
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append({
                        "title": r.get("title", ""),
                        "url": url,
                        "snippet": r.get("content", ""),
                        "published_date": r.get("published_date", ""),
                    })
            time.sleep(0.6)
        except Exception as e:
            print(f"  [WARN] Search '{query[:50]}...' failed: {e}", file=sys.stderr)

    # Filter out old results
    filtered = _filter_old_results(all_results)

    # If too few fresh results, retry with backup queries
    if len(filtered) < 3:
        print(f"  Only {len(filtered)} fresh results, trying backup...", file=sys.stderr)
        filtered = _retry_with_backup_queries(queries, filtered, seen_urls)

    return filtered[:max_results]


def _retry_with_backup_queries(original_queries, existing_results, seen_urls):
    """Retry search with broader but still recent queries."""
    # Use very recent date-specific backup queries
    today = datetime.now(timezone.utc)
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)

    dates = [
        today.strftime("%B %-d %Y"),
        yesterday.strftime("%B %-d %Y"),
        day_before.strftime("%B %-d %Y"),
        "this week",
        "latest news",
    ]

    # Derive backup from original query keywords
    backup_results = []
    for orig_query in original_queries:
        # Extract key terms (first 3-4 words)
        terms = " ".join(orig_query.split()[:4])
        for date_str in dates[:2]:  # just use 2 date variants
            backup_query = f"{terms} {date_str} news"
            try:
                body = json.dumps({
                    "query": backup_query,
                    "search_depth": "advanced",
                    "topic": "news",
                    "include_answer": False,
                    "max_results": 5,
                    "days": 7,
                }).encode()
                req = urllib.request.Request(
                    "https://api.tavily.com/search",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {TAVILY_KEY}",
                    },
                )
                resp = urllib.request.urlopen(req, timeout=15)
                data = json.loads(resp.read())
                for r in data.get("results", []):
                    url = r.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        backup_results.append({
                            "title": r.get("title", ""),
                            "url": url,
                            "snippet": r.get("content", ""),
                            "published_date": r.get("published_date", ""),
                        })
                time.sleep(0.3)
            except Exception as e:
                print(f"  [WARN] Backup search failed: {e}", file=sys.stderr)

        if len(backup_results) >= 3:
            break

    all_results = existing_results + _filter_old_results(backup_results)
    return all_results


def _filter_old_results(results: list[dict]) -> list[dict]:
    """Discard results without recent date evidence. Keeps only results with:
    1. published_date within 7 days, OR
    2. Explicit date in title/snippet within 7 days
    Results with NO date evidence are discarded to prevent stale content.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    now = datetime.now(timezone.utc)
    filtered = []
    
    for r in results:
        has_recent_date = False
        
        # Check published_date field
        pub = r.get("published_date", "")
        if pub:
            try:
                pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                if pub_dt >= cutoff:
                    has_recent_date = True
                else:
                    continue  # explicitly old, discard
            except (ValueError, TypeError):
                pass
        
        # Check for month+day patterns in title/snippet
        if not has_recent_date:
            title = r.get("title", "")
            snippet = r.get("snippet", "")
            combined = f"{title} {snippet}"
            
            date_patterns = [
                r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s*(20\d{2})',
                r'(20\d{2})[-/](\d{1,2})[-/](\d{1,2})',
            ]
            
            for pattern in date_patterns:
                for match in re.finditer(pattern, combined, re.IGNORECASE):
                    try:
                        if '/' in match.group(0) or '-' in match.group(0):
                            year = int(match.group(1))
                            month = int(match.group(2))
                            day = int(match.group(3))
                        else:
                            month_name = match.group(1)
                            day = int(match.group(2))
                            year = int(match.group(3))
                            month = {
                                'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,
                                'july':7,'august':8,'september':9,'october':10,'november':11,'december':12
                            }[month_name.lower()]
                        
                        story_date = datetime(year, month, day, tzinfo=timezone.utc)
                        if story_date >= cutoff:
                            has_recent_date = True
                            break
                    except (ValueError, KeyError):
                        pass
                if has_recent_date:
                    break
        
        # Also check for "today", "yesterday", "this week" keywords (strong freshness signal)
        if not has_recent_date:
            combined_lower = f"{r.get('title','')} {r.get('snippet','')}".lower()
            freshness_signals = ["today", "yesterday", "this week", "breaking", "just in", "latest"]
            for signal in freshness_signals:
                if signal in combined_lower:
                    has_recent_date = True
                    break
        
        # Check URL for old year patterns (absolute disqualifier)
        url = r.get("url", "")
        year_match = re.search(r'/(20\d{2})/', url)
        if year_match:
            year = int(year_match.group(1))
            if year < now.year - 1:
                continue  # previous year, definitely stale
        
        if has_recent_date:
            filtered.append(r)
    
    return filtered


def search_all_domains() -> dict:
    """Search 3 domains, return {domain_name: [results]}."""
    today = datetime.now(timezone.utc)
    month_abbr = today.strftime("%b")  # "May"
    day = today.strftime("%-d")        # "20"
    date_compact = today.strftime("%Y-%m-%d")
    yesterday = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    
    domains = {
        "macro": [
            f"global trade tariffs news {month_abbr} {day} 2026",
            f"US economy federal reserve news {date_compact}",
            f"immigration policy news {month_abbr} 2026",
            f"geopolitics international relations news today",
            f"financial markets stock market news {date_compact}",
            f"stock market biggest gainers movers today",
            f"Wall Street top stories market moving news this week",
            f"stock sector rotation which sectors outperforming {month_abbr} 2026",
            f"US stocks rally surge what is driving markets {date_compact}",
        ],
        "tech": [
            f"AI artificial intelligence news {date_compact}",
            f"technology semiconductor chip news {month_abbr} 2026",
            f"quantum computing breakthrough news {month_abbr} 2026",
            f"cybersecurity data breach vulnerability news {month_abbr} 2026",
            f"robotics autonomous drone news {month_abbr} 2026",
            f"consumer electronics tech product launch news {month_abbr} 2026",
        ],
        "energy_climate_health": [
            f"renewable energy news {month_abbr} {day} 2026",
            f"climate change news {date_compact}",
            f"biotechnology medical research news {date_compact}",
            f"public health epidemiology news {month_abbr} 2026",
            f"oceanography marine biology discovery news today",
        ],
        "science": [
            f"archaeology discovery paleontology news {month_abbr} 2026",
            f"astronomy space exploration news today",
            f"scientific breakthrough research news {month_abbr} 2026",
            f"physics particle discovery news {month_abbr} 2026",
            f"neuroscience brain research news {month_abbr} 2026",
        ],
        "finance_cn": [
            f"美股 涨幅 板块 今日 热点",
            f"美股 异动 行情 财经新闻",
            f"华尔街 今日 头条 市场",
        ],
    }

    all_domain_results = {}
    domain_labels = {
        "macro": "一、宏观经济与政治",
        "tech": "二、科技",
        "energy_climate_health": "三、能源 / 气候与环境 / 健康",
        "science": "四、科学探索",
        "finance_cn": "五、财经热点",
    }

    for key, queries in domains.items():
        results = search_news(queries, max_results=8)
        all_domain_results[key] = {"label": domain_labels[key], "stories": results}
        print(f"  [{key}] Found {len(results)} stories", file=sys.stderr)
        time.sleep(1)

    return all_domain_results


# ─── LLM Generation with Fallback ────────────────────────────────
def generate_briefing(news_data: dict, today_str: str) -> str:
    """Generate Chinese briefing, trying each LLM in chain."""
    # Build user message with news data
    news_text = ""
    for key in ["macro", "tech", "energy_climate_health", "science", "finance_cn"]:
        domain = news_data.get(key, {})
        news_text += f"\n## {domain.get('label', key)}\n"
        for i, story in enumerate(domain.get("stories", []), 1):
            news_text += f"\n{i}. {story['title']}\n   {story['snippet'][:300]}\n   URL: {story['url']}\n"

    system_msg = f"""You are a Chinese news briefing editor. Today is {today_str}.

Your task: select the most important stories from the provided news data and write a daily briefing in Chinese.

FORMAT RULES (strict):
- Output ONLY the briefing, no extra commentary, no code blocks, no markdown wrappers
- Section headers: "## 一、宏观经济与政治" style (with ## prefix, Chinese number)
- Each news story: "• English Title" on its own line, then Chinese 2-3 sentence summary on next line, then "🔗 URL" on next line, then an empty line
- Date subtitle: "{today_str} | 每日简报" (not 2026年X月X日 template)
- Title: "Daily Briefing -- {today_str}"
- Choose 5-8 best stories per section, skip irrelevant/outdated ones
- Be analytical in summaries — explain significance, not just restate facts
- Prioritize stories from reputable sources (Reuters, AP, BBC, NPR, etc.), skip YouTube/Reddit/Wikipedia
- If a URL seems low quality, skip that story entirely
- CRITICAL: Only use stories from the past 7 days. If a story's URL or snippet references dates older than one week ago, DO NOT include it. Quality over quantity — fewer fresh stories are better than many stale ones."""

    user_msg = f"""Create a daily briefing for {today_str}.

Use EXACTLY this structure (replace placeholders with real content):

```
Daily Briefing -- {today_str}
{today_str} | 每日简报

## 一、宏观经济与政治

• Exact English Title Here
  中文2-3句摘要，分析事件意义和影响。
  🔗 https://actual.source.url

• Next Story Title
  中文摘要...
  🔗 https://...

## 二、科技

• ...
  ...
  🔗 ...

## 三、能源 / 气候与环境 / 健康

• ...
  ...
  🔗 ...

## 四、科学探索

• ...
  ...
  🔗 ...

## 五、财经热点

• ...
  ...
  🔗 ...
```

Here is the news data to work with:
{news_text}
"""

    last_error = None
    global MODEL_USED
    for llm in LLM_CHAIN:
        name = llm["name"]
        print(f"  Trying {name}...", file=sys.stderr)
        try:
            url, headers, body = llm["prepare"](system_msg, user_msg)
            req = urllib.request.Request(url, data=body, headers=headers)
            resp = urllib.request.urlopen(req, timeout=120)
            resp_text = resp.read().decode()
            resp_json = json.loads(resp_text)

            # Check for content filter in response text
            resp_str = resp_text.lower()
            for pattern in llm["filter_error_patterns"]:
                if pattern.lower() in resp_str:
                    raise RuntimeError(f"Content filter: {pattern}")

            text = llm["extract"](resp_json)
            if not text or len(text.strip()) < 50:
                raise RuntimeError(f"Empty or too-short response ({len(text)} chars)")

            MODEL_USED = name
            print(f"  ✓ {name} succeeded ({len(text)} chars)", file=sys.stderr)
            return text

        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode()
            except Exception:
                pass
            last_error = f"{name}: HTTP {e.code} — {err_body[:200]}"
            print(f"  ✗ {last_error}", file=sys.stderr)
        except Exception as e:
            last_error = f"{name}: {e}"
            print(f"  ✗ {last_error}", file=sys.stderr)

    raise RuntimeError(f"All LLMs failed. Last error: {last_error}")


# ─── Notion Write ────────────────────────────────────────────────
def write_to_notion(briefing: str, today_iso: str) -> str:
    """Create Notion page and append briefing blocks. Returns page ID."""
    # Create page
    page_data = json.dumps({
        "parent": {"database_id": NOTION_DB},
        "properties": {
            "Name": {"title": [{"text": {"content": f"Daily Briefing -- {today_iso}"}}]},
            "Date": {"date": {"start": today_iso}},
        },
    }).encode()

    req = urllib.request.Request(
        "https://api.notion.com/v1/pages",
        data=page_data,
        headers={
            "Authorization": f"Bearer {NOTION_KEY}",
            "Notion-Version": "2025-09-03",
            "Content-Type": "application/json",
        },
    )
    resp = urllib.request.urlopen(req, timeout=15)
    page_id = json.loads(resp.read())["id"]

    # Parse briefing into blocks
    blocks = parse_briefing_to_blocks(briefing)

    # Append blocks (max 100 per call)
    chunks = [blocks[i:i+100] for i in range(0, len(blocks), 100)]
    for chunk in chunks:
        chunk_data = json.dumps({"children": chunk}, ensure_ascii=False).encode()
        req = urllib.request.Request(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            data=chunk_data,
            headers={
                "Authorization": f"Bearer {NOTION_KEY}",
                "Notion-Version": "2025-09-03",
                "Content-Type": "application/json",
            },
            method="PATCH",
        )
        urllib.request.urlopen(req, timeout=15)

    print(f"  ✓ Notion page {page_id} ({len(blocks)} blocks)", file=sys.stderr)
    return page_id


def parse_briefing_to_blocks(briefing: str) -> list[dict]:
    """Parse briefing markdown into Notion blocks."""
    blocks = []
    lines = briefing.strip().split("\n")

    in_code_block = False
    for line in lines:
        # Skip code block markers
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        stripped = line.strip()

        # Skip empty section separators (---)
        if stripped == "---":
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue

        # Section headers (## 一、...)
        if stripped.startswith("## "):
            text = stripped[3:]
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]},
            })
            continue

        # Story title (• ...)
        if stripped.startswith("• "):
            text = stripped[2:]
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"• {text}"}}]},
            })
            continue

        # URL line (🔗 ...)
        if stripped.startswith("🔗 "):
            url = stripped[2:]
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": f"🔗 {url}"}}]},
            })
            continue

        # Regular text (summary line)
        if stripped:
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"type": "text", "text": {"content": stripped}}]},
            })
        else:
            # Empty line = spacer
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": []},
            })

    return blocks


# ─── Main ────────────────────────────────────────────────────────
def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_cn = datetime.now(timezone.utc).strftime("%Y年%m月%d日")

    print(f"=== Daily Briefing {today} ===", file=sys.stderr)

    # 1. Search news
    print("[1/3] Searching news...", file=sys.stderr)
    news = search_all_domains()

    total_stories = sum(len(d["stories"]) for d in news.values())
    if total_stories == 0:
        print("[SILENT]")
        sys.exit(0)
    print(f"  Total: {total_stories} stories", file=sys.stderr)

    # 2. Generate briefing with fallback
    print("[2/3] Generating briefing...", file=sys.stderr)
    briefing = generate_briefing(news, today)
    # Replace date template if model used it
    briefing = briefing.replace("YYYY-MM-DD", today).replace("2026年X月X日", today_cn)

    # 3. Write to Notion
    print("[3/3] Writing to Notion...", file=sys.stderr)
    try:
        page_id = write_to_notion(briefing, today)
    except Exception as e:
        print(f"  ✗ Notion write failed: {e}", file=sys.stderr)
        page_id = "FAILED"

    # 4. Output for delivery (stdout)
    model_tag = MODEL_USED or "unknown"
    print(briefing)
    if page_id != "FAILED":
        print(f"\n📋 Notion: {page_id}")
    print(f"\n🤖 Model used: {model_tag}")

    print(f"\n✓ Done. Model fallback used, Notion {'ok' if page_id != 'FAILED' else 'FAILED'}.", file=sys.stderr)


if __name__ == "__main__":
    main()
