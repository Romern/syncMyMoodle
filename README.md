# syncMyMoodle

<p>
  <img src="https://raw.githubusercontent.com/Romern/syncMyMoodle/30a29fdcf7206713e49bdd47f1d0dee1a8887294/docs/assets/syncmymoodle-logo.png" alt="syncMyMoodle logo" width="280">
</p>
Synchronization client for RWTH Moodle.

syncMyMoodle downloads course materials from RWTH Moodle into a local directory.
It stores Moodle API tokens locally so normal syncs do not need your RWTH password or TOTP.
Run it again later to add new materials and update files that changed
remotely while preserving local edits according to your conflict settings.

It supports Linux, macOS, and Windows with Python 3.11 or newer.

## What it downloads

- Assignments, submissions, and feedback
- File resources and Moodle folders
- Pages and labels, including linked files and embedded media
- Opencast, YouTube, Sciebo, and emedia Medizin VEIRA content
- Quiz attempts as offline HTML, optionally also as PDF
- H5P packages and supported LTI content

## Installation

Installing syncMyMoodle as an isolated command-line tool is recommended. Use
either [uv](https://github.com/astral-sh/uv) or  [pipx](https://pipx.pypa.io):

```shell
uv tool install syncmymoodle

# Alternatively:
pipx install syncmymoodle
```

Only one of those commands is needed. Afterwards, `syncmymoodle` should be
available directly in your terminal.

You can instead install it into a virtual environment:

```shell
python -m venv .venv

# Linux or macOS:
source .venv/bin/activate

# Windows PowerShell:
.venv\Scripts\Activate.ps1

python -m pip install syncmymoodle
```

When installing from a source checkout, activate a virtual environment and run
`python -m pip install .` from the repository root.

## Quick start

Run the interactive setup once, then start the first sync:

```shell
syncmymoodle setup
syncmymoodle
```

Setup asks for:

- Your RWTH Single Sign-On username
- Your RWTH TOTP serial, such as `TOTP12345678`
- The directory where Moodle files should be stored
- Whether to use a detected password manager for future RWTH sign-ins
- Where to store the Moodle tokens

The TOTP serial is the identifier shown in the
[RWTH IDM Token Manager](https://idm.rwth-aachen.de/selfservice/MFATokenManager).

Setup performs one RWTH sign-in, obtains the Moodle tokens, stores them in the
system keyring when available, and writes an example configuration.
After this one-time login, syncMyMoodle should work without requiring re-sign-ins!

Setup is only for a new installation. To change an existing setup, locate and
edit its configuration instead:

```shell
syncmymoodle config path
syncmymoodle config check
```

If you change the account or RWTH sign-in settings, run the following command
afterwards so the stored Moodle tokens match the new configuration:

```shell
syncmymoodle auth login
```

## Everyday use

Running the command without a subcommand starts a sync:

```shell
# Sync using the saved configuration
syncmymoodle

# Show what would be downloaded without writing files or caches
syncmymoodle --dry-run

# Sync only selected course IDs or Moodle course URLs
syncmymoodle --courses 12345,67890

# Sync only one semester, and only download files up to 50 MB (when size is known)
syncmymoodle --semesters 25ws --max-file-size 50M

# Include diagnostic information
syncmymoodle --verbose

# Disable color explicitly (auto, always, and never are supported)
syncmymoodle --color never
```

Command-line sync options override the configuration for that run only. Use
`syncmymoodle --help` for the complete option list.

Interactive terminals show colored phases and prompts plus aggregate course,
item, and byte-transfer progress. Redirected output stays plain and reports
numbered course and periodic item milestones without animated progress;
`NO_COLOR` is also respected. Every sync ends with a summary of downloaded,
updated, unchanged, filtered, and failed items. A partial failure makes the
command exit non-zero after finishing the remaining work.

Boolean options have matching positive and negative forms, for example
`--update-files` and `--no-update-files`. An empty value clears a configured
comma-separated list for one run, i.e. `--courses ""`.


## Configuration

syncMyMoodle reads its global configuration from the platform's user config
directory. It does not auto-discover configuration files from the current
working directory. Use `config path` to see the exact locations on your system:

```shell
syncmymoodle config path
```

The packaged example is the authoritative reference for every setting and its
accepted values:

```shell
# Print the example without changing anything
syncmymoodle config example

# Start a configuration manually
syncmymoodle config example > config.toml
```

Validate the global configuration after editing it:

```shell
syncmymoodle config check
```

To select another configuration explicitly, put `--config` before the
subcommand:

```shell
syncmymoodle --config config.toml
syncmymoodle --config config.toml config check
syncmymoodle --config config.toml auth status
```

Relative paths in a configuration file resolve from that file's directory.
Relative paths passed on the command line resolve from the current working
directory.

### Courses and filters

The main course selectors are:

- `courses.selected`: sync only these course URLs or numeric IDs
- `courses.semesters`: sync courses from these semester IDs
- `courses.skip`: exclude these course URLs or numeric IDs
- `courses.exclude_roles`: exclude courses where a directly assigned Moodle
  course-role shortname matches, such as `tutor`

`courses.selected` takes priority over `semesters`, `skip`, and `exclude_roles`.
Role lookups are only made when `exclude_roles` is configured. If Moodle cannot
determine your role for a course, that course is kept.
Moodle's mobile API exposes only roles assigned directly in the course; roles
inherited from a course category or the system cannot be matched by this filter.

`exclude_sections` skips complete topic or week blocks. `exclude_modules` skips
individual activities or resources by name, type, ID, URL, or pattern. Both can
be a global list or a table keyed by Moodle course ID; use `*` in such a table
for rules shared by every course.

File, link, domain, type, and size filters are also available. Size limits
apply only when the remote size is known. Disabling `links.follow_links` also
disables the YouTube, Opencast, Sciebo, and emedia link handlers beneath it.

Intentional exclusions are summarized after each sync. Pass `--show-filtered`
to list them by configuration key and explain which rule matched. Combine it
with `--dry-run` to audit filters without writing downloads or caches:

```shell
syncmymoodle --dry-run --show-filtered
```

### Course directory names

`courses.prefix_handling` controls leading two-character course prefixes:

- `keep`: `(VO) Analysis`
- `remove`: `Analysis`
- `suffix`: `Analysis (VO)`

Setup-generated configurations use `suffix`. If the setting is absent, `keep`
remains the compatibility default. With `remove`, syncMyMoodle adds a stable
suffix when otherwise identical course names would collide.

### Updates and local changes

With `downloads.update_files = true`, syncMyMoodle replaces files when Moodle
or Sciebo reports a newer remote version. `downloads.conflict_handling`
determines what happens when the local file was also modified:

- `rename` moves the local version to a `.syncconflict.<hash>` file, then
  downloads the remote version
- `keep` leaves the local file unchanged and skips the update
- `overwrite` replaces the local file

`rename` is the default and protects local edits.

## Authentication and tokens

There are two categories of auth data:

| Data                 | Used for                                 | Configuration   |
|----------------------|------------------------------------------|-----------------|
| Moodle tokens        | Normal syncs and Moodle browser sessions | `[auth.tokens]` |
| RWTH sign-in secrets | Obtaining new Moodle tokens through SSO  | `[auth.login]`  |

The Moodle token record contains an API token and, when Moodle provides it, a
browser-login token. The local record is stored either in the system keyring or
in a protected environment file managed by syncMyMoodle.

In a desktop keyring viewer, look for service `syncmymoodle` and an entry named
like `mobile-tokens:moodle.rwth-aachen.de:<username>`. The exact grouping and
display name depend on the operating system's keyring backend.

A normal sync behaves as follows:

- Valid stored tokens are used without contacting RWTH SSO.
- With `auth.login.provider = "prompt"`, missing or invalid tokens stop the sync
  and direct you to `syncmymoodle auth login`.
- A reusable provider such as a password manager or protected sign-in file can
  sign in again and replace missing or invalid tokens automatically.
- Network or server failures leave token validity unknown and never trigger
  token replacement.

Automatic replacement makes at most one SSO attempt during a sync.

### Authentication commands

| Command                                  | Effect                                                                           |
|------------------------------------------|----------------------------------------------------------------------------------|
| `syncmymoodle auth status`               | Validates local tokens and reports the cached browser session without signing in |
| `syncmymoodle auth login`                | Performs one fresh SSO login and replaces the local token record                 |
| `syncmymoodle auth migrate --to keyring` | Copies tokens to another secure store and updates the configuration              |
| `syncmymoodle auth forget`               | Removes only this installation's tokens and cached browser session               |
| `syncmymoodle auth reset-token`          | Revokes and replaces the shared Moodle API token                                 |

`auth login` does not revoke the server token or log out another installation.
Use `auth login --totp-manual` to ignore the configured TOTP source and enter a
current code for that login only.

`auth migrate` leaves the previous token store untouched. To move to a protected
environment file, use `auth migrate --to env-file --env-file PATH`.

`auth forget` leaves the configuration, RWTH sign-in secrets, and shared server
token unchanged. If a reusable sign-in provider remains configured, a later
sync can obtain and store the local tokens again.

Use `auth reset-token` only when your account still has a legacy Moodle token
without the ability to mint Browser sessions, or you think your tokens may have been exposed.
Resetting the shared token logs out the Moodle app and every other
syncMyMoodle installation that uses it.

### Sign-in providers

`auth.login.provider` controls how RWTH SSO obtains the password and TOTP when
new Moodle tokens are needed. Supported choices are interactive prompts, the
system keyring, a protected environment file, 1Password, Bitwarden, pass, rbw,
gopass, and a custom command.

During setup, installed password-manager CLIs are detected without being run.
If selected, setup asks for provider-native password and optional TOTP
references, then verifies them during the initial login.
Password manager secrets are only requested when the tokens expire or are missing.

For headless systems, `auth.login.env_file` can point to a user-managed file:

```text
SYNCMYMOODLE_PASSWORD=...
SYNCMYMOODLE_TOTP_SECRET=...
```

This is separate from `auth.tokens.env_file`, which is managed by syncMyMoodle
and should not be edited manually. Both files are checked for safe permissions
before they are read.

In case your password manager cli is not supported, the `command` provider accepts
`password_command` and optional `otp_command` as argument arrays.
It does not invoke a shell and is accepted only from the default global configuration.

## Quizzes and linked content

Quiz attempts are saved as self-contained offline HTML by default. The
snapshot inlines same-origin assets and removes network-bearing content so it
does not contact Moodle when opened later. Set `modules.quiz` to `off`, `html`,
`pdf`, or `both`.

PDF output uses an installed Chrome, Chromium, or Edge browser in headless mode.
The browser is detected automatically on Linux, macOS, and Windows, or can be
set with `paths.browser`. If PDF rendering is unavailable, the HTML snapshot is
kept so the attempt is not lost.

Most downloads use the Moodle API token directly.
However, some content, like embedded Opencast, can't be accessed via this token.
Here syncMyMoodle uses the browser-login token to create a temporary browser session to access such files.
Moodle rate-limits that browser-login operation across devices, which may result in a temporary failure
to download some content. Simply wait a few minutes and try again.

## Cleanup and troubleshooting

Start with these read-only checks:

```shell
syncmymoodle config check
syncmymoodle auth status
syncmymoodle --dry-run --verbose
```

Cleanup commands are also dry runs unless `--apply` is passed:

```shell
# Preview or remove redundant conflict copies
syncmymoodle clean conflicts
syncmymoodle clean conflicts --apply

# Preview or reset per-course metadata caches
# Warning: this is a recovery command! Will fully rebuild the next sync
syncmymoodle clean caches
syncmymoodle clean caches --apply
```

They use `paths.sync_directory` by default. Pass `--path DIRECTORY` to inspect
another directory.

`clean conflicts` removes only `.syncconflict.*` files whose content duplicates
the current file or another conflict copy. `clean caches` removes per-course
`.syncmymoodle_cache` files; it is a recovery command and makes the next sync
rebuild its metadata.

## Migrating a legacy configuration

Legacy JSON configurations from versions prior to 1.0.0 are no longer supported.
Migrate them to the current TOML format with:

```shell
syncmymoodle config migrate --input config.json
```

Migration uses the old sign-in secrets for one login, stores the resulting
Moodle tokens separately, and writes a secret-free TOML configuration. The
generated file retains the example's comments and includes omitted example
settings only when they preserve the legacy configuration's behavior. It does
not modify the source JSON. Review both files after migration, then delete the
legacy JSON, especially if it contains secrets. The default token store is the
system keyring. For a headless system, use:

```shell
syncmymoodle config migrate --input config.json \
  --token-store env-file --token-env-file PATH
```

Use `--output` to select the TOML destination and `--force` to replace an
existing destination.

## Project information

- [Source code](https://github.com/Romern/syncMyMoodle)
- [Issue tracker](https://github.com/Romern/syncMyMoodle/issues)
- [Release documentation](https://github.com/Romern/syncMyMoodle/blob/master/docs/releasing.md)
- License: [GPL-3.0-only](https://github.com/Romern/syncMyMoodle/blob/master/LICENSE)
