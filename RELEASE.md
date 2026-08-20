# Release checklist

## Before publishing

- [ ] Confirm repository name and public owner (`chriopter/hermes-basecamp`).
- [ ] Review trademark wording and independent-project disclaimer.
- [ ] Run `PYTHONPATH=/path/to/hermes-agent .venv/bin/python -m pytest -q`.
- [ ] Run `uvx ruff check .`.
- [ ] Run Pyright with the Hermes source path.
- [ ] Run `hermes plugins doctor . --ci`.
- [ ] Build the wheel and verify the `hermes_agent.plugins` entry point.
- [ ] Run the read-only live Basecamp connect/poll/disconnect smoke test.
- [ ] Scan tracked files for secrets and real Basecamp account/person/project IDs.
- [x] Pin GitHub Actions (`actions/checkout`, `astral-sh/setup-uv`) to immutable commit SHAs and pin the hermes-agent checkout to a reviewed SHA.
- [ ] Review the complete staged diff with an independent reviewer.
- [ ] Verify Author and Committer are the intended public repository identity.

## Publish

```bash
gh repo create chriopter/hermes-basecamp --public --source=. --remote=origin
git push -u origin main
gh release create v0.2.0 dist/hermes_basecamp-0.2.0-py3-none-any.whl \
  --title "hermes-basecamp v0.2.0" \
  --notes-file CHANGELOG.md
```

## Announce

- Basecamp agent/CLI community
- Nous Research Discord `#plugins-skills-and-skins`
- Link to the official Basecamp CLI and Agent Skill
- Clearly label the plugin as independent and technology preview

## Post-release

- [ ] Install from the public Git repository into an isolated Hermes profile.
- [ ] Verify plugin discovery and gateway connection from the release tag.
- [ ] Verify a clean install in a temporary Hermes profile.
