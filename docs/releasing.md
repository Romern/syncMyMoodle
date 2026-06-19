# Releasing syncMyMoodle

Releases are mostly automated through GitHub Actions. In practice, you usually only need to:

1. merge the PRs you want in the release,
2. bump the version in `pyproject.toml`,
3. create and push a matching tag,
4. check that the workflow finishes successfully.

## Before releasing

Make sure merged PRs have useful titles and the right labels. These labels are used for the generated release notes.

Common labels are:

* `feature`
* `bug`
* `maintenance`
* `dependencies`
* `documentation`

Version labels such as `semver:major`, `semver:minor`, and `semver:patch` help Release Drafter suggest the next version.

Use `skip-changelog` for PRs that should not appear in the release notes.

## Release notes

The `release-drafter.yml` workflow updates a draft GitHub release whenever something is pushed to `master`.

Before publishing a release, open the draft release on GitHub and clean up the wording if needed. Do this shortly before tagging, because new merges can update the draft again.

## Publishing a release

The release workflow is `.github/workflows/release.yaml`.

It runs automatically when you push a version tag, for example:

```bash
git tag 0.2.4
git push origin 0.2.4
```

Supported tag formats include:

```text
0.2.3
0.2.3.post1
0.3.0-rc.1
```

The tag must match the version in `pyproject.toml`.

When the workflow runs, it will:

1. check that the tag exists,
2. check that the tag matches `pyproject.toml`,
3. check that a matching GitHub release already exists,
4. build the source distribution and wheel,
5. run `twine check`,
6. upload the package to TestPyPI and PyPI,
7. publish the existing GitHub draft release,
8. upload the built artifacts to the GitHub release.

The workflow uses PyPI Trusted Publishing, so no API tokens are needed.

## Release checklist

1. Merge all PRs that should be included in the release.

2. Check that PR labels and titles look good for the release notes.

3. Bump the version in `pyproject.toml`.

4. Commit the version bump.

5. Create and push the tag:

   ```bash
   git tag X.Y.Z
   git push origin X.Y.Z
   ```

6. Open the GitHub draft release and adjust the notes if needed.

7. Watch the **Publish Release** workflow.

8. Approve the Trusted Publishing request on PyPI/TestPyPI if GitHub or PyPI asks for it.

9. Once the workflow is green, check that:

   * the package is available on PyPI and TestPyPI,
   * the GitHub release is published,
   * the release contains the uploaded artifacts.

## Re-running a release

To re-run the workflow manually:

1. Open the **Actions** tab.
2. Select **Publish Release**.
3. Click **Run workflow**.
4. Enter the release tag you want to publish.

## Trusted Publishing setup

This is already configured.

For reference, the setup is:

1. Open **Manage project -> Publishing** on PyPI and TestPyPI.
2. Click **Add a trusted publisher -> GitHub Actions**.
3. Select this repository.
4. Use the workflow `.github/workflows/release.yaml`.
5. Use the matching environment:

   * `pypi`
   * `testpypi`
6. After the first run, approve the pending publisher once in the PyPI/TestPyPI UI.
