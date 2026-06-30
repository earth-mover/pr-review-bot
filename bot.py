#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""PR review-latency bot — a ball-in-court volley tracker.

Review is a back-and-forth. This reads open PRs, reconstructs each one's timeline
(opens, pushes, reviews, comments), works out whose court the ball is in *right now* and how
long it has sat there, then upserts a native Slack List (one item per PR) that moves items
between statuses as the ball flips.

Whose-court detection: a heuristic (last-mover-wins across commits/reviews/comments, author
events filtered out) computes the base verdict, then Claude reads the actual threads to refine
the court and write the next-move line, falling back to the heuristic on any error. Claude runs
by shelling out to the Claude Code CLI (`claude -p`), authed via CLAUDE_CODE_OAUTH_TOKEN.

Runtime Slack scopes: chat:write, channels:read, lists:read, lists:write. Identity map is
precomputed in reviewers.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

HERE = Path(__file__).parent
SLACK_API = "https://slack.com/api"
GH_LIGHT_FIELDS = "number,title,url,isDraft,createdAt,updatedAt,author,labels,reviewRequests,reviewDecision"

Court = Literal["reviewer", "author", "needs_reviewer", "ready"]
Tier = Literal["fresh", "aging", "stale", "overdue"]
TIER_EMOJI: dict[Tier, str] = {"fresh": "🟢", "aging": "🟡", "stale": "🟠", "overdue": "🔴"}
TIER_ORDER: dict[Tier, int] = {"overdue": 0, "stale": 1, "aging": 2, "fresh": 3}
COURT_RANK: dict[Court, int] = {"reviewer": 0, "needs_reviewer": 1, "author": 2, "ready": 3}
LEGACY_LIST_NAME = "PR Review Board"
DEFAULT_IGNORE_AUTHORS = ("dependabot", "app/dependabot")
DEFAULT_SUPPRESS_TITLES = ("do not merge", "[wip]", "wip:", "🚧", "draft:")
DEFAULT_SUPPRESS_LABELS = ("do-not-merge", "blocked", "on-hold", "wip")


@dataclass(frozen=True)
class Config:
    repo: str
    channel_id: str
    board_name: str
    ignore_authors: frozenset[str]
    bot_actors: frozenset[str]
    suppress_title_patterns: tuple[str, ...]
    suppress_labels: frozenset[str]
    active_days: int
    llm_model: str | None

    @staticmethod
    def load(path: Path) -> Config:
        raw = json.loads(path.read_text())
        return Config(
            repo=raw["repo"],
            channel_id=raw["channel_id"],
            board_name=raw.get("board_name", LEGACY_LIST_NAME),
            ignore_authors=frozenset(raw.get("ignore_authors", DEFAULT_IGNORE_AUTHORS)),
            bot_actors=frozenset(raw.get("bot_actors", [])),
            suppress_title_patterns=tuple(p.lower() for p in raw.get("suppress_title_patterns", DEFAULT_SUPPRESS_TITLES)),
            suppress_labels=frozenset(s.lower() for s in raw.get("suppress_labels", DEFAULT_SUPPRESS_LABELS)),
            active_days=int(raw.get("active_days", 21)),
            llm_model=raw.get("llm", {}).get("model"),
        )


@dataclass(frozen=True)
class Reviewer:
    login: str
    slack_id: str | None
    name: str


@dataclass(frozen=True)
class Event:
    at: datetime
    actor: str
    side: Literal["author", "reviewer"]
    kind: str
    text: str = ""


@dataclass(frozen=True)
class Pr:
    number: int
    title: str
    url: str
    author: str
    is_draft: bool
    created_at: datetime
    updated_at: datetime
    labels: tuple[str, ...]
    pending_reviewers: tuple[str, ...]
    pending_teams: tuple[str, ...]
    review_decision: str
    events: tuple[Event, ...]

    @property
    def reviewers_engaged(self) -> tuple[str, ...]:
        return tuple(sorted({e.actor for e in self.events if e.side == "reviewer"}))


@dataclass(frozen=True)
class Verdict:
    pr: Pr
    court: Court
    party: tuple[str, ...]
    since: datetime
    next_move: str
    volleys: int
    source: str
    ctx_hash: str = ""

    @property
    def hours_in_court(self) -> float:
        return (now() - self.since).total_seconds() / 3600.0

    @property
    def tier(self) -> Tier:
        return tier_for(self.hours_in_court)


def now() -> datetime:
    return datetime.now(UTC)


def parse_dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def run_gh(args: list[str]) -> str:
    proc = subprocess.run(["gh", *args], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def fetch_active_prs(repo: str, cfg: Config) -> list[Pr]:
    """Bulk-list cheap metadata, filter to active PRs, then fetch each one's timeline.

    The timeline fields (commits/reviews/comments) blow the GraphQL node budget across all
    open PRs, so we only pull them for the handful that survive the active filter.
    """
    raw = run_gh(["pr", "list", "--repo", repo, "--state", "open", "--limit", "300", "--json", GH_LIGHT_FIELDS])
    meta = [_parse_meta(p, cfg) for p in json.loads(raw)]
    active = [pr for pr in meta if not is_suppressed(pr, cfg) and (now() - pr.updated_at).days <= cfg.active_days]
    return [_attach_timeline(repo, pr, cfg) for pr in active]


def _actor_login(obj: dict[str, Any] | None) -> str:
    return (obj or {}).get("login", "") or "?"


def _parse_meta(p: dict[str, Any], cfg: Config) -> Pr:
    pending_reviewers = tuple(r["login"] for r in p.get("reviewRequests", []) if r.get("login"))
    pending_teams = tuple(t for r in p.get("reviewRequests", []) if not r.get("login") and (t := r.get("slug") or r.get("name")))
    return Pr(
        number=p["number"],
        title=p["title"],
        url=p["url"],
        author=_actor_login(p.get("author")),
        is_draft=p.get("isDraft", False),
        created_at=parse_dt(p["createdAt"]),
        updated_at=parse_dt(p["updatedAt"]),
        labels=tuple(label["name"] for label in p.get("labels", [])),
        pending_reviewers=pending_reviewers,
        pending_teams=pending_teams,
        review_decision=p.get("reviewDecision") or "REVIEW_REQUIRED",
        events=(),
    )


def _attach_timeline(repo: str, pr: Pr, cfg: Config) -> Pr:
    detail = json.loads(run_gh(["pr", "view", str(pr.number), "--repo", repo, "--json", "reviews,commits,comments"]))
    return replace(pr, events=_build_events(detail, pr.author, pr.created_at, cfg))


def _build_events(p: dict[str, Any], author: str, created: datetime, cfg: Config) -> tuple[Event, ...]:
    events: list[Event] = [Event(created, author, "author", "open")]
    for c in p.get("commits", []):
        when = parse_dt(c.get("committedDate") or c.get("authoredDate"))
        if when:
            events.append(Event(when, author, "author", "commit", c.get("messageHeadline", "")))
    for r in p.get("reviews", []):
        when = parse_dt(r.get("submittedAt"))
        actor = _actor_login(r.get("author"))
        if not when or actor in cfg.bot_actors:
            continue
        side = "author" if actor == author else "reviewer"
        events.append(Event(when, actor, side, f"review:{r.get('state', 'COMMENTED')}", (r.get("body") or "")[:200]))
    for c in p.get("comments", []):
        when = parse_dt(c.get("createdAt"))
        actor = _actor_login(c.get("author"))
        if not when or actor in cfg.bot_actors:
            continue
        side = "author" if actor == author else "reviewer"
        events.append(Event(when, actor, side, "comment", (c.get("body") or "")[:200]))
    return tuple(sorted(events, key=lambda e: e.at))


def is_suppressed(pr: Pr, cfg: Config) -> bool:
    if pr.is_draft or pr.author in cfg.ignore_authors:
        return True
    if any(pat in pr.title.lower() for pat in cfg.suppress_title_patterns):
        return True
    return bool({label.lower() for label in pr.labels} & cfg.suppress_labels)


def count_volleys(events: Iterable[Event]) -> int:
    sides = [e.side for e in events if e.kind != "open"]
    return sum(1 for a, b in zip(sides, sides[1:]) if a != b)


def display_name(login: str, reviewers: dict[str, Reviewer]) -> str:
    r = reviewers.get(login)
    return r.name if r else login


def classify_heuristic(pr: Pr, reviewers: dict[str, Reviewer]) -> Verdict:
    volleys = count_volleys(pr.events)
    reviewer_events = [e for e in pr.events if e.side == "reviewer"]
    author_pushes = [e for e in pr.events if e.kind == "commit"]
    author_last = max((e.at for e in pr.events if e.side == "author"), default=pr.created_at)
    reviewer_last = max((e.at for e in reviewer_events), default=None)
    approvals = [e for e in reviewer_events if e.kind == "review:APPROVED"]
    last_approval = max((e.at for e in approvals), default=None)
    last_push = max((e.at for e in author_pushes), default=pr.created_at)

    def v(court: Court, party: tuple[str, ...], since: datetime, move: str) -> Verdict:
        return Verdict(pr, court, party, since, move, volleys, "heuristic")

    if last_approval and last_push <= last_approval and pr.review_decision != "CHANGES_REQUESTED":
        return v("ready", (pr.author,), last_approval, "approved — merge it")
    if not reviewer_events and not pr.pending_reviewers and not pr.pending_teams:
        return v("needs_reviewer", (), pr.created_at, "no reviewer assigned — pick someone")
    if reviewer_last and reviewer_last > author_last:
        last = max(reviewer_events, key=lambda e: e.at)
        verb = {
            "review:CHANGES_REQUESTED": "address changes from",
            "review:COMMENTED": "respond to",
            "comment": "reply to",
        }.get(last.kind, "respond to")
        return v("author", (pr.author,), reviewer_last, f"{verb} {display_name(last.actor, reviewers)}")
    party = pr.reviewers_engaged or pr.pending_reviewers or pr.pending_teams
    if not party:
        return v("needs_reviewer", (), last_push, "no reviewer assigned — pick someone")
    move = "first review" if not reviewer_events else "re-review — author pushed since"
    return v("reviewer", party, last_push, move)


def tier_for(hours: float) -> Tier:
    if hours >= 48:
        return "overdue"
    if hours >= 24:
        return "stale"
    if hours >= 8:
        return "aging"
    return "fresh"


def classify(prs: list[Pr], cfg: Config, reviewers: dict[str, Reviewer], cached: dict[int, dict[str, Any]]) -> list[Verdict]:
    """Heuristic computes the base verdict; Claude refines court + next-move, but only for PRs
    whose context changed since last time (ctx_hash miss) — unchanged PRs reuse the stored Claude
    answer, so an idle PR's verdict is stable run-to-run instead of drifting with LLM randomness."""
    base = [replace(v, ctx_hash=_ctx_hash(v, reviewers)) for v in (classify_heuristic(pr, reviewers) for pr in prs)]
    sid_to_login = {r.slack_id: login for login, r in reviewers.items() if r.slack_id}

    def cache_hit(v: Verdict) -> dict[str, Any] | None:
        c = cached.get(v.pr.number)
        return c if c and c.get("hash") == v.ctx_hash and c.get("court") in COURT_RANK else None

    fresh = {cv.pr.number: cv for cv in apply_llm([v for v in base if not cache_hit(v)], cfg, reviewers)}
    out = []
    for v in base:
        c = cache_hit(v)
        if not c:
            out.append(fresh.get(v.pr.number, v))
            continue
        court: Court = c["court"]
        if court == "reviewer":
            # Reuse the exact reviewers Claude last stored (reverse-mapped from the assignee cell) so
            # a cached row keeps its members instead of being recomputed back to the full requested set.
            party = tuple(login for u in c["assignee"] if (login := sid_to_login.get(u)))
        elif court in {"author", "ready"}:
            party = (v.pr.author,)
        else:
            party = ()
        out.append(replace(v, court=court, party=party, next_move=c["next_move"], source="cached"))
    out.sort(key=lambda v: (COURT_RANK[v.court], TIER_ORDER[v.tier], -v.hours_in_court))
    return out


# ----- LLM thread-reader (pluggable; heuristic stays the fallback) -----

LLM_SYSTEM = (
    "You triage GitHub PRs by whose court the ball is in. Code review is a back-and-forth; "
    "decide who must act NEXT from the actual conversation. 'reviewer' = a reviewer must act "
    "(review or re-review). 'author' = the PR author must act (address feedback, answer a "
    "question, or merge). 'needs_reviewer' = nobody is reviewing and none is assigned. "
    "'ready' = approved, just needs merging. Read nuance: an author offering 'up to you' hands "
    "the ball back; an unanswered reviewer question is the author's court; a nit is not blocking.\n"
    "For 'reviewer' court, fill `reviewers` with EVERYONE still expected to act: if nobody has "
    "reviewed yet, list ALL requested reviewers (the author assigned several hoping any one picks "
    "it up — they are all candidates); once someone has reviewed, narrow to only those who have "
    "engaged. For 'author'/'ready' use the author's name; for 'needs_reviewer' leave it empty."
)


def _llm_item(v: Verdict, reviewers: dict[str, Reviewer]) -> dict[str, Any]:
    """The exact per-PR input Claude sees — also what the ctx_hash is computed over."""
    pr = v.pr
    return {
        "number": pr.number,
        "title": pr.title,
        "author": display_name(pr.author, reviewers),
        "pending_reviewers": [display_name(r, reviewers) for r in pr.pending_reviewers],
        "review_decision": pr.review_decision,
        "timeline": [
            {
                "at": e.at.strftime("%Y-%m-%d %H:%M"),
                "who": "AUTHOR" if e.side == "author" else display_name(e.actor, reviewers),
                "what": e.kind,
                "text": e.text.replace("\n", " ").strip()[:160],
            }
            for e in pr.events[-12:]
        ],
    }


def _ctx_hash(v: Verdict, reviewers: dict[str, Reviewer]) -> str:
    return hashlib.sha1(json.dumps(_llm_item(v, reviewers), sort_keys=True).encode()).hexdigest()[:16]


def build_llm_prompt(verdicts: list[Verdict], reviewers: dict[str, Reviewer]) -> str:
    items = [_llm_item(v, reviewers) for v in verdicts]
    schema = '{"number":int,"court":"reviewer|author|needs_reviewer|ready","reviewers":["names whose court the ball is in"],"next_move":"<=10 words, imperative"}'
    return (
        "For each PR below, decide whose court the ball is in NOW.\n"
        f"Return ONLY a JSON array (no prose, no code fences) of objects: {schema}\n\n"
        f"PRS:\n{json.dumps(items, indent=1)}"
    )


def _llm_call(prompt: str, cfg: Config) -> str:
    """Shell out to the Claude Code CLI with project MCP/settings disabled so it starts fast."""
    cmd = ["claude", "-p", prompt, "--output-format", "text", "--strict-mcp-config"]
    if cfg.llm_model:
        cmd += ["--model", cfg.llm_model]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240, cwd=tempfile.gettempdir())
    if proc.returncode != 0:
        raise RuntimeError(f"claude cli failed: {proc.stderr.strip()[:300]}")
    return proc.stdout


def _parse_llm_json(raw: str) -> list[dict[str, Any]]:
    text = raw.strip()
    if "```" in text:
        text = text.split("```")[1].removeprefix("json").strip()
    start, end = text.find("["), text.rfind("]")
    return json.loads(text[start : end + 1]) if start >= 0 < end else []


def apply_llm(verdicts: list[Verdict], cfg: Config, reviewers: dict[str, Reviewer]) -> list[Verdict]:
    if not verdicts:
        return verdicts
    prompt = LLM_SYSTEM + "\n\n" + build_llm_prompt(verdicts, reviewers)
    try:
        parsed = {d["number"]: d for d in _parse_llm_json(_llm_call(prompt, cfg))}
    except Exception as exc:  # noqa: BLE001 — any LLM/parse failure must degrade to heuristic
        print(f"warning: llm classify failed, using heuristic: {exc}", file=sys.stderr)
        return verdicts
    name_to_login = {r.name: login for login, r in reviewers.items()}
    out = []
    for v in verdicts:
        d = parsed.get(v.pr.number)
        if not d or d.get("court") not in COURT_RANK:
            out.append(v)
            continue
        court: Court = d["court"]
        if court == "reviewer":
            logins = tuple(name_to_login[n] for n in (d.get("reviewers") or []) if n in name_to_login)
            party = logins or v.party  # fall back to the heuristic set if no name maps to a login
        elif court in {"author", "ready"}:
            party = (v.pr.author,)
        else:
            party = ()
        out.append(replace(v, court=court, party=party, next_move=d.get("next_move", v.next_move), source="claude"))
    return out


def humanize_hours(h: float) -> str:
    if h < 1:
        return f"{int(h * 60)}m"
    if h < 48:
        return f"{int(h)}h"
    return f"{int(h / 24)}d"


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


COURT_CHIP: dict[Court, str] = {"reviewer": "🟥", "needs_reviewer": "🆕", "author": "🟦", "ready": "✅"}
COURT_TITLE: dict[Court, str] = {
    "reviewer": "🟥 Waiting on a reviewer",
    "needs_reviewer": "🆕 Needs a reviewer",
    "author": "🟦 On the author",
    "ready": "✅ Ready to merge",
}


# ----- Slack web API -----


def slack_request(method: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{SLACK_API}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def slack_get(method: str, token: str, params: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(f"{SLACK_API}/{method}?{urllib.parse.urlencode(params)}", headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def slack_call(method: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = slack_request(method, token, payload)
    if not body.get("ok"):
        raise RuntimeError(f"slack {method} failed: {body.get('error')}")
    return body


# ----- Slack Lists surface (native task board: one item per PR, moves between courts) -----

LIST_STATUS_CHOICES = [
    {"value": "reviewer", "label": "🟥 Review", "color": "red"},
    {"value": "needs_reviewer", "label": "🆕 Unassigned", "color": "orange"},
    {"value": "author", "label": "🟦 Author", "color": "blue"},
    {"value": "ready", "label": "✅ Ready", "color": "green"},
]
LIST_SCHEMA = [
    {"key": "title", "name": "PR", "type": "text", "is_primary_column": True},
    {"key": "assignee", "name": "Whose court", "type": "user", "options": {"format": "multi_entity", "show_member_name": True}},
    {
        "key": "status",
        "name": "Status",
        "type": "select",
        "options": {"format": "single_select", "choices": LIST_STATUS_CHOICES},
    },
    {"key": "since", "name": "Waiting since", "type": "date"},
    {"key": "next_move", "name": "Next move", "type": "text"},
    {"key": "volleys", "name": "↩︎", "type": "number"},
    {"key": "aband", "name": "Abandonment", "type": "rating", "options": {"emoji": ":skull:", "max": 5}},
    {"key": "ctx_hash", "name": "ctx", "type": "text"},  # bookkeeping: hash of Claude's input; hide in views
]


def _owner_logins(v: Verdict) -> tuple[str, ...]:
    if v.court == "reviewer":
        return v.party
    return (v.pr.author,)


def _rich(text: str) -> list[dict[str, Any]]:
    return [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}]}]


def _rich_link(url: str, text: str) -> list[dict[str, Any]]:
    return [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [{"type": "link", "url": url, "text": text}]}]}]


def _abandonment(hours: float) -> int:
    """1–5 skulls by how long the ball has sat untouched in its court."""
    for threshold, rating in ((168, 5), (72, 4), (24, 3), (8, 2)):
        if hours >= threshold:
            return rating
    return 1


def _list_fields(v: Verdict, cols: dict[str, str], reviewers: dict[str, Reviewer]) -> list[dict[str, Any]]:
    fields = [
        {"column_id": cols["title"], "rich_text": _rich_link(v.pr.url, f"#{v.pr.number} {_truncate(v.pr.title, 72)}")},
        {"column_id": cols["status"], "select": [v.court]},
        {"column_id": cols["since"], "date": [v.since.strftime("%Y-%m-%d")]},
        {"column_id": cols["next_move"], "rich_text": _rich(v.next_move)},
        {"column_id": cols["volleys"], "number": [v.volleys]},
    ]
    if "aband" in cols:
        fields.append({"column_id": cols["aband"], "rating": [_abandonment(v.hours_in_court)]})
    if "ctx_hash" in cols and v.ctx_hash:
        fields.append({"column_id": cols["ctx_hash"], "rich_text": _rich(v.ctx_hash)})
    uids = [reviewers[login].slack_id for login in _owner_logins(v) if reviewers.get(login) and reviewers[login].slack_id]
    if uids:
        fields.append({"column_id": cols["assignee"], "user": uids})
    return fields


def list_find(name: str, channel: str, token: str) -> list[str]:
    # All list ids in the channel with this title. types=lists returns Slack Lists shared to the
    # channel (the type string isn't formally documented; revisit if Slack changes it). Paginate
    # so a list past page 1 isn't missed.
    ids: list[str] = []
    page = 1
    while True:
        try:
            resp = slack_get("files.list", token, {"channel": channel, "types": "lists", "count": "100", "page": str(page)})
        except RuntimeError:
            break
        ids += [f["id"] for f in resp.get("files", []) if f.get("title") == name]
        if page >= resp.get("paging", {}).get("pages", 1):
            break
        page += 1
    return ids


def _schema_cols(schema: list[dict[str, Any]]) -> dict[str, str]:
    return {c["key"]: c["id"] for c in schema if c.get("key") and c.get("id")}


def list_columns(list_id: str, token: str) -> dict[str, str]:
    # files.info returns the full schema (all column ids), unlike items which only
    # carry populated fields — so an item with no assignee won't hide that column.
    info = slack_get("files.info", token, {"file": list_id})
    return _schema_cols(info.get("file", {}).get("list_metadata", {}).get("schema", []))


def list_ensure(name: str, channel: str, token: str) -> tuple[str, dict[str, str]]:
    """Find this channel's list by title (stateless) or create + share it."""
    found = list_find(name, channel, token)
    if not found and name != LEGACY_LIST_NAME:
        # Adopt a pre-rename "PR Review Board" list in place (keep its id + channel tab) instead
        # of orphaning it and creating a fresh board under the new name.
        legacy = list_find(LEGACY_LIST_NAME, channel, token)
        if legacy:
            slack_call("slackLists.update", token, {"id": legacy[0], "name": name})
            found = legacy
    if len(found) > 1:
        # Collapse accidental duplicates (e.g. two created during the files.list indexing lag):
        # keep the most-populated, delete the rest.
        found.sort(key=lambda i: len(list_items(i, token)), reverse=True)
        for extra in found[1:]:
            try:
                slack_call("files.delete", token, {"file": extra})
            except RuntimeError:
                pass
        found = found[:1]
    if found:
        return found[0], list_columns(found[0], token)
    created = slack_call("slackLists.create", token, {"name": name, "schema": LIST_SCHEMA})
    list_id = created["list_id"]
    slack_call("slackLists.access.set", token, {"list_id": list_id, "access_level": "write", "channel_ids": [channel]})
    return list_id, _schema_cols(created.get("list_metadata", {}).get("schema", []))


def _item_pr_num(item: dict[str, Any]) -> int | None:
    # Match the /pull/N link in the title cell; fall back to the #N prefix in its plain text so
    # a row whose link a human stripped by editing is still identified (not orphaned/duplicated).
    title = next((f for f in item.get("fields", []) if f.get("key") == "title"), {})
    m = re.search(r"/pull/(\d+)", json.dumps(title)) or re.search(r"#(\d+)", title.get("text") or "")
    return int(m.group(1)) if m else None


def _delete_item(list_id: str, item_id: str, token: str) -> None:
    try:
        slack_call("slackLists.items.delete", token, {"list_id": list_id, "id": item_id})
    except RuntimeError:
        pass


def list_items(list_id: str, token: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params = {"list_id": list_id, "limit": "100"}
        if cursor:
            params["cursor"] = cursor
        resp = slack_get("slackLists.items.list", token, params)
        items.extend(resp.get("items", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return items


def _row_cache(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Per-PR {court, next_move, hash, assignee} from existing rows, for the classify() cache."""
    out: dict[int, dict[str, Any]] = {}
    for it in rows:
        num = _item_pr_num(it)
        if num is None:
            continue
        f = {fld.get("key"): fld for fld in it.get("fields", [])}
        out[num] = {
            "court": (f.get("status", {}).get("select") or [""])[0],
            "next_move": f.get("next_move", {}).get("text") or "",
            "hash": f.get("ctx_hash", {}).get("text") or "",
            "assignee": f.get("assignee", {}).get("user") or [],
        }
    return out


def list_write(
    verdicts: list[Verdict], reviewers: dict[str, Reviewer], list_id: str, cols: dict[str, str], rows: list[dict[str, Any]], token: str
) -> None:
    by_num: dict[int, list[dict[str, Any]]] = {}
    for it in rows:
        if (n := _item_pr_num(it)) is not None:
            by_num.setdefault(n, []).append(it)
    current: set[int] = set()
    for v in verdicts:
        current.add(v.pr.number)
        existing = by_num.get(v.pr.number, [])
        fields = _list_fields(v, cols, reviewers)
        # The board is bot-owned: rewrite every field in place (restores a title link a human
        # edit stripped, and reverts stray edits). Matching now also catches link-stripped rows
        # via the #N text fallback, so they're updated rather than orphaned; collapse any dupes.
        if existing:
            cells = [{**f, "row_id": existing[0]["id"]} for f in fields]
            slack_call("slackLists.items.update", token, {"list_id": list_id, "cells": cells})
            for r in existing[1:]:
                _delete_item(list_id, r["id"], token)
        else:
            slack_call("slackLists.items.create", token, {"list_id": list_id, "initial_fields": fields})
    for num, existing in by_num.items():
        if num not in current:
            for r in existing:
                _delete_item(list_id, r["id"], token)


def load_reviewers(path: Path) -> dict[str, Reviewer]:
    text = path.read_text().strip() if path.exists() else ""
    if not text:
        print(f"warning: no reviewers map at {path}; falling back to GitHub logins", file=sys.stderr)
        return {}
    raw = json.loads(text)
    return {
        login: Reviewer(login=login, slack_id=v.get("slack_id"), name=v.get("name", login))
        for login, v in raw.items()
        if not login.startswith("_")
    }


def summarize(verdicts: list[Verdict], reviewers: dict[str, Reviewer]) -> str:
    if not verdicts:
        return "No active PRs."
    lines = []
    for v in verdicts:
        who = ", ".join(display_name(p, reviewers) for p in v.party) or display_name(v.pr.author, reviewers)
        lines.append(
            f"{TIER_EMOJI[v.tier]} #{v.pr.number} [{v.court}] {who} · {humanize_hours(v.hours_in_court)} · "
            f"{v.next_move} · ↩︎{v.volleys} [{v.source}]"
        )
    return "\n".join(lines)


def cmd_board(cfg: Config, reviewers: dict[str, Reviewer], token: str, dry_run: bool) -> str:
    prs = fetch_active_prs(cfg.repo, cfg)
    if dry_run:
        return summarize(classify(prs, cfg, reviewers, {}), reviewers)
    list_id, cols = list_ensure(cfg.board_name, cfg.channel_id, token)
    rows = list_items(list_id, token)
    verdicts = classify(prs, cfg, reviewers, _row_cache(rows))
    list_write(verdicts, reviewers, list_id, cols, rows, token)
    return summarize(verdicts, reviewers)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.json")
    parser.add_argument("--reviewers", type=Path, default=HERE / "reviewers.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    token = os.environ.get("SLACK_BOT_TOKEN") or os.environ.get("SLACK_APP_TOKEN") or ""
    if not args.dry_run and not token:
        print("error: SLACK_BOT_TOKEN (or SLACK_APP_TOKEN) required unless --dry-run", file=sys.stderr)
        return 2

    print(cmd_board(cfg, load_reviewers(args.reviewers), token, args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
