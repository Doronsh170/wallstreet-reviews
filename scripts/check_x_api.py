import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

USER_INFO_URL = "https://api.twitterapi.io/twitter/user/info"
LAST_TWEETS_URL = "https://api.twitterapi.io/twitter/user/last_tweets"
ACCOUNTS_FILE = Path("accounts.txt")
OUTPUT_DIR = Path("output")
MAX_TWEETS_PER_ACCOUNT = 10

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$([A-Z]{1,6})(?![A-Za-z0-9_])")


def load_accounts() -> List[str]:
    if not ACCOUNTS_FILE.exists():
        raise FileNotFoundError("accounts.txt not found")
    accounts: List[str] = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        accounts.append(line.lstrip("@"))
    return accounts


def api_get(api_key: str, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
    headers = {"X-API-Key": api_key}
    response = requests.get(url, headers=headers, params=params, timeout=30)
    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text}
    if response.status_code != 200:
        raise RuntimeError(
            f"Request failed: HTTP {response.status_code}: "
            f"{json.dumps(payload, ensure_ascii=False)[:1500]}"
        )
    if payload.get("status") == "error":
        raise RuntimeError(
            f"API returned error: {json.dumps(payload, ensure_ascii=False)[:1500]}"
        )
    return payload


def get_user_info(api_key: str, username: str) -> Dict[str, Any]:
    payload = api_get(api_key, USER_INFO_URL, {"userName": username})
    data = payload.get("data") or {}
    return {
        "status": payload.get("status"),
        "msg": payload.get("msg") or payload.get("message"),
        "id": data.get("id"),
        "userName": data.get("userName") or username,
        "name": data.get("name"),
        "followers": data.get("followers"),
        "statusesCount": data.get("statusesCount"),
        "raw_keys": sorted(list(payload.keys())),
    }


def fetch_last_tweets(api_key: str, username: str, user_id: Optional[str]) -> Dict[str, Any]:
    # userId is recommended by TwitterAPI.io as more stable/faster than userName.
    params: Dict[str, Any] = {"cursor": "", "includeReplies": False}
    if user_id:
        params["userId"] = user_id
    else:
        params["userName"] = username

    payload = api_get(api_key, LAST_TWEETS_URL, params)
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
        "isReply": tweet.get("isReply"),
    }


def compact_raw_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    # Keep enough debug info without dumping every tweet twice.
    return {
        "keys": sorted(list(payload.keys())),
        "status": payload.get("status"),
        "message": payload.get("message") or payload.get("msg"),
        "has_next_page": payload.get("has_next_page"),
        "next_cursor_present": bool(payload.get("next_cursor")),
        "tweet_count": len(payload.get("tweets") or []),
        "first_tweet_keys": sorted(list((payload.get("tweets") or [{}])[0].keys())) if payload.get("tweets") else [],
    }


def write_outputs(results: Dict[str, Any]) -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"twitterapi_check_v2_{stamp}.json"
    md_path = OUTPUT_DIR / f"twitterapi_check_v2_{stamp}.md"

    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    lines: List[str] = []
    lines.append("# TwitterAPI.io Check v2")
    lines.append("")
    lines.append(f"Generated UTC: {results['generated_utc']}")
    lines.append(f"Accounts checked: {len(results['accounts'])}")
    lines.append(f"Total tweets collected: {results['total_tweets']}")
    lines.append("")

    lines.append("## Account status")
    for acc, st in results["status_by_account"].items():
        info = st.get("user_info", {})
        lines.append(f"- @{acc}: userId={info.get('id')}, tweets={st.get('tweet_count_received')}, message={st.get('message')}")
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
        user_info = get_user_info(api_key, username)
        print(f"Resolved @{username}: id={user_info.get('id')}, followers={user_info.get('followers')}")

        payload = fetch_last_tweets(api_key, username, user_info.get("id"))
        tweets = payload.get("tweets") or []
        raw_status[username] = {
            "user_info": user_info,
            "status": payload.get("status"),
            "message": payload.get("message") or payload.get("msg"),
            "tweet_count_received": len(tweets),
            "has_next_page": payload.get("has_next_page"),
            "next_cursor_present": bool(payload.get("next_cursor")),
            "raw_debug": compact_raw_payload(payload),
        }
        for tweet in tweets[:MAX_TWEETS_PER_ACCOUNT]:
            all_tweets.append(simplify_tweet(tweet, username))
        print(f"OK @{username}: received {len(tweets)} tweets")

    results = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
        "source_api": "twitterapi.io",
        "endpoints": {"user_info": USER_INFO_URL, "last_tweets": LAST_TWEETS_URL},
        "total_tweets": len(all_tweets),
        "status_by_account": raw_status,
        "tweets": all_tweets,
    }
    write_outputs(results)

    if len(all_tweets) == 0:
        print("WARNING: Connection worked, but zero tweets were returned. Check output JSON debug fields.")
        # Do not fail the GitHub Action. We want the artifact for debugging.
        return 0

    print("SUCCESS: TwitterAPI.io connection works and tweets were collected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
