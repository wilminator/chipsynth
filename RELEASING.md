# Releasing chipsynth

How a version gets to PyPI, and what you do by hand. Nothing is uploaded until you approve it on GitHub, and no token or
password exists anywhere: PyPI trusts the `CI-CD` workflow of this repository ("trusted publishing", OIDC).

## How it works

| file | does |
|---|---|
| `.github/workflows/ci-cd.yml` | the one workflow that starts by itself: on every push and pull request it calls quality and build; on a push to `main` it then calls release and deploy |
| `quality.yml` | the test suite on Python 3.11, 3.12, 3.13 and 3.14 (Linux), and 3.13 on Windows and macOS, with the SDL dummy drivers |
| `build.yml` | builds the sdist and wheel, `twine check --strict`, installs the wheel into a clean environment and runs `chipsynth --help` and a tiny export (`.github/scripts/smoke-wheel.sh`) |
| `release.yml` | python-semantic-release reads the commit messages since the last tag; if one calls for a release it raises the version in `pyproject.toml`, writes `CHANGELOG.md`, commits `chore(release): X.Y.Z`, tags `X.Y.Z` and makes the GitHub release |
| `deploy.yml` | builds that tag again, checks the built version equals the tag, then the **upload job waits for the `release` environment** and publishes to PyPI |

Versions come from Conventional Commits (see `CONTRIBUTING.md`): `feat:` makes a minor version, `fix:` / `perf:` a patch,
`feat!:` or a `BREAKING CHANGE:` footer a major one (a minor one while we are on 0.x). `docs:`, `test:`, `ci:`, `chore:`
and `refactor:` commits make no release. The tag is the bare version (`0.2.0`, no `v`).

## One-time setup (by hand)

### 1. On GitHub: the `release` environment

Repository > Settings > Environments > New environment, name it exactly `release`:

* Required reviewers: add yourself. This is the gate: the upload job stops at "Waiting for review" until you click
  Approve and deploy.
* Deployment branches and tags: Selected branches, `main` only.
* No secrets are needed in it.

### 2. On GitHub: let the workflow push the release commit

* Settings > Actions > General > Workflow permissions: Read and write permissions (the release job also asks for
  `contents: write` itself, but a repository set to read-only can refuse it).
* If `main` is protected (pull requests required), the release commit and tag cannot be pushed by the workflow. Either
  allow `github-actions[bot]` to bypass the rule for `main`, or do not protect `main` against it.

### 3. On pypi.org: register the trusted publisher

The name `chipsynth` was free on PyPI when this was written (checked 2026-11-22; check again, names go quickly). Because
the project does not exist yet, register a **pending** publisher: log in to pypi.org > Your account > Publishing >
"Add a new pending publisher", GitHub tab, and fill in:

| field | value |
|---|---|
| PyPI Project Name | `chipsynth` |
| Owner | `wilminator` |
| Repository name | `chipsynth` |
| Workflow name | `ci-cd.yml` |
| Environment name | `release` |

The workflow name is `ci-cd.yml`, not `deploy.yml`: PyPI checks the workflow that was started, and `deploy.yml` is only
called from it. The first successful upload creates the project and turns the pending publisher into a normal one.
Enable two-factor authentication on the PyPI account if it is not already.

Optional: to rehearse on TestPyPI first, do the same on test.pypi.org (a separate account and publisher) and temporarily
add `repository-url: https://test.pypi.org/legacy/` to the publish step of `deploy.yml`.

## The first release (0.1.0)

`pyproject.toml` already says 0.1.0, and the history before packaging has no Conventional Commits, so the release
step will find nothing to release. Publish 0.1.0 by tagging it yourself:

1. Merge the `pypi-packaging` branch into `main` (its checks must pass).
2. Do the one-time setup above.
3. Tag the merge commit and push the tag (pushing a tag starts no workflow):

   ```console
   $ git checkout main
   $ git pull
   $ git tag -a 0.1.0 -m "chipsynth 0.1.0"
   $ git push origin 0.1.0
   ```

4. Actions > CI-CD > Run workflow, branch `main`, `deploy_tag` = `0.1.0`. It runs the tests and the build check, builds
   the tag, checks the wheel is version 0.1.0, and waits.
5. Open the run, click Review deployments, tick `release`, Approve and deploy.
6. Check https://pypi.org/project/chipsynth/ and try it:

   ```console
   $ python -m venv /tmp/try && /tmp/try/bin/pip install chipsynth && /tmp/try/bin/chipsynth --help
   ```

## Every release after that

1. Work on branches and merge with Conventional Commit messages (a squash merge's title is the commit message that
   counts, so write it as `feat: ...` or `fix: ...`).
2. A push to `main` runs CI-CD. If the commits since the last tag call for a release, the release job makes the version
   commit, the tag and the GitHub release, and the deploy job starts and waits at the `release` environment.
3. Check the new version and the CHANGELOG entry in the run summary, then Review deployments > Approve and deploy.
   Rejecting the review leaves the tag and GitHub release in place without a PyPI upload: upload it later with the
   `deploy_tag` run above, or delete the tag and release and fix forward.

To force a particular bump on the next release, put the commit type for it in a commit message (an empty commit works):

```console
$ git commit --allow-empty -m "fix: release the pending changes"
```

## If something goes wrong

* **The wheel's version differs from the tag**: the build job stops before the upload. Someone changed `version` in
  `pyproject.toml` by hand; let python-semantic-release own that line.
* **PyPI answers "invalid-publisher"** (403 from the upload step): the publisher fields in step 3 do not match. The error
  text shows the claims GitHub sent (repository, workflow, environment); make PyPI's entry say the same.
* **The release job cannot push**: see step 2 (branch protection or read-only workflow permissions).
* **A bad version reached PyPI**: you cannot overwrite or re-upload a version. Yank it on pypi.org (Manage > Releases)
  and release a fixed one.
* **Check a build before releasing it**, locally, from the project root:

  ```console
  $ pip install build twine
  $ python -m build
  $ twine check --strict dist/*
  $ bash .github/scripts/smoke-wheel.sh dist/chipsynth-*.whl
  ```

## Action versions

All actions are pinned to their latest GA release tag, and Dependabot (`.github/dependabot.yml`) opens a pull request
when one gets a new release. Public repositories use GitHub-hosted runners only.
