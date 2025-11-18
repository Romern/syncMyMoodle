# Releasing syncMyMoodle

There are two GitHub Actions workflows that do publishing and prepare release notes.

## Release Drafter workflow

Every push to `master` has it's information go into `release-drafter.yml`, which updates the draft release on GitHub. Labels drive what happens:

- `feature`, `bug`, `maintenance`, `dependencies`, `documentation`, etc. decide
  which section a PR lands in.
- `semver:major`, `semver:minor`, `semver:patch` hint at the next version bump.
- `skip-changelog` hides a PR completely.

Before you tag a release, open the draft on GitHub and tweak the wording. Do it
close to tagging so new merges don't overwrite your edits.

## Publish Release workflow

`release.yaml` runs on tags that look like our versions (`0.2.3`, `0.2.3.post1`,
`0.3.0-rc.1`, ...) or when you trigger it manually. The job order:

1. Build sdist + wheel via `python -m build`.
2. Push both archives to TestPyPI and PyPI using Trusted Publishing (OIDC).
3. Publish the curated release draft and upload the built artifacts to it.

Since this uses Trusted Publishing you don't need API tokens, but PyPI/TestPyPI
must trust the workflow first.

## Trusted Publishing setup

(this is already done)

1. On PyPI and TestPyPI open **Manage project -> Publishing**.
2. Click **Add a trusted publisher -> GitHub Actions**.
3. Point it at this repo, the workflow `.github/workflows/release.yaml`, and the
   matching environment (`pypi` or `testpypi`).
4. After the first workflow run, approve the pending publisher in the UI once.

## Release todos

1. Merge PRs with the right labels and useful titles for the release note draft.
2. Bump the version in `pyproject.toml`.
3. Commit and tag using the version string (`X.Y.Z`, `X.Y.Z.postN`, etc.):
   ```bash
   git tag 0.2.4
   git push origin 0.2.4
   ```
4. Watch the "Publish Release" workflow. Approve the Trusted Publishing request
   on PyPI/TestPyPI if one pops up (didn't have that happen for me yet).
5. Once it's green, PyPI/TestPyPI have the new files and the GitHub release is
   live with the release notes + artifacts.

To re-run the workflow, use the Actions
tab -> **Publish Release -> Run workflow**.
