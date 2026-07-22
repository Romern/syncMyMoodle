# How synchronization works

This page explains what happens during a sync, how syncMyMoodle decides whether
to download or update a file, and which local state it keeps between runs.

For commands and settings, see the [CLI reference](cli-reference.md) and
[configuration reference](configuration.md).

## The synchronization model

syncMyMoodle is a one-way download client:

- Moodle and supported linked services are treated as the remote source.
- New remote material can be added locally.
- Previously downloaded material can be updated when the remote version changes.
- Local edits are never uploaded.
- Content removed from Moodle is reported, but the existing local files are
  kept.

The sync directory is therefore not a mirror in the destructive sense. A sync
does not remove ordinary local files merely because they are no longer visible
remotely.

## Run lifecycle

A normal run proceeds through these stages.

### 1. Load and validate configuration

syncMyMoodle loads either:

- the global configuration, or
- the file selected with `--config`.

It applies command-line sync overrides after loading the file. Invalid settings,
unknown keys, incompatible values, or unsafe paths stop the run before course
content is written.

Relative paths in TOML resolve from the configuration file's directory.
Relative command-line paths resolve from the current working directory.

### 2. Obtain a usable Moodle token record

The stored Moodle token record normally contains:

- a Moodle API token, and
- when Moodle supplied one, a browser-login token used to create temporary
  browser sessions.

Valid stored tokens are used directly. syncMyMoodle does not contact RWTH SSO
for every run.

When the API token is missing or confirmed invalid:

- browser-assisted configurations stop and ask you to run
  `syncmymoodle auth login`;
- the interactive `prompt` TOTP provider also stops and asks for an explicit
  login;
- a reusable TOTP provider may obtain replacement tokens automatically.

A sync performs at most one automatic RWTH sign-in attempt. Temporary network
or server failures do not cause token replacement because the program cannot
reliably conclude that the token is invalid.

See [Authentication](authentication.md) for the complete lifecycle.

### 3. Discover and select courses

syncMyMoodle asks Moodle for the courses available to the configured account,
then applies course selection.

`courses.selected` is an explicit allowlist and takes priority over:

- `courses.semesters`;
- `courses.skip`;
- `courses.exclude_roles`.

When no explicit course list is configured, semester, skip, and directly
assigned role filters narrow the discovered set. Semester IDs are read from the
first four characters of Moodle's course `idnumber`, for example `25ws`.

Course-role filtering uses directly assigned Moodle course-role shortnames. If
the role lookup for a course fails, syncMyMoodle records the failure and keeps
the course rather than excluding it on uncertain information.

### 4. Inventory sections, activities, and resources

For every selected course, syncMyMoodle obtains the Moodle course contents and
builds a local download tree.

Filtering occurs from broadest to narrowest:

1. course selection;
2. section exclusions;
3. module/activity exclusions;
4. module-type switches;
5. linked-content and domain rules;
6. filename, extension, and known-size filters.

An excluded section removes every module beneath it from the planned tree. An
excluded module removes that activity or resource before its files and links
are processed.

Use the following command to see which configured rule excluded each item:

```shell
syncmymoodle --dry-run --show-filtered
```

### 5. Discover linked content

When `links.follow_links = true`, supported handlers inspect Moodle descriptions,
pages, labels, H5P content, and selected activities for links and embeds.

Built-in linked sources include:

- YouTube;
- RWTH Opencast;
- public Sciebo shares;
- emedia Medizin VEIRA.

The individual `links.*` switches can disable a source. Turning off
`links.follow_links` disables all of them together.

`filters.exclude_links` is applied before a discovered link is followed.
`filters.allowed_domains`, when nonempty, restricts discovered HTTP(S) links to
the listed domains and their allowed subdomains.

Moodle files exposed directly through the API are not general web crawling and
do not depend on the domain allowlist.

### 6. Generate safe local paths

Course, section, module, and file names are converted into filesystem-safe path
components. The path builder:

- removes or replaces characters that are invalid on supported platforms;
- protects Windows reserved names;
- shortens very long components with stable hash suffixes;
- adds stable suffixes where otherwise identical names collide.

`courses.prefix_handling` controls how a leading course prefix such as `(VO)` is
represented in the course directory name.

Because collision handling and shortening are stable, repeated runs normally
choose the same local path for the same remote item.

### 7. Compare the remote inventory with local state

Each course has a private metadata cache beneath the sync directory. The cache
records the previous inventory and source metadata needed to recognize unchanged
or updated material.

Depending on the source, remote-change detection can use:

- Moodle content hashes;
- Moodle modification timestamps;
- HTTP validators;
- source-specific IDs and metadata;
- hashes of generated artifacts, such as quiz snapshots.

The exact information available varies by Moodle module and linked service.
Unknown remote metadata is handled conservatively; syncMyMoodle does not claim a
file changed merely because a source omitted a validator.

### 8. Decide whether to download or update

For a target path that does not yet exist, syncMyMoodle plans a normal download.

For an existing target:

- If remote updates are disabled, the file is left in place.
- If the remote item is unchanged, the file is counted as unchanged.
- If the remote item changed and the local file still matches the previously
  synced copy, the remote version replaces it.
- If both the remote item and the local file changed, conflict handling applies.

`downloads.conflict_handling` has three modes:

| Mode        | Result when both sides changed                                                           |
|-------------|------------------------------------------------------------------------------------------|
| `rename`    | Preserve the local version as a `.syncconflict...` copy, then install the remote version |
| `keep`      | Leave the local version in place and skip the remote update                              |
| `overwrite` | Replace the local version and discard its local changes                                  |

`rename` is the safest general-purpose choice and is used by interactive setup.

> [!WARNING]
> `overwrite` can permanently destroy local edits.

### 9. Stage and install downloads

Writing syncs use temporary staging locations before files are installed at
their final paths. This reduces the chance that an interrupted transfer leaves a
partially written target file.

A run lock under the sync directory prevents two writing syncs from modifying
the same tree concurrently. Read-only dry runs do not take the writer lock.

Internal cache and staging paths are treated as private paths. Unsafe symlinks
or reparse points within syncMyMoodle-managed internal locations are rejected
rather than followed.

### 10. Persist metadata and report the result

After processing a course, syncMyMoodle stores the updated course inventory and
source metadata unless the run is a dry run.

Every run ends with a summary. Failures in one course, module, or transfer
normally do not abort all remaining work. The program continues where possible,
then exits nonzero after the summary.

## Dry runs

A dry run performs discovery and planning but does not write downloaded files
or course metadata caches:

```shell
syncmymoodle --dry-run
```

It may still:

- contact Moodle and supported linked services;
- validate authentication;
- enumerate courses and remote content;
- inspect source metadata;
- estimate or retrieve sizes where needed for planning.

It does not make the run offline or prevent all network requests.

A browser session created only for a dry run is not saved as persistent local
session state.

Combine dry-run mode with verbose or filter output when diagnosing behavior:

```shell
syncmymoodle --dry-run --verbose
syncmymoodle --dry-run --show-filtered
```

## Local metadata layout

syncMyMoodle creates private metadata beneath the sync directory. Names may
include:

- `.syncmymoodle-cache` for run-level internal state and the writer lock;
- `.syncmymoodle_cache` entries associated with course metadata.

These files are syncMyMoodle state. Do not edit or copy them between accounts manually,
as this can lead to unexpected behavior.

The metadata is account-bound so that one account's cache is not silently used
for another account when the same sync directory is used with multiple Moodle
accounts.

Use the supported cleanup command rather than deleting selected cache files by
hand:

```shell
syncmymoodle clean caches
```

See [Cleanup and troubleshooting](cleanup-and-troubleshooting.md) before using
`--apply`.

## Remote removals

When an item from the previous course inventory is no longer present in Moodle,
syncMyMoodle reports the removal and explicitly notes that local files are kept.

This behavior avoids destructive surprises, but it also means that the local
sync directory can contain historical material that Moodle no longer exposes.
Review and remove such content manually when appropriate.

## Generated content

Some outputs are generated rather than copied byte-for-byte from Moodle:

- quiz attempts can be converted to offline HTML and PDF;
- YouTube and emedia downloads are produced through yt-dlp;
- linked-service handlers derive local names and metadata from source APIs.

Generated outputs participate in the same inventory, update, and local-conflict
model where the source provides enough identity and version information.

## Failure and exit behavior

The principal exit statuses are:

| Status | Meaning                                                            |
|-------:|--------------------------------------------------------------------|
|    `0` | The requested operation completed successfully                     |
|    `1` | A sync or diagnostic operation completed with one or more failures |
|    `2` | Command-line usage or argument error                               |
|  `130` | Interrupted with Ctrl+C                                            |

A status of `1` can accompany useful partial results. Read the final summary and
the earlier course/module diagnostics to identify what succeeded and what did
not.

## Related documentation

- [Getting started](getting-started.md)
- [Everyday recipes](everyday-recipes.md)
- [Configuration reference](configuration.md)
- [Authentication reference](authentication.md)
- [Quizzes and linked content](quizzes-and-linked-content.md)
- [Cleanup and troubleshooting](cleanup-and-troubleshooting.md)
