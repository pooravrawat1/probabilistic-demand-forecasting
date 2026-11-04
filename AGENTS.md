# Agent notes

## Commits
- Commit as the repository's configured git user only. Do not add
  `Co-Authored-By` trailers or "Generated with" lines to commit messages.
- Match the existing style: short lowercase subject lines.

## Verification
- Tests: `.venv/bin/python -m pytest -q`
- Full-data pipeline: `.venv/bin/demand-forecast build-data` then
  `.venv/bin/demand-forecast build-labels` (outputs under ignored `data/`).
