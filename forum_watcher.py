"""
forum_watcher.py — companion to bounty-watcher.
Polls a list of subreddits for posts matching error keywords and pings
Telegram. NOTIFY-ONLY: no auto-reply, no auto-DM, no scraping of
Discord. A human reads the alert and posts the reply themselves.
"""

import os
import time
import requests

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Verify these subreddits still exist / add more before the first run —
# not hardcoded from a live check.
SUBREDDITS = ["Supabase", "nocode", "automation"]

KEYWORDS = [
    "row-level security",
    "RLS policy",
    "Make.com scenario",
    "Bad Request webhook",
    "array expected",
    "BundleValidationError",
]

LOOKBACK_SECONDS = 30 * 60  # wider than the 20-min cron interval, avoids missed posts
HEADERS = {"User-Agent": "forum-watcher/1.0 by u/K-E-E-MOO1"}


def search_subreddit(subreddit, keyword):
    url = f"https://www.reddit.com/r/{subreddit}/search.json"
    params = {"q": keyword, "restrict_sr": 1, "sort": "new", "limit": 10}
    r = requests.get(url, headers=HEADERS, params=params, timeout=10)
    r.raise_for_status()
    return r.json()["data"]["children"]


def notify(post, subreddit, keyword):
    title = post["data"]["title"]
    permalink = f"https://reddit.com{post['data']['permalink']}"
    text = f'[FORUM] r/{subreddit} matched "{keyword}"\n{title}\n{permalink}'
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
        timeout=10,
    )


def run():
    cutoff = time.time() - LOOKBACK_SECONDS
    for subreddit in SUBREDDITS:
        for keyword in KEYWORDS:
            try:
                posts = search_subreddit(subreddit, keyword)
            except requests.RequestException:
                continue
            for post in posts:
                if post["data"]["created_utc"] >= cutoff:
                    notify(post, subreddit, keyword)
            time.sleep(2)  # be polite between calls


if __name__ == "__main__":
    run()
