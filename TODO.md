# TODO

## Publish a release newer than 1.0.0

PyPI has only 1.0.0, which predates the asset routes annotate's page needs.
claude-annotate's `requirements-test.txt` pins a git commit of this repo for
that reason. Publish the current tree, check `pipx install webcompanion`,
`webcompanion --version` and `webcompanion doctor`, then point that pin at the
release.

## Known issues

- `Registry.persist()` re-reads, unions and writes under the file lock. A row
  another process removed in the meantime is still in this process's memory,
  so the union brings it back.
- `commands/doctor.py` `_log_tail` reads the whole service log into memory to
  return its last lines.
- `uploads.py` answers 408 when the body times out without reading the rest
  of it, so a keep-alive connection is left mid-body and its next request is
  parsed from the leftover bytes.
- `tests/test_registry.py::test_persist_serialises_concurrent_writers` uses
  threads. The flock it is meant to prove serialises processes.
- Two assertions barely constrain anything:
  `tests/test_doctor.py::test_doctor_reports_a_healthy_daemon` and
  `tests/test_static_assets.py::test_the_runtime_does_not_claim_the_token_is_reminted`.

## Migration

`webcompanion migrate` cannot turn non-annotate content (`steps.json`,
`dataflow.json`, `diff.patch`) into items, so those sessions arrive with
`needs_repush: true`. Never test a migration by copying directories: legacy
`sessions.json` holds absolute paths. Use `--into` or a throwaway `HOME`. The
last rehearsal: 30 sessions gave 15 migrated, 14 needs-repush and 1 read-only,
with all 96 comment threads kept.

## Machine note

The launchd service runs under the Command Line Tools python 3.9.6 while the
shell has a newer python, which is why `requires-python >= 3.9` matters.
`launchctl kickstart -k gui/$UID/dev.webcompanion` restarts it; the log is
`~/.claude/webcompanion/dev.webcompanion.log`.
