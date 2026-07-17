# Releasing syncMyMoodle

Releases are mostly automated through GitHub Actions. In practice, you usually only need to:

1. merge the PRs you want in the release,
2. bump the version in `pyproject.toml`,
3. prepare the draft GitHub release for that version,
4. create and push the matching tag,
5. check that the workflow finishes successfully.

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

Once the version bump is on `master`, clean up the draft release and set its tag
to the version from `pyproject.toml`. Do not add a `v` prefix. Finish the draft
before pushing the tag, because that starts the release workflow immediately.

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
Prerelease versions are marked as prereleases on GitHub automatically.

When the workflow runs, it will:

1. check that the tag exists,
2. check that the tag matches `pyproject.toml`,
3. check that a matching GitHub release already exists,
4. build the source distribution and wheel,
5. run `twine check`,
6. install and smoke-test the built wheel and source distribution,
7. upload them to TestPyPI and PyPI,
8. publish the existing GitHub draft release,
9. attach the built packages to the GitHub release.

The workflow uses PyPI Trusted Publishing, so no API tokens are needed.

## Release checklist

1. Merge all PRs that should be included in the release.

2. Check that PR labels and titles look good for the release notes.

3. Bump the version in `pyproject.toml`.

4. Commit the version bump.

5. Wait for Release Drafter to update, edit the draft notes, and set the draft's
   tag to the exact version in `pyproject.toml`. Do not add a `v` prefix.

6. Verify the version and intended tag locally:

   ```bash
   python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])'
   git status --short
   git log -1 --oneline
   ```

   The working tree should be clean. Check that the matching draft exists on
   GitHub before continuing.

7. Create and push the tag only after the version and draft checks pass:

   ```bash
   git tag X.Y.Z
   git push origin X.Y.Z
   ```

8. Watch the **Publish Release** workflow.

9. Approve the Trusted Publishing request on PyPI/TestPyPI if GitHub or PyPI asks for it.

10. Once the workflow is green, check that:

   * the package is available on PyPI and TestPyPI,
   * the GitHub release is published,
   * the release contains the uploaded artifacts.

## Recovering or re-running a release

Use **Re-run failed jobs** on the existing workflow run when possible. This
reuses the packages that were already built and tested.

* If nothing has been uploaded, fix the problem and retry. If the fix changes
  the package, bump the version and make a new tag.
* Retrying TestPyPI is safe: its upload job skips files that are already there.
* Only retry PyPI if it accepted neither file. If it accepted just one, upload
  the missing file from the retained `syncmymoodle-dist` artifact. Do not
  rebuild it.
* If only the GitHub release job failed, retry that job without publishing the
  package again.

Never move a tag or rebuild a version after a package file has been uploaded.
If the workflow artifact has expired, recover the published files and verify
their hashes instead of creating new ones.

To start a new manual run:

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
