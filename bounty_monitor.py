"""
Bounty Watcher
Checks GitHub for new bounty-labeled issues, filters them against config.json,
and sends a Telegram message for anything new that passes the filters.

Designed to run on a schedule via GitHub Actions (see .github/workflows/bounty-check.yml).
State (which issues have already been seen) is stored in seen_issues.json and
committed back to the repo after every run.
"""

import json
import os
import re
import requests

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

CONFIG_FILE = "config.json"
SEEN_FILE = "seen_issues.json"

MAX_SEEN = 3000

DOLLAR_PATTERN = re.compile(r"\$\s?\d|\busd\b", re.IGNORECASE)
_repo_star_cache = {}


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def github_search(query):
    url = "https://api.github.com/search/issues"
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    params = {"q": query, "sort": "created", "order": "desc", "per_page": 30}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("items", [])


def send_telegram(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured - skipping send. Message was:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        },
        timeout=30,
    )
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text}")


def get_repo_full_name(issue):
    return issue["repository_url"].split("/repos/")[-1]


def get_repo_stars(owner, repo):
    """Fetch a repo's star count, with a per-run cache so repeated issues
    from the same repo only cost one extra API call. Returns None (not 0)
    on any failure, so a transient API error never wrongly rejects an issue."""
    key = f"{owner}/{repo}"
    if key in _repo_star_cache:
        return _repo_star_cache[key]
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        resp = requests.get(f"https://api.github.com/repos/{key}", headers=headers, timeout=30)
        resp.raise_for_status()
        stars = resp.json().get("stargazers_count")
    except Exception as e:
        print(f"Could not fetch repo stars for {key}: {e}")
        stars = None
    _repo_star_cache[key] = stars
    return stars


def mentions_dollar_amount(issue):
    text = f"{issue.get('title', '')} {issue.get('body', '')}"
    return bool(DOLLAR_PATTERN.search(text))


def passes_filters(issue, config):
    title = (issue.get("title") or "").lower()
    body = (issue.get("body") or "").lower()
    repo_full_name = get_repo_full_name(issue)
    owner = repo_full_name.split("/")[0].lower()
    labels = [l["name"].lower() for l in issue.get("labels", [])]

    for blocked in config.get("blocked_owners", []):
        if blocked.lower() in owner:
            return False

    for blocked in config.get("blocked_labels", []):
        if blocked.lower() in labels:
            return False

    for kw in config.get("blocked_keywords", []):
        if kw.lower() in title or kw.lower() in body:
            return False

    require_any = config.get("require_any_keyword", [])
    if require_any:
        if not any(kw.lower() in title or kw.lower() in body for kw in require_any):
            return False

    min_stars = config.get("min_repo_stars")
    if min_stars is not None:
        owner_name, repo_name = repo_full_name.split("/", 1)
        stars = get_repo_stars(owner_name, repo_name)
        if stars is not None and stars < min_stars:
            return False

    return True


CLAIMED_SIGNALS = [
    "has claimed all rewards",
    "already assigned",
    "already working on this",
    "already started working",
]


def get_comment_bodies(issue):
    """Fetch an issue's comments (only if there are any) and return their text."""
    if not issue.get("comments"):
        return []
    comments_url = issue.get("comments_url")
    if not comments_url:
        return []
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        resp = requests.get(comments_url, headers=headers, timeout=30)
        resp.raise_for_status()
        return [c.get("body", "") for c in resp.json()]
    except Exception as e:
        print(f"Could not fetch comments: {e}")
        return []


def looks_already_claimed(issue):
    for body in get_comment_bodies(issue):
        lower = (body or "").lower()
        if any(signal in lower for signal in CLAIMED_SIGNALS):
            return True
    return False


def format_message(issue, source_label):
    repo_full_name = get_repo_full_name(issue)
    title = issue.get("title", "(no title)")
    url = issue.get("html_url", "")
    labels = ", ".join(l["name"] for l in issue.get("labels", []))
    created = issue.get("created_at", "")

    body = (issue.get("body") or "").strip()
    if len(body) > 900:
        body = body[:900] + "\n...[truncated, open link for the rest]"

    warning = ""
    if looks_already_claimed(issue):
        warning += "\n\u26A0\uFE0F COMMENTS SUGGEST THIS MAY ALREADY BE CLAIMED \u2014 check before starting\n"
    if not mentions_dollar_amount(issue):
        warning += "\n\U0001F4B0 No dollar amount detected in the text \u2014 verify the payout before starting\n"

    return (
        f"\U0001F514 New match: {source_label}{warning}\n"
        f"<b>{title}</b>\n"
        f"Repo: {repo_full_name}\n"
        f"Labels: {labels}\n"
        f"Opened: {created}\n"
        f"{url}\n\n"
        f"{body}"
    )


def main():
    config = load_json(CONFIG_FILE, {})
    seen = load_json(SEEN_FILE, {"seen": []})
    seen_ids = set(seen["seen"])
    all_encountered = set(seen_ids)

    queries = config.get(
        "queries",
        [{"label": "General bounty search", "q": "label:bounty state:open is:issue"}],
    )

    found_new = False

    for q in queries:
        try:
            items = github_search(q["q"])
        except Exception as e:
            print(f"Error querying '{q['label']}': {e}")
            continue

        for issue in items:
            issue_url = issue["html_url"]
            all_encountered.add(issue_url)

            if issue_url in seen_ids:
                continue

            if not passes_filters(issue, config):
                continue

            message = format_message(issue, q["label"])
            send_telegram(message)
            print(f"NEW MATCH sent: {issue_url}")
            found_new = True

    seen_list = list(all_encountered)[-MAX_SEEN:]
    save_json(SEEN_FILE, {"seen": seen_list})

    if not found_new:
        print("No new matches this run.")


if __name__ == "__main__":
    main()
