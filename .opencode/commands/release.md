---
description: Release this Python OSS project — version bump, changelog, tag, and push in one step
agent: build
---
Release this Python OSS repository.

Load the local `git-release` skill and execute the full release flow.

Run this command from `main` after a feature PR has been merged and you are ready to publish a new version.

The skill will:
1. validate the repository state and tooling
2. run the full `nox` validation gate
3. inspect commit history since the last tag and suggest a version bump
4. show you a preview and wait for your confirmation
5. bump `pyproject.toml`, generate `CHANGELOG.md`, commit, tag, and push

Do not run this from a feature branch. Do not run this with uncommitted changes.
