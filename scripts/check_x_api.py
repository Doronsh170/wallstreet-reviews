import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import requests

API_URL = "https://api.twitterapi.io/twitter/user/last_tweets"
ACCOUNTS_FILE = Path("accounts.txt")
OUTPUT_DIR = Path("output")
MAX_TWEETS_PER_ACCOUNT = 10

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$([A-Z]{1,6})(?![A-Za-z0-9_])")


def load_accounts() -> List[str]:
    if not ACCOUNTS_FILE.exists():
        raise FileNotFoundError("accounts.txt not found")
    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        accounts.append(line.lstrip("@"))
    return accounts


def fetch_last_tweets(api_key: str, username: str) -> Dict[str, Any]:
    headers = {"X-API-Key": api_key}
    params = {"userName": username, "includeReplies": "false"}
    response = requests.get(API_URL, headers=headers, params=params, timeout=30)
    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text}
    if response.status_code != 200:
        raise RuntimeError(
            f"TwitterAPI.io request failed for @{username}: "
            f"HTTP {response.status_code}: {json.dumps(payload, ensure_ascii=False)[:1000]}"
        )
    if payload.get("status") == "error":
        raise RuntimeError(
            f"TwitterAPI.io returned error for @{username}: "
            f"{json.dumps(payload, ensure_ascii=False)[:1000]}"
        )
    return payload


def simplify_tweet(tweet: Dict[str, Any], fallback_username: str) -> Dict[str, Any]:
    text = tweet.get("text") or ""
    author = tweet.get("author") or {}
    username = author.get("userName") or fallback_username
    return {
        "id": tweet.get("id"),
        "url": tweet.get("url"),
        "createdAt": tweet.get("createdAt"),
        "author": username,
        "text": text,
        "cashtags": sorted(set(CASHTAG_RE.findall(text.upper()))),
        "likeCount": tweet.get("likeCount"),
        "retweetCount": tweet.get("retweetCount"),
        "replyCount": tweet.get("replyCount"),
        "viewCount": tweet.get("viewCount"),
    }


def write_outputs(results: Dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"twitterapi_check_{stamp}.json"
    md_path = OUTPUT_DIR / f"twitterapi_check_{stamp}.md"

    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = []
    lines.append("# TwitterAPI.io Check")
    lines.append("")
    lines.append(f"Generated UTC: {results['generated_utc']}")
    lines.append(f"Accounts checked: {len(results['accounts'])}")
    lines.append(f"Total tweets collected: {results['total_tweets']}")
    lines.append("")

    all_tickers = sorted({t for item in results["tweets"] for t in item.get("cashtags", [])})
    lines.append("## Detected cashtags")
    lines.append(", ".join(f"${t}" for t in all_tickers) if all_tickers else "No cashtags detected.")
    lines.append("")

    lines.append("## Tweets")
    for item in results["tweets"]:
        tickers = ", ".join(f"${t}" for t in item.get("cashtags", [])) or "no cashtags"
        lines.append(f"### @{item['author']} | {item.get('createdAt') or 'no date'} | {tickers}")
        lines.append(item.get("text", "").replace("\n", " "))
        if item.get("url"):
            lines.append(f"URL: {item['url']}")
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")


def main() -> int:
    api_key = os.environ.get("TWITTER_API_KEY")
    if not api_key:
        print("ERROR: Missing GitHub Secret named TWITTER_API_KEY")
        return 1

    accounts = load_accounts()
    if not accounts:
        print("ERROR: accounts.txt is empty")
        return 1

    all_tweets: List[Dict[str, Any]] = []
    raw_status: Dict[str, Any] = {}

    for username in accounts:
        print(f"Checking @{username}...")
        payload = fetch_last_tweets(api_key, username)
        tweets = payload.get("tweets") or []
        raw_status[username] = {
            "status": payload.get("status"),
            "message": payload.get("message"),
            "tweet_count_received": len(tweets),
            "has_next_page": payload.get("has_next_page"),
        }
        for tweet in tweets[:MAX_TWEETS_PER_ACCOUNT]:
            all_tweets.append(simplify_tweet(tweet, username))
        print(f"OK @{username}: received {len(tweets)} tweets")

    results = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
        "source_api": "twitterapi.io",
        "endpoint": API_URL,
        "total_tweets": len(all_tweets),
        "status_by_account": raw_status,
        "tweets": all_tweets,
    }
    write_outputs(results)
    print("SUCCESS: TwitterAPI.io connection works.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
