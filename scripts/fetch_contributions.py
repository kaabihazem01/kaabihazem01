#!/usr/bin/env python3
"""Refresh data/contributions.json from the GitHub GraphQL API.

    GH_TOKEN=... python scripts/fetch_contributions.py [login]

`login` defaults to $PROFILE_LOGIN, then $GITHUB_REPOSITORY_OWNER.
The default GITHUB_TOKEN of an Action sees your *public* contributions. To
include private ones, add a classic PAT with `read:user` as the repo secret
PROFILE_TOKEN (the workflow prefers it automatically).

Output shape (what scripts/build_cards.py reads):
  {"login", "name", "bio", "days": [{"date": "YYYY-MM-DD", "count": n}, ...],
   "repos": [{"name", "description", "url", "language", "languageColor", "stars", "forks", "fork_of"}, ...]}
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
    bio
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar { weeks { contributionDays { date contributionCount } } }
    }
    pinnedItems(first: 6, types: REPOSITORY) {
      nodes { ... on Repository { ...Repo } }
    }
    repositories(first: 30, ownerAffiliations: OWNER, privacy: PUBLIC, orderBy: {field: STARGAZERS, direction: DESC}) {
      nodes { ...Repo }
    }
  }
}
fragment Repo on Repository {
  name description url stargazerCount forkCount isFork isArchived pushedAt
  parent { nameWithOwner }
  primaryLanguage { name color }
}
"""


def _repo(n: dict) -> dict:
    lang = n.get("primaryLanguage") or {}
    return {
        "name": n["name"],
        "description": n.get("description") or "",
        "url": n["url"],
        "language": lang.get("name"),
        "languageColor": lang.get("color"),
        "stars": n.get("stargazerCount", 0),
        "forks": n.get("forkCount", 0),
        "fork_of": (n.get("parent") or {}).get("nameWithOwner") if n.get("isFork") else None,
    }


def transform(resp: dict, login: str, first_day: dt.date, last_day: dt.date) -> dict:
    user = resp["data"]["user"]
    if user is None:
        raise SystemExit(f"GitHub user {login!r} not found")
    days = {}
    for week in user["contributionsCollection"]["contributionCalendar"]["weeks"]:
        for d in week["contributionDays"]:
            date = dt.date.fromisoformat(d["date"])
            if first_day <= date <= last_day:
                days[d["date"]] = d["contributionCount"]
    # Pinned repos win. With nothing pinned, fall back to your most-starred, most
    # recently pushed public repos (never the profile repo itself or archived ones).
    repos = [_repo(n) for n in user["pinnedItems"]["nodes"] if n]
    if not repos:
        pool = [n for n in user["repositories"]["nodes"] if n and n["name"].lower() != login.lower() and not n.get("isArchived")]
        pool.sort(key=lambda n: n.get("pushedAt") or "", reverse=True)
        pool.sort(key=lambda n: n.get("stargazerCount", 0), reverse=True)  # stable: stars first, then recency
        repos = [_repo(n) for n in pool[:6]]
    return {
        "login": login,
        "name": user.get("name") or login,
        "bio": (user.get("bio") or "").strip(),
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
