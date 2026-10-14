# webcompanion

## The service runs a frozen copy

The CLI is an editable install (`uv tool install -e`), so it runs this checkout's code as soon as a file is saved. The launchd service runs `~/.local/share/webcompanion/webcompanion.pyz`, a zipapp that only `webcompanion install-service` rebuilds. Between the two, routes added in the source answer 404 from the daemon, and `webcompanion status` prints `build: daemon X / cli Y` with two different ids.

`githooks/refresh-service` compares those ids and runs `install-service` in the background when they differ (log: `~/.claude/webcompanion/refresh-service.log`). The post-commit, post-merge, post-rewrite and post-checkout hooks call it. In a fresh clone, link them with `githooks/install`. Edits that are saved but not committed do not trigger it: run `githooks/refresh-service` by hand to test them against the daemon.
