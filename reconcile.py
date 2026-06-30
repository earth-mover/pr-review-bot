#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Keep reviewers.json in sync with the GitHub team.

Default mode prints drift: team members missing from the map, and mapped logins no longer
on the team. With SLACK_USER_TOKEN set (a token carrying users:read + users:read.email),
it also proposes Slack matches by name/email for any missing member, ready to paste into
reviewers.json after a human eyeballs them.

The committed reviewers.json was originally built via the Slack MCP (name search); this is
the unattended path for refreshing it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent


def run_gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def team_members(org: str, team: str) -> dict[str, str]:
    logins = json.loads(run_gh(["api", f"/orgs/{org}/teams/{team}/members", "--paginate"]))
    out: dict[str, str] = {}
    for m in logins:
        login = m["login"]
        profile = json.loads(run_gh(["api", f"/users/{login}"]))
        out[login] = profile.get("name") or login
    return out


def slack_users(token: str) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    cursor = ""
    while True:
        url = f"https://slack.com/api/users.list?limit=200&cursor={cursor}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
        if not body.get("ok"):
            raise RuntimeError(f"slack users.list failed: {body.get('error')}")
        users.extend(body["members"])
        cursor = body.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return users


def propose_match(name: str, users: list[dict[str, Any]]) -> dict[str, str] | None:
    norm = name.strip().lower()
    for u in users:
        if u.get("deleted") or u.get("is_bot"):
            continue
        profile = u.get("profile", {})
        real = (profile.get("real_name") or "").strip().lower()
        if real == norm:
            return {"slack_id": u["id"], "name": profile.get("display_name") or name, "email": profile.get("email", ""), "tz": u.get("tz")}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="GitHub org, e.g. earth-mover")
    parser.add_argument("--team", required=True, help="Reviewer team slug whose members should be mapped")
    parser.add_argument("--reviewers", type=Path, default=HERE / "reviewers.json")
    args = parser.parse_args()

    org, team = args.org, args.team
    mapped = {k for k in json.loads(args.reviewers.read_text()) if not k.startswith("_")}
    members = team_members(org, team)

    missing = sorted(set(members) - mapped)
    stale = sorted(mapped - set(members))

    print(f"team {org}/{team}: {len(members)} members, {len(mapped)} mapped")
    if stale:
        print("\n⚠️  mapped but no longer on the team (remove from reviewers.json):")
        for login in stale:
            print(f"  - {login}")
    if not missing:
        print("\n✅ every team member is mapped.")
        return 0

    print(f"\n⚠️  {len(missing)} team member(s) missing from reviewers.json:")
    token = os.environ.get("SLACK_USER_TOKEN")
    users = slack_users(token) if token else []
    for login in missing:
        name = members[login]
        match = propose_match(name, users) if users else None
        if match:
            print(f'  "{login}": {json.dumps(match)},')
        else:
            hint = "" if token else " (set SLACK_USER_TOKEN to auto-propose)"
            print(f"  - {login} ({name}) — no Slack match{hint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
