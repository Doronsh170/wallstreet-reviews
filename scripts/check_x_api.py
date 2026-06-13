import os
import re
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

BASE = "https://api.twitterapi.io"
ACCOUNTS_FILE = Path("accounts.txt")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

API_KEY = os.environ.get("TWITTER_API_KEY", "").strip()
if not API_KEY:
    raise SystemExit("Missing GitHub secret: TWITTER_API_KEY")

HEADERS = {"X-API-Key": API_KEY}
TIMEOUT = 30

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$[A-Z]{1,6}(?![A-Za-z0-9_])")


def read_accounts() -> List[str]:
    if not ACCOUNTS_FILE.exists():
        raise SystemExit("accounts.txt not found")

    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        account = line.strip().lstrip("@")
        if account and not account.startswith("#"):
            accounts.append(account)
    return accounts


def get_json(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{BASE}{path}"
    response = requests.get(url, headers=HEADERS, params=params, timeout=TIMEOUT)

    try:
        payload = response.json()
    except Exception:
        payload = {"_raw_text": response.text[:1000]}

    payload["_http_status"] = response.status_code
    payload["_requested_url"] = response.url
    return payload


def compact(obj: Any, max_chars: int = 2200) -> Any:
    text = json.dumps(obj, ensure_ascii=False, default=str)
    if len(text) <= max_chars:
        return obj
    return text[:max_chars] + "... [truncated]"


def extract_user_info(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data") or {}

    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]

    return {
        "status": payload.get("status"),
        "msg": payload.get("msg") or payload.get("message"),
        "http_status": payload.get("_http_status"),
        "id": data.get("id") if isinstance(data, dict) else None,
        "userName": data.get("userName") if isinstance(data, dict) else None,
        "name": data.get("name") if isinstance(data, dict) else None,
        "followers": data.get("followers") if isinstance(data, dict) else None,
        "statusesCount": data.get("statusesCount") if isinstance(data, dict) else None,
        "raw_keys": sorted(list(payload.keys())),
    }


def looks_like_tweet(item: Any) -> bool:
    if not isinstance(item, dict):
        return False

    text = item.get("text") or item.get("fullText") or item.get("content")
    return isinstance(text, str) and bool(text.strip())


def find_tweets(obj: Any) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []

    if looks_like_tweet(obj):
        found.append(obj)

    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_tweets(item))

    elif isinstance(obj, dict):
        for key in ("tweets", "data", "items", "results", "timeline", "list"):
            if key in obj:
                found.extend(find_tweets(obj[key]))

        if not found:
            for value in obj.values():
                found.extend(find_tweets(value))

    dedup = {}
    for tweet in found:
        key = str(
            tweet.get("id")
            or tweet.get("tweetId")
            or tweet.get("url")
            or tweet.get("text")
            or tweet.get("fullText")
            or tweet.get("content")
        )
        dedup[key] = tweet

    return list(dedup.values())


def normalize_tweet(tweet: Dict[str, Any], account: str, endpoint_used: str) -> Dict[str, Any]:
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}

    text = (
        tweet.get("text")
        or tweet.get("fullText")
        or tweet.get("content")
        or ""
    )

    return {
        "account_requested": account,
        "author_userName": author.get("userName") or account,
        "id": tweet.get("id") or tweet.get("tweetId"),
        "url": tweet.get("url"),
        "createdAt": tweet.get("createdAt") or tweet.get("created_at"),
        "text": text,
        "likeCount": tweet.get("likeCount"),
        "retweetCount": tweet.get("retweetCount"),
        "replyCount": tweet.get("replyCount"),
        "quoteCount": tweet.get("quoteCount"),
        "viewCount": tweet.get("viewCount"),
        "endpoint_used": endpoint_used,
        "cashtags": sorted(set(CASHTAG_RE.findall(text or ""))),
    }


def try_tweet_endpoints(account: str, user_id: Optional[str]) -> Dict[str, Any]:
    endpoint_calls = [
        (
            "last_tweets_by_userName",
            "/twitter/user/last_tweets",
            {"userName": account, "cursor": "", "includeReplies": "false"},
        ),
        (
            "advanced_search_from_user",
            "/twitter/tweet/advanced_search",
            {"query": f"from:{account}", "queryType": "Latest"},
        ),
    ]

    if user_id:
        endpoint_calls.insert(
            1,
            (
                "last_tweets_by_userId",
                "/twitter/user/last_tweets",
                {"userId": user_id, "cursor": "", "includeReplies": "false"},
            ),
        )
        endpoint_calls.insert(
            2,
            (
                "tweet_timeline_by_userId",
                "/twitter/user/tweet_timeline",
                {"userId": user_id, "cursor": "", "includeReplies": "false"},
            ),
        )

    attempts = []
    all_tweets: List[Dict[str, Any]] = []

    for label, path, params in endpoint_calls:
        payload = get_json(path, params)
        tweets = find_tweets(payload)

        attempts.append(
            {
                "label": label,
                "path": path,
                "params_used": params,
                "http_status": payload.get("_http_status"),
                "status": payload.get("status"),
                "msg": payload.get("msg") or payload.get("message"),
                "root_keys": sorted([k for k in payload.keys() if not k.startswith("_")]),
                "tweet_count_detected": len(tweets),
                "raw_sample": compact(payload, 1800),
            }
        )

        if tweets:
            all_tweets.extend([normalize_tweet(t, account, label) for t in tweets])
            break

        time.sleep(0.4)

    dedup = {}
    for tweet in all_tweets:
        key = str(tweet.get("id") or tweet.get("url") or tweet.get("text"))
        dedup[key] = tweet

    return {"attempts": attempts, "tweets": list(dedup.values())}


def main() -> None:
    accounts = read_accounts()
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    result = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "accounts": accounts,
        "source_api": "twitterapi.io",
        "endpoints_tested": [
            "/twitter/user/info",
            "/twitter/user/last_tweets",
            "/twitter/user/tweet_timeline",
            "/twitter/tweet/advanced_search",
        ],
        "total_tweets": 0,
        "status_by_account": {},
        "tweets": [],
    }

    for account in accounts:
        print(f"Checking @{account}...")

        info_payload = get_json("/twitter/user/info", {"userName": account})
        user_info = extract_user_info(info_payload)
        user_id = user_info.get("id")

        fetch = try_tweet_endpoints(account, user_id)

        result["status_by_account"][account] = {
            "user_info": user_info,
            "attempts": fetch["attempts"],
            "tweet_count_received": len(fetch["tweets"]),
        }

        result["tweets"].extend(fetch["tweets"])
        time.sleep(0.5)

    dedup = {}
    for tweet in result["tweets"]:
        key = str(tweet.get("id") or tweet.get("url") or tweet.get("text"))
        dedup[key] = tweet

    result["tweets"] = list(dedup.values())
    result["total_tweets"] = len(result["tweets"])

    cashtags = sorted(
        set(tag for tweet in result["tweets"] for tag in tweet.get("cashtags", []))
    )
    result["detected_cashtags"] = cashtags

    json_path = OUTPUT_DIR / f"twitterapi_check_v3_{run_ts}.json"
    md_path = OUTPUT_DIR / f"twitterapi_check_v3_{run_ts}.md"

    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# TwitterAPI.io Check v3",
        "",
        f"Generated UTC: {result['generated_utc']}",
        f"Accounts checked: {len(accounts)}",
        f"Total tweets collected: {result['total_tweets']}",
        "",
        "## Account status",
    ]

    for account, status in result["status_by_account"].items():
        user_info = status.get("user_info", {})
        attempt_summary = ", ".join(
            [
                f"{attempt['label']}={attempt['tweet_count_detected']}"
                for attempt in status.get("attempts", [])
            ]
        )
        lines.append(
            f"- @{account}: userId={user_info.get('id')}, "
            f"tweets={status.get('tweet_count_received')}, "
            f"attempts: {attempt_summary}"
        )

    lines.extend(
        [
            "",
            "## Detected cashtags",
            ", ".join(cashtags) if cashtags else "No cashtags detected.",
            "",
            "## Tweets",
        ]
    )

    for tweet in result["tweets"][:50]:
        lines.append(
            f"### @{tweet.get('author_userName')} | "
            f"{tweet.get('createdAt')} | {tweet.get('endpoint_used')}"
        )
        if tweet.get("url"):
            lines.append(str(tweet.get("url")))
        lines.append(str(tweet.get("text") or "").replace("\n", " "))
        lines.append("")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"Saved {json_path}")
    print(f"Saved {md_path}")
    print(f"Total tweets collected: {result['total_tweets']}")

    if result["total_tweets"] == 0:
        print("No tweets found. Download the JSON artifact and inspect attempts.raw_sample.")


if __name__ == "__main__":
    main()
