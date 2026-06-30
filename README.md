# pr-review-bot — ball-in-court PR review board

Code review is a back-and-forth, and PR latency is really "the ball sat in someone's court too
long." This bot reconstructs each open PR's timeline (opens, pushes, reviews, comments), works
out **whose court the ball is in right now** and **how long it's been there**, and keeps a native
**Slack List** in sync — one item per PR, moving between statuses as the ball flips.

One board per repo. The action is generic — it holds no repo names, channels, or roster; each
calling repo passes its own channel + board name, and the repo to scan is auto-detected from the
CI context (`github.repository`).

## How a repo turns it on

Add `.github/workflows/pr-review-bot.yml` calling this repo's composite action:

```yaml
name: PR Review Bot
on:
  pull_request:
    types: [opened, reopened, ready_for_review, review_requested, review_request_removed, synchronize, closed]
  pull_request_review:
    types: [submitted, dismissed]
  pull_request_review_comment:
    types: [created]
  issue_comment:
    types: [created]
  schedule:
    - cron: "0 13-23 * * 1-5"   # hourly board refresh, weekday business hours (UTC)
  workflow_dispatch:
permissions:
  contents: read
  pull-requests: read
concurrency:
  group: pr-review-bot
  cancel-in-progress: true
jobs:
  run:
    if: ${{ github.event_name != 'issue_comment' || github.event.issue.pull_request }}
    runs-on: ubuntu-latest
    steps:
      - uses: earth-mover/pr-review-bot@<commit-sha>   # pin to a commit; org policy forbids @main / @tag
        with:
          channel: C0123456789           # Slack channel id for this repo's board
          board-name: My Repo PR Board
          slack-token: ${{ secrets.SLACK_APP_TOKEN }}
          claude-token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          gh-token: ${{ secrets.GITHUB_TOKEN }}
          reviewers: ${{ secrets.PR_REVIEW_REVIEWERS }}
          # bot-actors: dependabot,github-actions,codecov   # optional; overrides the default ignore-list
```

The caller owns the triggers (PR events + an hourly weekday cron) so the board updates on that
repo's own activity; `GITHUB_TOKEN` reads that repo's PRs. The action carries `bot.py`, so it
lives in exactly one place.

Requirements on the calling repo:
- Secrets `SLACK_APP_TOKEN` and `CLAUDE_CODE_OAUTH_TOKEN` (org-level secrets cover every repo).
  Without the Claude token the bot still runs — it just falls back to the heuristic.
- The `PR_REVIEW_REVIEWERS` org secret (the login→Slack map JSON) — see [Updating reviewers](#updating-reviewers).
- Pin `uses:` to a full commit SHA (org policy rejects `@main` and tags).

## The model

For each active PR we build a merged, time-sorted event stream and classify by last mover
(author's own events filtered out) into one of four courts:

- 🟥 **reviewer** — a reviewer must act (first review, or re-review after the author pushed/replied).
- 🆕 **needs_reviewer** — nobody is reviewing and none is assigned.
- 🟦 **author** — the author must act (address changes, answer a question, or merge).
- ✅ **ready** — approved, just needs merging.

A heuristic (last-mover-wins) computes the base verdict — court, time-in-court, volley count. Then
**Claude reads the actual threads** to refine the court and write the next-move line, falling back
to the heuristic on any error. When several reviewers are requested (the "assign a few, hope one
bites" pattern), the board lists all of them until someone reviews, then narrows to whoever did.

**Stable verdicts.** Claude's wording drifts run-to-run, so a hash of its exact input is stored in
a hidden `ctx` column; an unchanged PR reuses the stored verdict and Claude is only re-consulted
when the PR actually gets new activity. Most runs make zero Claude calls.

## Layout

| Path | Purpose |
|---|---|
| `bot.py` | The bot. Stdlib only — Slack Web API + the `claude` CLI. Reads a small config (repo, channel, board name, bot actors) + the reviewers map. |
| `action.yml` | Composite action repos call; builds the config from inputs + `github.repository` at runtime. |
| `reviewers.example.json` | Sample of the login → Slack map. The real one is **not committed** — it's injected from the `PR_REVIEW_REVIEWERS` org secret. |
| `reconcile.py` | Drift-checks the reviewers map against a GitHub team. |

## Local usage

The reviewers map isn't committed; keep a local `reviewers.json` (gitignored — copy
`reviewers.example.json` and fill it in). Hand `bot.py` a small config file:

```bash
export SLACK_BOT_TOKEN=xoxb-...        # and a logged-in `claude` CLI
cat > /tmp/cfg.json <<'EOF'
{"repo": "my-org/my-repo", "channel_id": "C0123456789", "board_name": "My Repo PR Board"}
EOF
uv run bot.py --config /tmp/cfg.json --reviewers reviewers.json --dry-run   # classify + print
uv run bot.py --config /tmp/cfg.json --reviewers reviewers.json             # sync the board
uv run reconcile.py --org my-org --team my-team                             # reviewers drift check
```

## Updating reviewers

The login → Slack map lives in the `PR_REVIEW_REVIEWERS` **org secret** (visible to the repos with
boards), not in this repo. To change it (e.g. someone new joins):

1. Edit your local `reviewers.json` — add `"<github-login>": {"slack_id": "U…", "name": "First"}`.
   `uv run reconcile.py --org <org> --team <team>` lists anyone on the team who's missing, and (with
   `SLACK_USER_TOKEN` set) auto-proposes their Slack id/name to paste in.
2. Push the updated map to the org secret:
   ```bash
   gh secret set PR_REVIEW_REVIEWERS --org <org> --visibility selected \
     --repos "repo-a,repo-b" < reviewers.json
   ```
   (Needs `admin:org`; run `gh auth refresh -h github.com -s admin:org` first if needed.)

The next board run picks it up — no code change or redeploy.

## Adding a repo

1. Invite the Slack bot to the target channel; add the list as a channel tab once (no API for it).
2. Make sure every reviewer is in the map (see [Updating reviewers](#updating-reviewers)); add the
   repo to the secret's `--repos` list.
3. Add the caller workflow (see [How a repo turns it on](#how-a-repo-turns-it-on)) with the repo's
   `channel` + `board-name`.
