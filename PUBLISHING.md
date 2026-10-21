# Publishing checklist

How this plugin gets from this folder to the community: first a public GitHub repository,
which anyone can install from, then an optional listing in the Hermes plugin catalog,
which is reviewed by Nous maintainers. The rules are in
[Submitting to the Plugin Catalog](https://hermes-agent.nousresearch.com/docs/developer-guide/plugins/catalog-submission).

## 0. Before every release

```bash
~/.hermes/hermes-agent/venv/bin/python tests/run_scenarios.py   # expect 6/6 passed
hermes plugins validate .                                       # expect "Validation passed."
```

Bump `version` in `plugin.yaml` for any release after the first.

## 1. Screenshots (required for a UI plugin)

Take 2–3 PNGs of Hermes Desktop and save them under `docs/screenshots/`:

- `by-bot.png`: the pane in **By bot** view, with a turn expanded to show tool arguments.
- `by-group-chat.png`: the **By Group Chat** view with at least one room section.
- `track-now.png` (optional): the yellow "isn't tracked yet" banner.

Check them for private content before committing: repository paths, prompts, tool output and
room names all show on screen. Use a demo Bot and a throwaway room if you need to. Then add a
`## Screenshots` section to `README.md` that references them, and commit.

## 2. Public repository

The repository is `https://github.com/risyadul/hermes-bot-activity` (public, `main` branch):

```bash
cd ~/StudioProjects/hermes-bot-activity
git push origin main
```

Check that it installs from GitHub. Use a throwaway profile so your real Bots aren't touched:

```bash
hermes profile create ba-smoke --no-alias --no-skills
hermes -p ba-smoke plugins install risyadul/hermes-bot-activity --enable
hermes -p ba-smoke plugins list | grep bot-activity
hermes profile delete ba-smoke
```

People can install from this point on:

- `hermes plugins install risyadul/hermes-bot-activity --enable`
- the link `hermes://plugin/install?repo=risyadul/hermes-bot-activity&enable=1`

## 3. Catalog entry (optional, reviewed)

1. Fork `NousResearch/hermes-agent` and create a branch.
2. Add one file, `plugin-catalog/bot-activity.yaml`. Set `sha` to the full commit you pushed
   (`git rev-parse HEAD`), and set the screenshot URLs to that same commit:

```yaml
name: bot-activity
title: Bot Activity
repo: https://github.com/risyadul/hermes-bot-activity
sha: <output of `git rev-parse HEAD` — 40 hex characters>
version: "0.1.0"
description: Live per-Bot activity timeline for Hermes Desktop — turns, tool calls, approvals and Group Chat streams, grouped by Bot or by Group Chat.
maintainer: risyadul
tier: community
category: desktop
requires_hermes: ">=0.21.6"
screenshots:
  - https://raw.githubusercontent.com/risyadul/hermes-bot-activity/<sha>/docs/screenshots/by-bot.png
  - https://raw.githubusercontent.com/risyadul/hermes-bot-activity/<sha>/docs/screenshots/by-group-chat.png
known_issues:
  - Group Chat attribution relies on Desktop's member-session title and room prompt header, which are not public APIs.
  - A Bot's first turn in a new Group Chat thread, or a re-titled session, can show its room without a thread label.
capabilities:
  provides_tools: []
  provides_hooks:
    - pre_llm_call
    - post_llm_call
    - pre_tool_call
    - post_tool_call
    - pre_approval_request
    - post_approval_response
    - on_room_member_activity
  provides_middleware: []
  requires_env: []
```

3. Open the PR, using the description below. Wait for the catalog CI to go green; it runs
   `hermes plugins validate` at your pinned commit.

### PR description (rule 13 disclosures)

```markdown
Adds **bot-activity**: a live activity timeline for Hermes Desktop Bots.
Group Chat members run their turns in hidden sessions, so a room only shows "working…".
This plugin records each Bot's turns, tool calls (arguments, result, duration), approvals and
hosted-room reasoning streams through observer hooks, and shows them in a Desktop pane,
grouped by Bot or by Group Chat.

**Surfaces used:** observer hooks `pre/post_llm_call`, `pre/post_tool_call`,
`pre/post_approval_request`, `on_room_member_activity`; `broadcast_plugin_event`; a dashboard
backend router; Desktop SDK pane + route + sidebar nav + status-bar item.
No core overrides, no tools, no middleware.

**Disclosures**
- No network calls, no telemetry.
- Stores tool arguments/results, Bot replies, reasoning streams and approval commands
  (redacted with `agent.redact`, capped per field) in one local SQLite file shared by all
  profiles: `<root>/plugin-data/bot-activity/activity.db`. 3-day / 20k-row retention by default.
- Reads outside its own data, read-only: each Bot profile's `state.db` (session title, parent id,
  first user message) and `profile.yaml` `ui_meta` (Group Chat names, Bot titles), to attribute
  activity to Group Chats; each profile's `config.yaml` to see whether the plugin is enabled.
- One background writer thread per Hermes process; a room-attribution pass every 2 minutes.
- Shell commands only on an explicit user click (**Track now** / **Update**):
  `hermes -p <bot> plugins install <same source> --ref <same sha> --enable` when Hermes
  installed this plugin, else a file copy into `<profile>/plugins/bot-activity` plus
  `hermes -p <bot> plugins enable bot-activity`. Profiles where the user disabled the plugin
  are never re-enabled. No self-update: nothing is fetched outside `hermes plugins install`.

Screenshots: <attach>
```

## 4. Releasing an update

1. Commit, run the step 0 checks, and bump `version` in `plugin.yaml`.
2. Push. GitHub installs (`hermes plugins update bot-activity`) follow the repository.
3. For the catalog: open a new PR that changes `sha`, `version` and any screenshot URLs. The
   reviewers read the commit range you are adopting.
