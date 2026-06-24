#!/usr/bin/env python3
"""
Hacker News top stories via Algolia API.
Returns stories with score ≥ 50 from the last 24 hours, sorted by points descending.

Usage as module:
    from hn_top import fetch_hackernews
    stories = fetch_hackernews(min_score=50, max_items=30)

Usage standalone:
    python hn_top.py              # prints JSON to stdout
    python hn_top.py --min 100    # higher score threshold
"""

import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

ALGOLIA_URL = "https://hn.algolia.com/api/v1/search"


def fetch_hackernews(min_score: int = 50, max_items: int = 30) -> list[dict]:
    """
    Fetch Hacker News front page stories from Algolia.

    Returns list of dicts with keys:
        title, url, score, comments_count, author, hn_url, posted_at
    """
    now = int(time.time())
    cutoff = now - 86400  # 24 hours ago

    # Algolia numericFilters only supports created_at_i (not points).
    # Filter by time via API, then filter by score in Python.
    params = urllib.parse.urlencode({
        "tags": "front_page",
        "hitsPerPage": max_items * 2,  # fetch extra, score-filter later
        "numericFilters": f"created_at_i>{cutoff}",
    })
    url = f"{ALGOLIA_URL}?{params}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Hermes-HN-Top/1.0"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"  [WARN] HN Algolia request failed: {e}", file=sys.stderr)
        return []
    except json.JSONDecodeError as e:
        print(f"  [WARN] HN Algolia JSON parse failed: {e}", file=sys.stderr)
        return []

    hits = data.get("hits", [])
    stories = []

    for h in hits:
        points = h.get("points", 0)
        if points < min_score:
            continue  # score filter in Python (Algolia doesn't support it)

        title = h.get("title", "")
        story_url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        hn_url = f"https://news.ycombinator.com/item?id={h['objectID']}"
        created = datetime.fromtimestamp(h.get("created_at_i", 0), tz=timezone.utc)

        stories.append({
            "title": title,
            "url": story_url,
            "score": points,
            "comments_count": h.get("num_comments", 0),
            "author": h.get("author", ""),
            "hn_url": hn_url,
            "posted_at": created.strftime("%Y-%m-%d %H:%M UTC"),
        })

    # Sort by score descending, then truncate to max_items
    stories.sort(key=lambda s: s["score"], reverse=True)
    stories = stories[:max_items]

    print(f"  [HN] Fetched {len(stories)} stories (score ≥ {min_score})", file=sys.stderr)
    return stories


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch Hacker News top stories")
    parser.add_argument("--min", type=int, default=50, help="Minimum score (default: 50)")
    parser.add_argument("--max", type=int, default=30, help="Max items (default: 30)")
    args = parser.parse_args()

    stories = fetch_hackernews(min_score=args.min, max_items=args.max)
    print(json.dumps(stories, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
