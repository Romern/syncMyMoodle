# Releasing syncMyMoodle

This page is for project maintainers. Releases are built, validated, and
published by GitHub Actions.

The normal maintainer workflow is:

1. Merge the intended pull requests.
2. Set the release version in `pyproject.toml`.
3. Curate the matching draft GitHub release.
4. Create and push the exact version tag.
5. Monitor the **Publish Release** workflow.
6. Verify PyPI, TestPyPI, and the GitHub release.

## Release automation

Two workflows participate:

| Workflow                                | Purpose                                                             |
|-----------------------------------------|---------------------------------------------------------------------|
| `.github/workflows/release-drafter.yml` | Updates draft release notes after pushes to `master`                |
| `.github/workflows/release.yaml`        | Validates, builds, tests, publishes, and finalizes a tagged release |

The publish workflow uses Python 3.12 for its release environment and PyPI
Trusted Publishing for uploads.

## Pull-request labels and release notes

Release Drafter uses pull-request titles and labels to build the draft notes.

Common changelog labels are:

- `feature` or `enhancement` → **Features**;
- `bug` or `fix` → **Bug Fixes**;
- `chore`, `maintenance`, `refactor`, `dependencies`, or `documentation` →
  **Maintenance**.

Version labels guide the suggested next version:

- `semver:major`;
- `semver:minor`;
- `semver:patch`.

Pull requests without a version label fall back to a patch increment. Use
`skip-changelog` for work that should not appear in release notes.

Before releasing, verify that merged pull requests have concise user-facing
titles and the intended labels. The generated entry format includes the PR
number and author.

## Version and tag format

The release tag must match `project.version` in `pyproject.toml` exactly.

Examples accepted by the workflow include:

```text
1.0.0
1.0.1.post1
1.1.0-rc.1
```

The workflow uses Python packaging version semantics to decide whether the
GitHub release is a prerelease.

Check the configured version locally:

```shell
python - <<'PY'
import tomllib
from pathlib import Path

print(tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"])
PY
```

## Prepare the draft release

Release Drafter updates a draft whenever changes reach `master`.

Before pushing the tag:

1. Open the draft release on GitHub.
2. Set its name or tag to the exact `pyproject.toml` version.
3. Curate the generated notes.
4. Confirm that the intended changes are present and categorized correctly.
5. Keep the release as a draft.

The publish workflow requires a matching draft or already published release. It
will fail early when no release with the exact version can be found.

Complete this step before pushing the tag, because the tag immediately triggers
the publish workflow.

## Pre-release checks

Run the normal project checks from a clean checkout using the project's current
development instructions.

At minimum, verify:

```shell
git status --short
git log -1 --oneline
python -m pytest
python -m build
python -m twine check dist/*
```

Also inspect the built package contents and documentation rendering when the
release changes packaging or docs.

The working tree should be clean before creating the tag.

## Publish the release

After the version commit is on `master` and the draft release is ready:

```shell
git tag X.Y.Z
git push origin X.Y.Z
```

The tag push triggers `.github/workflows/release.yaml`.

## What the workflow validates

The **Validate release inputs** job:

1. Checks out the exact tag.
2. Verifies that the tag resolves to a commit.
3. Verifies that the tag exactly matches `pyproject.toml`.
4. Determines prerelease status from the package version.
5. Verifies that a matching draft or published GitHub release exists.

A mismatch stops the release before building or uploading packages.

## Build and distribution tests

The workflow then:

1. Builds the source distribution and wheel with `python -m build`.
2. Runs `twine check` on both artifacts.
3. Uploads them as the `syncmymoodle-dist` workflow artifact with seven-day
   retention.
4. Creates isolated virtual environments for the wheel and sdist separately.
5. Installs each exact artifact.
6. Runs `.github/scripts/smoke_distribution.py` against each installation.

The isolated smoke tests validate the artifacts that will actually be
published.

## Publication order

After distribution validation succeeds:

1. Upload to TestPyPI with `skip-existing: true`.
2. Upload the same retained artifacts to PyPI.
3. Publish the curated GitHub draft.
4. Mark prereleases appropriately.
5. Attach the wheel and source distribution to the GitHub release.

The PyPI publication job depends on successful TestPyPI publication. The GitHub
release is finalized only after the PyPI job succeeds.

## Release checklist

### Before tagging

- [ ] Intended pull requests are merged.
- [ ] PR titles and changelog labels are correct.
- [ ] `pyproject.toml` contains the intended version.
- [ ] Tests and local build checks pass.
- [ ] The working tree is clean.
- [ ] Release Drafter produced the matching draft.
- [ ] Draft notes are curated.
- [ ] Draft name/tag exactly matches the package version without `v`.

### Publish

```shell
git tag X.Y.Z
git push origin X.Y.Z
```

### After tagging

- [ ] **Validate release inputs** passed.
- [ ] Wheel and sdist validation passed.
- [ ] TestPyPI upload passed.
- [ ] PyPI upload passed.
- [ ] GitHub draft was published.
- [ ] Prerelease status is correct.
- [ ] Both distribution files are attached to the GitHub release.
- [ ] The package pages show the intended version and README.
- [ ] A fresh isolated installation reports the intended version.

Example final smoke check:

```shell
python -m venv /tmp/syncmymoodle-release-check
/tmp/syncmymoodle-release-check/bin/python -m pip install --upgrade syncmymoodle
/tmp/syncmymoodle-release-check/bin/syncmymoodle --version
```

## Manual workflow dispatch

The publish workflow can also be started manually:

1. Open **Actions**.
2. Select **Publish Release**.
3. Choose **Run workflow**.
4. Enter the existing release tag.

Manual dispatch does not replace the tag requirement. The workflow still checks
out and validates the supplied tag, package version, and GitHub release.

## Recovering from a failed release

Prefer **Re-run failed jobs** on the existing workflow run. This reuses the
retained artifacts that were already built and tested.

### Failure before any upload

Fix the problem and retry when the artifacts remain valid.

When the required fix changes package contents, increment the version and create
a new tag. Do not move an existing release tag to different content.

### TestPyPI upload failure

The TestPyPI action uses `skip-existing: true`, so rerunning safely skips files
already accepted there.

### Partial PyPI upload

PyPI does not permit replacing a file for an existing version.

- Retry the PyPI job only when neither distribution file was accepted.
- If PyPI accepted one file but not the other, retrieve the missing original
  file from the retained `syncmymoodle-dist` artifact and upload that exact
  file.
- Do not rebuild the missing file; a rebuild may not be byte-identical to the
  artifact that passed validation.

### GitHub release failure after PyPI succeeds

Rerun only the GitHub release job where possible. Do not republish the package.
The job can locate the matching draft or existing release and uploads the
retained artifacts with `--clobber`.

### Expired workflow artifact

Never rebuild and publish new bytes under a version that already exists on
PyPI. Recover the published files, verify their hashes and provenance, and use
them for GitHub release attachment or auditing.

### Immutable release rule

Once any package file has been uploaded:

- do not move the tag;
- do not change the release commit;
- do not rebuild that version for publication;
- issue a new version for corrections.

## Trusted Publishing

The workflow requests OpenID Connect tokens through GitHub environments named:

- `testpypi`;
- `pypi`.

The corresponding PyPI and TestPyPI project settings must trust:

- this GitHub repository;
- `.github/workflows/release.yaml`;
- the matching environment name.

No long-lived PyPI API token is required by the workflow.

When recreating the configuration:

1. Open the project's **Publishing** settings on PyPI or TestPyPI.
2. Add a GitHub Actions trusted publisher.
3. Select this repository.
4. Enter `.github/workflows/release.yaml`.
5. Enter `pypi` or `testpypi` as appropriate.
6. Confirm any pending publisher association required by the service.


## Related files

- `.github/workflows/release.yaml`
- `.github/workflows/release-drafter.yml`
- `.github/release-drafter.yml`
- `.github/scripts/smoke_distribution.py`
- `pyproject.toml`
