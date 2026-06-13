# Wall Street Reviews - TwitterAPI.io v3

This version checks twitterapi.io using the GitHub secret:

```text
TWITTER_API_KEY
```

It tests several endpoints:

1. `/twitter/user/info`
2. `/twitter/user/last_tweets` by `userName`
3. `/twitter/user/last_tweets` by `userId`
4. `/twitter/user/tweet_timeline` by `userId`
5. `/twitter/tweet/advanced_search` as fallback

The output is saved as an artifact under `output/`.

Do not upload API keys to GitHub files. Use Repository Secrets only.
