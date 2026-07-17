<p align="center"> <img src="https://raw.githubusercontent.com/Romern/syncMyMoodle/30a29fdcf7206713e49bdd47f1d0dee1a8887294/docs/assets/syncmymoodle-logo.png" alt="syncMyMoodle logo" width="280" > </p>

<h1 align="center">syncMyMoodle</h1>

<p align="center"> Download and keep your RWTH Moodle course materials up to date. </p>

<p align="center"> <a href="https://pypi.org/project/syncMyMoodle/"> <img src="https://img.shields.io/pypi/v/syncmymoodle" alt="PyPI version"> </a> <a href="https://pypi.org/project/syncMyMoodle/"> <img src="https://img.shields.io/pypi/pyversions/syncmymoodle" alt="Supported Python versions"> </a> <a href="https://github.com/Romern/syncMyMoodle/actions/workflows/test.yaml"> <img src="https://github.com/Romern/syncMyMoodle/actions/workflows/test.yaml/badge.svg?branch=master" alt="Test status"> </a> <a href="https://github.com/Romern/syncMyMoodle/blob/master/LICENSE"> <img src="https://img.shields.io/github/license/Romern/syncMyMoodle" alt="License"> </a> </p>

syncMyMoodle is a command-line client that downloads course content from
[RWTH Moodle](https://moodle.rwth-aachen.de/) into a local directory.

Run it again later to add new materials and update files that changed remotely.
Configurable conflict handling prevents remote updates from silently
overwriting local edits.

Normal syncs use locally stored Moodle tokens and do not require your RWTH
password or TOTP code.

> [!NOTE]
> This README documents syncMyMoodle >= 1.0.0. For version 0.5.0 and its documentation, see the
> [syncMyMoodle 0.5.0 page on PyPI](https://pypi.org/project/syncMyMoodle/0.5.0/).

> [!IMPORTANT]
> syncMyMoodle is an independent project. It is not affiliated with, endorsed
> by, or supported by RWTH Aachen University, Moodle Pty Ltd, or the Moodle
> project.

## Features

syncMyMoodle can download:

* Assignments, submissions, and feedback
* File resources and Moodle folders
* Pages and labels, including linked files and embedded media
* Opencast, YouTube, Sciebo, and emedia Medizin VEIRA content
* Quiz attempts as self-contained offline HTML
* Quiz attempts as PDF using Chrome, Chromium, or Microsoft Edge
* H5P packages and supported LTI content

It also supports:

* Course and semester selection
* Course, section, module, file, link, domain, type, and size filters
* Dry runs and explanations for filtered content
* Remote file updates with configurable conflict handling
* Browser-assisted or TOTP-based RWTH sign-in
* System-keyring and environment-file Moodle token stores
* Password-manager integrations for obtaining RWTH sign-in credentials
* Configuration migration from syncMyMoodle 0.5.0 and earlier

syncMyMoodle is a one-way download client. It does not upload local changes to
Moodle.

## Requirements

* Python 3.11 or newer
* Linux, macOS, or Windows
* An RWTH account with access to RWTH Moodle

Quiz PDF generation additionally requires an installed Chrome, Chromium, or
Microsoft Edge browser.

## Installation

Installing syncMyMoodle as an isolated command-line tool is recommended. Use
either [uv](https://docs.astral.sh/uv/) or
[pipx](https://pipx.pypa.io/).

Using uv:

```shell
uv tool install syncmymoodle
```

Alternatively, using pipx:

```shell
pipx install syncmymoodle
```

Only one of these commands is needed.

Verify the installation:

```shell
syncmymoodle --version
```

### Install from a source checkout

Clone the repository and create a virtual environment:

```shell
git clone https://github.com/Romern/syncMyMoodle.git
cd syncMyMoodle
python -m venv .venv
```

Activate the environment:

```shell
# Linux or macOS
source .venv/bin/activate
```

```powershell
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install syncMyMoodle:

```shell
python -m pip install .
```

### Existing <= 0.5.0 installations

Starting with version 1.0.0, syncMyMoodle uses a different command-line interface and configuration
format. Read [Migrating from 0.5.0](#migrating-from-05) before replacing an
existing installation.

The documentation for the 0.5.0 release remains available on its
[PyPI project page](https://pypi.org/project/syncMyMoodle/0.5.0/).

## Quick start

Run the interactive setup once, then start your first sync.

Choose the setup method that matches how you sign in to RWTH:

| Sign-in method | Use it when                                                                             | Command                        |
|----------------|-----------------------------------------------------------------------------------------|--------------------------------|
| TOTP           | You sign in with an RWTH password and TOTP token                                        | `syncmymoodle setup`           |
| Browser        | You use a passkey, security key, or another MFA method supported by the RWTH login page | `syncmymoodle setup --browser` |

### TOTP setup

```shell
syncmymoodle setup
```

Setup asks for:

* Your RWTH Single Sign-On username
* Your RWTH TOTP serial, such as `TOTP12345678`
* The directory where Moodle files should be downloaded
* How RWTH sign-in credentials should be obtained when new Moodle tokens are
  needed
* Where the Moodle tokens should be stored

The TOTP serial is the identifier shown in the
[RWTH IDM Token Manager](https://idm.rwth-aachen.de/selfservice/MFATokenManager).

Depending on the selected sign-in provider, setup may prompt for your RWTH
password, TOTP secret, or password-manager references. These are used to
complete the initial RWTH sign-in and, when configured through a reusable
provider, to obtain new Moodle tokens later.

### Browser setup

Use browser setup for passkeys, security keys, or other MFA methods handled by
the RWTH login page:

```shell
syncmymoodle setup --browser
```

Browser setup asks for your username, sync directory, and Moodle token store.
It does not request your RWTH password or TOTP details.

The command opens an RWTH/Moodle sign-in link. After signing in, Moodle will
display a blue link:

1. Right-click the blue link.
2. Copy its complete link address.
3. Paste the address into the hidden syncMyMoodle prompt.

> [!CAUTION]
> The app-link address contains your Moodle tokens. Do not share, save, publish,
> or paste it anywhere except the syncMyMoodle prompt.

A setup created with `--browser` saves browser-assisted sign-in as the default
login method. A later `syncmymoodle auth login` will therefore use the browser
again.

### Start the first sync

After setup completes:

```shell
syncmymoodle
```

Running the command without a subcommand always starts a sync using the saved
configuration.

Setup is intended for new installations. To change an existing setup, edit the
configuration instead:

```shell
syncmymoodle config path
syncmymoodle config check
```

If you change the account or RWTH sign-in settings, obtain a new matching
Moodle token record afterwards:

```shell
syncmymoodle auth login
```

## Everyday use

```shell
# Sync using the saved configuration
syncmymoodle

# Preview the sync without writing files or caches
syncmymoodle --dry-run

# Sync selected course IDs or Moodle course URLs
syncmymoodle --courses 12345,67890

# Sync courses from one semester
syncmymoodle --semesters 25ws

# Ignore files larger than 50 MB when the remote size is known
syncmymoodle --max-file-size 50M

# Preview the sync and explain configured exclusions
syncmymoodle --dry-run --show-filtered

# Include diagnostic information
syncmymoodle --verbose

# Disable colored output
syncmymoodle --color never
```

Command-line sync options override the saved configuration for that run only.

Use the built-in help for the complete option list:

```shell
syncmymoodle --help
```

Boolean settings have matching positive and negative options. For example:

```shell
syncmymoodle --update-files
syncmymoodle --no-update-files
```

An empty command-line value clears a configured comma-separated list for one
run:

```shell
syncmymoodle --courses ""
```

## Output and exit status

Interactive terminals show colored phases, prompts, and aggregate course, item,
and byte-transfer progress.

When output is redirected, syncMyMoodle disables animated progress and uses
plain numbered course and item milestones. The
[`NO_COLOR`](https://no-color.org/) convention is also respected.

Every sync ends with a summary of downloaded, updated, unchanged, filtered, and
failed items.

A course, module, or download failure does not immediately stop the complete
sync. syncMyMoodle finishes the remaining work and exits with a non-zero status
afterwards.

## Configuration

syncMyMoodle reads its global configuration from the platform-specific user
configuration directory. It does not automatically discover configuration
files in the current working directory.

Show the global configuration location:

```shell
syncmymoodle config path
```

Print the complete commented example:

```shell
syncmymoodle config example
```

The packaged example is the authoritative reference for every setting and its
accepted values.

To create a configuration manually:

```shell
syncmymoodle config example > config.toml
```

Validate the global configuration after editing it:

```shell
syncmymoodle config check
```

Select another configuration explicitly by placing `--config` before the
subcommand:

```shell
syncmymoodle --config config.toml
syncmymoodle --config config.toml config check
syncmymoodle --config config.toml auth status
```

Relative paths inside a configuration file resolve from that file's directory.
Relative paths supplied on the command line resolve from the current working
directory.

### Course selection

The primary course selectors are:

| Setting                 | Effect                                                                            |
| ----------------------- | --------------------------------------------------------------------------------- |
| `courses.selected`      | Sync only the listed course URLs or numeric IDs                                   |
| `courses.semesters`     | Sync courses belonging to the listed semester IDs                                 |
| `courses.skip`          | Exclude the listed course URLs or numeric IDs                                     |
| `courses.exclude_roles` | Exclude courses where your directly assigned Moodle course-role shortname matches |

`courses.selected` takes priority over `courses.semesters`, `courses.skip`, and
`courses.exclude_roles`.

Role lookups are performed only when `courses.exclude_roles` is configured. If
Moodle cannot determine your role for a course, the course is kept.

The Moodle mobile API exposes only roles assigned directly in a course. Roles
inherited from a course category or the Moodle system cannot be matched by
this filter.

### Sections, modules, and files

`exclude_sections` skips complete Moodle topic or week blocks, including
everything inside them.

`exclude_modules` skips individual activities or resources. Rules can match
module names, Moodle types, IDs, URLs, or patterns.

Both settings can be either:

* A global list
* A table keyed by Moodle course ID

Use `*` in a per-course table for rules shared by every course.

Additional filters are available for:

* Filenames and paths
* File extensions and Moodle file types
* Links and domains
* Remote file size
* Moodle module types
* Individual courses and course roles

Size limits apply only when Moodle or the linked service reports the remote
size.

Disabling `links.follow_links` also disables the linked-content handlers below
it, including YouTube, Opencast, Sciebo, and emedia.

Intentional exclusions are included in the final sync summary. To list each
excluded item and the matching configuration rule, run:

```shell
syncmymoodle --dry-run --show-filtered
```

### Course directory names

`courses.prefix_handling` controls leading two-character course prefixes:

| Value    | Moodle course name | Local directory |
| -------- | ------------------ | --------------- |
| `keep`   | `(VO) Analysis`    | `(VO) Analysis` |
| `remove` | `(VO) Analysis`    | `Analysis`      |
| `suffix` | `(VO) Analysis`    | `Analysis (VO)` |

Setup-generated configurations use `suffix`.

When the setting is absent, `keep` remains the compatibility default. With
`remove`, syncMyMoodle adds a stable suffix when otherwise identical directory
names would collide.

## Updates and local changes

With `downloads.update_files = true`, syncMyMoodle replaces a file when Moodle
or Sciebo reports a newer remote version.

If the local file was also modified, `downloads.conflict_handling` determines
what happens:

| Mode        | Behavior                                                                                  |
| ----------- | ----------------------------------------------------------------------------------------- |
| `rename`    | Move the local version to a `.syncconflict.<hash>` file, then download the remote version |
| `keep`      | Leave the local file unchanged and skip the update                                        |
| `overwrite` | Replace the local file with the remote version                                            |

`rename` is the default. It preserves the local version while allowing the
newer remote file to be downloaded.

> [!WARNING]
> `overwrite` can permanently discard local changes. Use it only when files in
> the sync directory are not edited manually or are backed up elsewhere.

## Authentication and tokens

syncMyMoodle distinguishes between two categories of authentication data:

| Data                     | Used for                                           | Configuration   |
| ------------------------ | -------------------------------------------------- | --------------- |
| Moodle tokens            | Normal syncs and temporary Moodle browser sessions | `[auth.tokens]` |
| RWTH sign-in credentials | Obtaining new Moodle tokens through RWTH SSO       | `[auth.login]`  |

The Moodle token record contains an API token and, when Moodle provides one, a
browser-login token.

The token record can be stored in:

* The system keyring
* An environment file managed by syncMyMoodle

The system keyring is preferred when a working backend is available.

In a desktop keyring viewer, look for the service `syncmymoodle` and an entry
similar to:

```text
mobile-tokens:moodle.rwth-aachen.de:<username>
```

The grouping and display name depend on the operating system and keyring
backend.

### Authentication during a sync

A normal sync follows these rules:

* Valid stored Moodle tokens are used without contacting RWTH SSO.
* Missing or invalid tokens require a new RWTH sign-in.
* With `auth.login.method = "browser"`, the sync stops and directs you to the
  browser-assisted `syncmymoodle auth login` flow.
* With `auth.login.provider = "prompt"`, the sync stops and directs you to
  `syncmymoodle auth login`.
* A reusable sign-in provider can obtain replacement Moodle tokens
  automatically.
* Network and server errors do not cause token replacement because token
  validity cannot be determined reliably.

Automatic token replacement makes at most one RWTH SSO attempt during a sync.

### Authentication commands

| Command                                                   | Effect                                                                                 |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------- |
| `syncmymoodle auth status`                                | Validate stored Moodle tokens and report the cached browser session without signing in |
| `syncmymoodle auth login`                                 | Perform one fresh RWTH sign-in and replace the local Moodle token record               |
| `syncmymoodle auth login --browser`                       | Perform a one-off browser-assisted sign-in                                             |
| `syncmymoodle auth login --totp-manual`                   | Ignore the configured TOTP source and prompt for a current code for this login         |
| `syncmymoodle auth migrate --to keyring`                  | Copy the Moodle tokens to the system keyring and update the configuration              |
| `syncmymoodle auth migrate --to env-file --env-file PATH` | Copy the Moodle tokens to an environment file and update the configuration             |
| `syncmymoodle auth forget`                                | Remove this installation's tokens and cached browser session                           |
| `syncmymoodle auth reset-token`                           | Revoke and replace the shared Moodle API token                                         |

`auth login` replaces only this installation's local token record. It does not
revoke the shared Moodle API token or log out other installations.

New tokens are accepted only after Moodle confirms that they belong to the
same account as an existing token record.

`auth migrate` leaves the previous token store untouched.

`auth forget` leaves the configuration, configured RWTH sign-in credentials,
and shared server token unchanged. When a reusable sign-in provider remains
configured, a later sync may obtain and store new local Moodle tokens again.

> [!CAUTION]
> `auth reset-token` revokes the shared Moodle API token. This logs out the
> Moodle mobile app and every other syncMyMoodle installation using that token.
>
> Use it only when recovering from a legacy token that cannot create browser
> sessions or when the token may have been exposed.

### Sign-in methods and providers

`auth.login.method` selects the RWTH sign-in flow:

* `totp`
* `browser`

With the TOTP method, `auth.login.provider` controls how syncMyMoodle obtains
the RWTH password and TOTP information when new Moodle tokens are needed.

Supported providers include:

* Interactive prompts
* The system keyring
* An environment file
* 1Password
* Bitwarden
* pass
* rbw
* gopass
* A custom command

Browser-assisted sign-in does not use the TOTP sign-in providers.

During setup, syncMyMoodle detects installed password-manager CLIs without
executing them. If you select one, setup asks for provider-native references
and verifies them during the initial sign-in. The referenced secrets are
requested only when new Moodle tokens are needed.

For headless systems, `auth.login.env_file` can point to a user-managed
environment file containing:

```text
SYNCMYMOODLE_PASSWORD=...
SYNCMYMOODLE_TOTP_SECRET=...
```

This is separate from `auth.tokens.env_file`, which stores Moodle tokens and
is managed by syncMyMoodle. Do not edit the Moodle token environment file
manually.

The `command` provider accepts `password_command` and an optional
`otp_command` as argument arrays. It does not invoke a shell and is accepted
only from the default global configuration.

## Quizzes and linked content

Quiz attempts are saved as self-contained offline HTML by default.

The snapshot inlines supported same-origin assets and removes network-bearing
content so it does not contact Moodle when opened later.

Set `modules.quiz` to one of:

| Value  | Output                     |
| ------ | -------------------------- |
| `off`  | Do not save quiz attempts  |
| `html` | Save self-contained HTML   |
| `pdf`  | Render a PDF               |
| `both` | Save HTML and render a PDF |

PDF output uses an installed Chrome, Chromium, or Microsoft Edge browser in
headless mode.

The browser is detected automatically on Linux, macOS, and Windows. A specific
browser executable can be configured with `paths.browser`.

If PDF rendering is unavailable, syncMyMoodle keeps the HTML snapshot so the
attempt is not lost.

Most content is downloaded directly through the Moodle API. Some content, such
as embedded Opencast resources, requires a temporary Moodle browser session
created with the browser-login token.

Moodle rate-limits creation of this browser session across devices. If that
causes a temporary download failure, wait a few minutes and retry the sync.

## Cleanup and troubleshooting

Start with these read-only checks:

```shell
syncmymoodle config check
syncmymoodle auth status
syncmymoodle --dry-run --verbose
```

To inspect configured exclusions:

```shell
syncmymoodle --dry-run --show-filtered
```

Cleanup commands are previews unless `--apply` is supplied.

### Redundant conflict copies

Preview redundant `.syncconflict.*` files:

```shell
syncmymoodle clean conflicts
```

Delete only the files listed as redundant:

```shell
syncmymoodle clean conflicts --apply
```

A conflict copy is considered redundant only when its content duplicates the
current file or another conflict copy.

### Course metadata caches

Preview a reset of per-course metadata caches:

```shell
syncmymoodle clean caches
```

Delete the listed caches:

```shell
syncmymoodle clean caches --apply
```

This is a recovery operation. The next sync rebuilds the metadata caches and
may perform additional work.

Both cleanup commands use `paths.sync_directory` by default. To inspect another
directory:

```shell
syncmymoodle clean conflicts --path DIRECTORY
syncmymoodle clean caches --path DIRECTORY
```

## Migrating from 0.5

Configurations from versions before 1.0.0 use a legacy JSON format which is no longer supported.

You can migrate a JSON configuration to TOML using:

```shell
syncmymoodle config migrate --input config.json
```

Migration:

* Uses the legacy sign-in credentials for one RWTH login
* Obtains and stores a new Moodle token record
* Writes a TOML configuration without embedding the legacy sign-in secrets
* Retains comments from the packaged example
* Preserves omitted example settings when needed to reproduce the legacy
  behavior
* Leaves the source JSON file unchanged

Review the original and generated configuration files after migration. Delete
the legacy JSON file once the new setup works, especially when the JSON file
contains an RWTH password or TOTP secret.

The default destination for migrated Moodle tokens is the system keyring.

For a headless system:

```shell
syncmymoodle config migrate --input config.json \
  --token-store env-file \
  --token-env-file PATH
```

Use `--output` to select the TOML destination and `--force` to replace an
existing destination:

```shell
syncmymoodle config migrate \
  --input config.json \
  --output config.toml \
  --force
```

For the old configuration format and 0.5 command-line interface, see the
[syncMyMoodle 0.5.0 documentation on PyPI](https://pypi.org/project/syncMyMoodle/0.5.0/).

## Reporting problems

Use the [GitHub issue tracker](https://github.com/Romern/syncMyMoodle/issues)
for reproducible bugs and feature requests.

A useful bug report includes:

* Operating system
* Python version
* syncMyMoodle version
* The command that was run
* The relevant error message
* Whether `syncmymoodle config check` succeeds
* Whether `syncmymoodle auth status` succeeds

Before posting logs, screenshots, configuration excerpts, or command output,
remove:

* RWTH passwords
* TOTP secrets and current TOTP codes
* Moodle API and browser-login tokens
* Moodle app-link addresses
* Environment-file contents
* Password-manager secret values
* Private course or account information

## Project information

* [Source code](https://github.com/Romern/syncMyMoodle)
* [Issue tracker](https://github.com/Romern/syncMyMoodle/issues)
* [Releases](https://github.com/Romern/syncMyMoodle/releases)
* [Release documentation](docs/releasing.md)
* License: [GPL-3.0-only](LICENSE)

## License

syncMyMoodle is licensed under the
[GNU General Public License v3.0 only](LICENSE).
