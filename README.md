# Wall Street Reviews - TwitterAPI.io test

This repository checks that the TwitterAPI.io key works and fetches recent tweets from the configured X accounts.

## GitHub Secret required

Create one repository secret:

`TWITTER_API_KEY`

Paste your twitterapi.io key as the value.

## Accounts

Edit `accounts.txt` to add or remove X accounts. Use names without `@`.

## Run

GitHub → Actions → Check TwitterAPI.io → Run workflow

The output is uploaded as an artifact named `twitterapi-io-output` and includes:

- raw JSON
- markdown summary
- detected cashtags
