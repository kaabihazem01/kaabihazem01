#!/usr/bin/env python3
"""Refresh data/contributions.json from the GitHub GraphQL API.

    GH_TOKEN=... python scripts/fetch_contributions.py [login]

`login` defaults to $PROFILE_LOGIN, then $GITHUB_REPOSITORY_OWNER.
The default GITHUB_TOKEN of an Action sees your *public* contributions. To
include private ones, add a classic PAT with `read:user` as the repo secret
PROFILE_TOKEN (the workflow prefers it automatically).

Output shape (what scripts/build_cards.py reads):
  {"login", "name", "days": [{"date": "YYYY-MM-DD", "count": n}, ...],
   "repos": [{"name", "description", "url", "language", "languageColor", "stars", "forks"}, ...]}
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "contributions.json"

QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    name
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
    pinnedItems(first: 6, types: REPOSITORY) {
      nodes { ... on Repository {
        name description url stargazerCount forkCount primaryLanguage { name color }
      } }
    }
  }
}
"""


def transform(resp: dict, login: str, first_day: dt.date, last_day: dt.date) -> dict:
    user = resp["data"]["user"]
    if user is None:
        raise SystemExit(f"GitHub user {login!r} not found")
    days = {}
    for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]:
        for d in week["contributionDays"]:
            date = dt.date.fromisoformat(d["date"])
            if first_day <= date <= last_day:
                count = d["contributionCount"]
                if not isinstance(count, int) or count < 0:
                    raise SystemExit(f"Invalid contribution count for {date}: {count!r}")
                if d["date"] in days:
                    raise SystemExit(f"Duplicate contribution date from GitHub: {d['date']}")
                days[d["date"]] = count

    expected = {
        (first_day + dt.timedelta(days=i)).isoformat()
        for i in range((last_day - first_day).days + 1)
    }
    if set(days) != expected:
        missing = sorted(expected - set(days))
        extra = sorted(set(days) - expected)
        raise SystemExit(f"Incomplete contribution calendar; missing={missing[:3]}, extra={extra[:3]}")
    repos = []
    for n in user["pinnedItems"]["nodes"]:
        if not n:
            continue
        lang = n.get("primaryLanguage") or {}
        repos.append(
            {
                "name": n["name"],
                "description": n.get("description") or "",
                "url": n["url"],
                "language": lang.get("name"),
                "languageColor": lang.get("color"),
                "stars": n.get("stargazerCount", 0),
                "forks": n.get("forkCount", 0),
            }
        )
    return {
        "login": login,
        "name": user.get("name") or login,
        "days": [{"date": k, "count": v} for k, v in sorted(days.items())],
        "repos": repos,
    }


def main() -> None:
    login = (sys.argv[1] if len(sys.argv) > 1 else "") or os.environ.get("PROFILE_LOGIN") or os.environ.get("GITHUB_REPOSITORY_OWNER")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not login or not token:
        sys.exit("Need a login (argument, PROFILE_LOGIN or GITHUB_REPOSITORY_OWNER) and GH_TOKEN/GITHUB_TOKEN.")

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    start = now - dt.timedelta(days=364)  # the API allows at most one year per request
    body = json.dumps(
        {"query": QUERY, "variables": {"login": login, "from": start.isoformat(), "to": now.isoformat()}}
    ).encode()
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=body,
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json", "User-Agent": "profile-cards"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"GitHub API error {e.code}: {e.read().decode()[:300]}")
    if "errors" in resp:
        sys.exit(f"GraphQL errors: {json.dumps(resp['errors'])[:400]}")

    data = transform(resp, login, start.date(), now.date())
    OUT.parent.mkdir(exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    tmp.replace(OUT)  # no "generated" timestamp on purpose: no data change, no commit
    print(f"{OUT.relative_to(ROOT)}: {len(data['days'])} days, {len(data['repos'])} pinned repos")


if __name__ == "__main__":
    main()
