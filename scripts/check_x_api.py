import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Any

import requests

BASE_URL = "https://api.twitter.com/2"
ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS_FILE = ROOT / "accounts.txt"
OUTPUT_DIR = ROOT / "output"


def fail(message: str, status_code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(status_code)


def get_bearer_token() -> str:
    token = os.getenv("X_BEARER_TOKEN", "").strip()
    if not token:
        fail("Missing X_BEARER_TOKEN. Add it in GitHub Secrets, not in the code.")
    return token


def headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def read_accounts() -> List[str]:
    if not ACCOUNTS_FILE.exists():
        fail("accounts.txt not found.")

    accounts: List[str] = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        item = line.strip().replace("@", "")
        if item and not item.startswith("#"):
            accounts.append(item)

    if not accounts:
        fail("accounts.txt is empty.")
    return accounts


def x_get(url: str, token: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
    response = requests.get(url, headers=headers(token), params=params, timeout=30)
    if response.status_code >= 400:
        print("X API response:", response.text[:1000], file=sys.stderr)
        fail(f"X API request failed: {response.status_code} {response.reason}")
    return response.json()


def get_user(token: str, username: str) -> Dict[str, Any]:
    url = f"{BASE_URL}/users/by/username/{username}"
    params = {"user.fields": "id,name,username,verified,verified_type"}
    data = x_get(url, token, params)
    if "data" not in data:
        fail(f"No user data returned for @{username}: {data}")
    return data["data"]


def get_tweets(token: str, user_id: str) -> List[Dict[str, Any]]:
    url = f"{BASE_URL}/users/{user_id}/tweets"
    params = {
        "max_results": 10,
        "tweet.fields": "created_at,public_metrics,lang,entities,referenced_tweets,context_annotations",
        "exclude": "replies",
    }
    data = x_get(url, token, params)
    return data.get("data", [])


def main() -> None:
    token = get_bearer_token()
    accounts = read_accounts()
    OUTPUT_DIR.mkdir(exist_ok=True)

    results = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "accounts_checked": accounts,
        "sources": [],
    }

    for username in accounts:
        print(f"Checking @{username}...")
        try:
            user = get_user(token, username)
            tweets = get_tweets(token, user["id"])
            results["sources"].append({
                "username": username,
                "user": user,
                "tweet_count": len(tweets),
                "tweets": tweets,
            })
            print(f"OK @{username}: {len(tweets)} tweets")
        except Exception as exc:
            results["sources"].append({
                "username": username,
                "error": str(exc),
                "tweet_count": 0,
                "tweets": [],
            })
            print(f"FAILED @{username}: {exc}", file=sys.stderr)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_file = OUTPUT_DIR / f"x_api_check_{timestamp}.json"
    output_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    total = sum(source.get("tweet_count", 0) for source in results["sources"])
    print(f"Saved: {output_file}")
    print(f"Total tweets fetched: {total}")

    if total == 0:
        fail("No tweets fetched. Check token permissions, plan limits, or account access.")


if __name__ == "__main__":
    main()
