<p align="center">
  <img src="https://raw.githubusercontent.com/Romern/syncMyMoodle/master/docs/assets/syncmymoodle-logo.png" alt="syncMyMoodle logo" width="280">
</p>

<h1 align="center">syncMyMoodle</h1>

<p align="center">Download and keep your RWTH Moodle course materials up to date.</p>

<p align="center">
  <a href="https://pypi.org/project/syncMyMoodle/"><img src="https://img.shields.io/pypi/v/syncmymoodle" alt="PyPI version"></a>
  <a href="https://pypi.org/project/syncMyMoodle/"><img src="https://img.shields.io/pypi/pyversions/syncmymoodle" alt="Supported Python versions"></a>
  <a href="https://github.com/Romern/syncMyMoodle/actions/workflows/test.yaml"><img src="https://github.com/Romern/syncMyMoodle/actions/workflows/test.yaml/badge.svg?branch=master" alt="Test status"></a>
  <a href="https://github.com/Romern/syncMyMoodle/blob/master/LICENSE"><img src="https://img.shields.io/github/license/Romern/syncMyMoodle" alt="License"></a>
</p>

syncMyMoodle is a command-line client that downloads your course materials from
[RWTH Moodle](https://moodle.rwth-aachen.de/) and keeps a local copy organized
and up to date.

Run it again whenever you want to download newly published material or update
files that changed on Moodle. Configurable conflict handling protects local
edits from being silently overwritten by remote updates.

Normal syncs use stored Moodle API tokens, so you do not need to sign in every
time. syncMyMoodle supports both interactive login and reusable credential
providers for obtaining new tokens when necessary.


> [!NOTE]
> This documentation targets syncMyMoodle 1.0.0. For the old 0.5.0 command-line
> interface and JSON configuration format, see the
> [syncMyMoodle 0.5.0 page on PyPI](https://pypi.org/project/syncMyMoodle/0.5.0/).

> [!IMPORTANT]
> syncMyMoodle is an independent project. It is not affiliated with, endorsed
> by, or supported by RWTH Aachen University, Moodle Pty Ltd, or the Moodle
> project.

## What it downloads

syncMyMoodle supports:

- Assignment attachments, your submissions, and feedback files
- Moodle file resources and folders
- Files exposed by books, pages, URL activities, and PDF annotator activities
- Supported links and embedded media discovered in assignments, folders,
  pages, labels, and H5P activities
- YouTube, RWTH Opencast, public Sciebo shares, and emedia Medizin VEIRA videos
- Opencast episodes and series exposed through supported LTI activities
- Quiz attempts as self-contained offline HTML, PDF, or both

It also provides:

- Course, semester, and course-role selection
- Course-specific and global section, module, link, and domain filters
- Filename, extension, and known-size filters
- Dry runs and explanations for filtered content
- Remote-update detection with configurable local-conflict handling
- Browser-assisted or terminal-based RWTH sign-in
- System-keyring and environment-file Moodle token stores
- Optional password-manager integrations for automatic TOTP sign-in recovery
- Migration from syncMyMoodle <1.0.0 JSON configurations

syncMyMoodle is a **one-way download client**. It does not upload local changes
to Moodle.

## Requirements

- Python 3.11 or newer
- Linux, macOS, or Windows
- An RWTH account with access to RWTH Moodle

Optional features have additional requirements:

- Quiz PDF generation needs Chrome, Chromium, or Microsoft Edge.
- Reliable YouTube downloads with yt-dlp may need
  [Deno](https://docs.deno.com/runtime/getting_started/installation/) 2.3.0 or
  newer.

## Installation

Installing syncMyMoodle as an isolated command-line tool is recommended.
Use either [uv](https://docs.astral.sh/uv/) or
[pipx](https://pipx.pypa.io/).

With uv:

```shell
uv tool install syncmymoodle
```

Or with pipx:

```shell
pipx install syncmymoodle
```

Verify the installation:

```shell
syncmymoodle --version
```

### Install from a source checkout

```shell
git clone https://github.com/Romern/syncMyMoodle.git
cd syncMyMoodle
python -m venv .venv
```

Activate the virtual environment:

```shell
# Linux or macOS
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install the project:

```shell
python -m pip install .
```

### Upgrading from <=0.5.0

Version 1.0 uses a new command-line interface and TOML configuration format.
Do not replace an existing <=0.5.0 installation until you have read
[Migrating from syncMyMoodle 0.5](https://github.com/Romern/syncMyMoodle/blob/master/docs/migrating-from-0-5.md).

## Quick start

Run setup once, then start the first sync.

| Setup mode       | Best for                                                           | Command                     |
|------------------|--------------------------------------------------------------------|-----------------------------|
| Browser-assisted | The simplest setup and supports any MFA method offered by RWTH SSO | `syncmymoodle setup`        |
| Terminal TOTP    | Terminal-only use and optional automatic sign-in                   | `syncmymoodle setup --totp` |

### Browser-assisted setup

```shell
syncmymoodle setup
```

Setup asks for your RWTH username, sync directory, and Moodle token store. It
does not request your RWTH password or TOTP information.

The command opens an RWTH/Moodle sign-in page in your browser. After signing in, Moodle shows a
blue app-launch link:

1. Right-click the blue link.
2. Copy its complete link address.
3. Paste it into the hidden syncMyMoodle prompt.
4. Confirm the Moodle account before the tokens are stored.

> [!CAUTION]
> The app-launch address contains your Moodle tokens. Do not share, save,
> publish, or paste it anywhere except the syncMyMoodle prompt.

Browser setup remains interactive when replacement tokens are needed. Run
`syncmymoodle auth login` to sign in again.

### Terminal TOTP setup

```shell
syncmymoodle setup --totp
```

TOTP setup asks for:

- Your RWTH Single Sign-On username
- Your RWTH TOTP serial, such as `TOTP12345678`
- The directory where course content should be downloaded
- An optional detected password-manager provider
- The Moodle token store

The TOTP serial is the identifier shown in the
[RWTH IDM Token Manager](https://idm.rwth-aachen.de/selfservice/MFATokenManager),
not the current six-digit code.

The default `prompt` provider asks for your RWTH password and current TOTP code
during the initial login. A reusable provider can instead obtain the password
and optionally generate or retrieve future TOTP codes when stored Moodle tokens
need replacement.


### Start syncing

After setup completes:

```shell
syncmymoodle
```

Running `syncmymoodle` without a subcommand always starts a sync using the
selected configuration.

A setup-generated configuration downloads into the directory you selected,
enables remote-file updates, preserves local conflicts by renaming them, and
saves quiz attempts as offline HTML.

## Common commands

```shell
# Sync using the saved configuration
syncmymoodle

# Preview the sync without writing files or metadata caches
syncmymoodle --dry-run

# Sync only selected course IDs or Moodle course URLs
syncmymoodle --courses 12345,67890

# Sync courses from one semester
syncmymoodle --semesters 25ws

# Skip files larger than 50 MiB when the size is known
syncmymoodle --max-file-size 50M

# Preview the sync and explain every configured exclusion
syncmymoodle --dry-run --show-filtered

# Include diagnostic logging
syncmymoodle --verbose

# Disable colored output
syncmymoodle --color never
```

Command-line sync options override the configuration for that run only. Boolean
settings have matching positive and negative forms:

```shell
syncmymoodle --update-files
syncmymoodle --no-update-files
```

An empty value clears a configured comma-separated list for one run:

```shell
syncmymoodle --courses ""
```

Use `syncmymoodle --help` for every sync option and see the
[CLI reference](https://github.com/Romern/syncMyMoodle/blob/master/docs/cli-reference.md) for all subcommands.

## Updates and local changes

When `downloads.update_files = true`, syncMyMoodle checks whether previously
seen remote files have changed. Depending on the source, it uses Moodle content
hashes, modification timestamps, HTTP validators, or cached source metadata.

If a remote file changed and the local file still matches the previously synced
copy, syncMyMoodle updates it directly. If the local file also changed,
`downloads.conflict_handling` determines the result:

| Mode        | Behavior                                                                             |
|-------------|--------------------------------------------------------------------------------------|
| `rename`    | Move the local version to a `.syncconflict...` copy, then install the remote version |
| `keep`      | Keep the local version and skip the remote update                                    |
| `overwrite` | Replace the local version                                                            |

`rename` is the default and is used by setup-generated configurations.

> [!WARNING]
> `overwrite` can permanently discard local changes. Use it only when the sync
> directory is not edited manually or is backed up elsewhere.

syncMyMoodle stages downloads before installing them and prevents two writing
syncs from using the same sync directory concurrently. More detail is available
in [How synchronization works](https://github.com/Romern/syncMyMoodle/blob/master/docs/how-sync-works.md).

## Configuration

The global configuration is stored in the platform-specific user configuration
directory. syncMyMoodle does not search the current working directory for a
configuration file.

Show the global location:

```shell
syncmymoodle config path
```

Print the complete commented example:

```shell
syncmymoodle config example
```

Validate the global configuration after editing it:

```shell
syncmymoodle config check
```

Use another configuration by placing `--config` before the subcommand or sync
arguments:

```shell
syncmymoodle --config config.toml
syncmymoodle --config config.toml config check
syncmymoodle --config config.toml auth status
```

Relative paths in a configuration file resolve from that file's directory.
Relative paths supplied on the command line resolve from the current working
directory.

See the [complete configuration reference](https://github.com/Romern/syncMyMoodle/blob/master/docs/configuration.md) for every
setting, accepted value, default, filter rule, and interaction.

## Authentication and tokens

syncMyMoodle separates normal Moodle access from RWTH sign-in:

| Data                       | Purpose                                                        | Configuration   |
|----------------------------|----------------------------------------------------------------|-----------------|
| Moodle token record        | Normal syncs and creation of temporary Moodle browser sessions | `[auth.tokens]` |
| RWTH sign-in configuration | Obtaining replacement Moodle tokens through RWTH SSO           | `[auth.login]`  |

The Moodle token record contains an API token and, when Moodle supplies it, a
browser-login token. It can be stored in the system keyring or in a private
environment file managed by syncMyMoodle.

Start authentication diagnostics with:

```shell
syncmymoodle auth status
```

A nonzero status means the stored token state is missing, invalid, unavailable,
or insufficient for enabled browser-session features.

See the [authentication reference](https://github.com/Romern/syncMyMoodle/blob/master/docs/authentication.md) for token lifecycle,
sign-in providers, environment files, password-manager references, and all
authentication commands.

## Output and exit status

Interactive terminals show phases and aggregate course, item, and transfer
progress. Redirected output uses plain numbered milestones instead of animated
progress. The [`NO_COLOR`](https://no-color.org/) convention is respected.

Every run ends with a summary of courses and downloaded, updated, unchanged,
filtered, planned, or failed items as applicable.

A failure in one course, module, or download usually does not stop the entire
sync. syncMyMoodle continues with the remaining work and exits with status `1`
afterwards. Invalid command usage exits with status `2`; interruption with
Ctrl+C exits with status `130`.

## Troubleshooting and cleanup

Begin with read-only diagnostics:

```shell
syncmymoodle config check
syncmymoodle auth status
syncmymoodle --dry-run --verbose
syncmymoodle --dry-run --show-filtered
```

Cleanup commands are previews unless `--apply` is supplied:

```shell
# Find redundant local conflict copies
syncmymoodle clean conflicts

# Find per-course metadata caches
syncmymoodle clean caches
```

See [Cleanup and troubleshooting](https://github.com/Romern/syncMyMoodle/blob/master/docs/cleanup-and-troubleshooting.md) before
applying either cleanup operation.

## Documentation

- [Documentation index](https://github.com/Romern/syncMyMoodle/blob/master/docs/README.md)
- [Getting started](https://github.com/Romern/syncMyMoodle/blob/master/docs/getting-started.md)
- [Everyday recipes](https://github.com/Romern/syncMyMoodle/blob/master/docs/everyday-recipes.md)
- [How synchronization works](https://github.com/Romern/syncMyMoodle/blob/master/docs/how-sync-works.md)
- [CLI reference](https://github.com/Romern/syncMyMoodle/blob/master/docs/cli-reference.md)
- [Configuration reference](https://github.com/Romern/syncMyMoodle/blob/master/docs/configuration.md)
- [Authentication reference](https://github.com/Romern/syncMyMoodle/blob/master/docs/authentication.md)
- [Supported content and linked services](https://github.com/Romern/syncMyMoodle/blob/master/docs/quizzes-and-linked-content.md)
- [Cleanup and troubleshooting](https://github.com/Romern/syncMyMoodle/blob/master/docs/cleanup-and-troubleshooting.md)
- [Migrating from 0.5](https://github.com/Romern/syncMyMoodle/blob/master/docs/migrating-from-0-5.md)

## Reporting problems

Use the [GitHub issue tracker](https://github.com/Romern/syncMyMoodle/issues)
for reproducible bugs and feature requests.

Include:

- Operating system
- Python version
- syncMyMoodle version
- The command that was run
- The relevant error message
- Whether `syncmymoodle config check` succeeds
- Whether `syncmymoodle auth status` succeeds

Before posting logs, screenshots, configuration excerpts, or command output,
remove:

- RWTH passwords
- TOTP seeds and current TOTP codes
- Moodle API and browser-login tokens
- Moodle app-launch addresses
- Environment-file contents
- Password-manager secret values
- Private course or account information

## Project information

- [Source code](https://github.com/Romern/syncMyMoodle)
- [Issue tracker](https://github.com/Romern/syncMyMoodle/issues)
- [Releases](https://github.com/Romern/syncMyMoodle/releases)
- [Maintainer release process](https://github.com/Romern/syncMyMoodle/blob/master/docs/releasing.md)
- License: [GPL-3.0-only](LICENSE)
