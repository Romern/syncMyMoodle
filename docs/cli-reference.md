# Command-line reference

This page documents the syncMyMoodle 1.0.0 command-line interface.

Use the installed program's built-in help as the final authority for the exact
version you are running:

```shell
syncmymoodle --help
syncmymoodle COMMAND --help
```

## Command structure

```text
syncmymoodle [GLOBAL-AND-SYNC-OPTIONS]
syncmymoodle [--config FILE] config COMMAND [OPTIONS]
syncmymoodle [--config FILE] auth COMMAND [OPTIONS]
syncmymoodle [--config FILE] clean COMMAND [OPTIONS]
syncmymoodle setup [--browser | --totp]
```

Running the command without a subcommand starts a sync.

`--config` is a top-level option and must be placed before a subcommand:

```shell
syncmymoodle --config config.toml auth status
```

Sync-only options cannot be combined with `config`, `auth`, `clean`, or `setup`.
`setup` always creates the global configuration and does not accept `--config`.
Among the `config` subcommands, an alternate `--config` is useful for
`config check`; `config path`, `config example`, and `config migrate` operate on
their own explicitly defined locations.

## Top-level options

| Option          | Purpose                                                       |
|-----------------|---------------------------------------------------------------|
| `--config FILE` | Use an explicit TOML configuration instead of the global file |
| `--version`     | Print the installed syncMyMoodle version and exit             |
| `-h`, `--help`  | Show command help and exit                                    |

## Sync options

These options apply when no subcommand is present. They override the loaded
configuration for the current run only.

### Authentication overrides

| Option                           | Configuration equivalent                                      | Description                                                                 |
|----------------------------------|---------------------------------------------------------------|-----------------------------------------------------------------------------|
| `--user USER`                    | `auth.user`                                                   | RWTH SSO username                                                           |
| `--totp-serial SERIAL`           | `auth.login.totp_serial`                                      | RWTH TOTP token identifier, such as `TOTP12345678`                          |
| `--keyring-store-totp-secret`    | `auth.login.keyring_store_totp_secret = true`                 | Use a keyring TOTP seed, prompting and storing it if missing                |
| `--no-keyring-store-totp-secret` | `auth.login.keyring_store_totp_secret = false`                | Do not use a keyring TOTP seed for this run; existing seeds are not deleted |
| `--login-env-file FILE`          | `auth.login.provider = "env-file"` plus `auth.login.env_file` | Use a user-managed environment file for RWTH password and TOTP seed         |

These options affect sign-in only when a new Moodle token is required. Normal
runs continue to use the stored Moodle token record.

### Path overrides

| Option                       | Configuration equivalent | Description                                             |
|------------------------------|--------------------------|---------------------------------------------------------|
| `--sync-directory DIRECTORY` | `paths.sync_directory`   | Destination root for downloaded course content          |
| `--cookie-file FILE`         | `paths.cookie_file`      | Private cached browser-session file                     |
| `--browser FILE`             | `paths.browser`          | Chrome, Chromium, or Edge executable used for quiz PDFs |

Relative command-line paths resolve from the current working directory.

### Course selection

| Option                                          | Configuration equivalent  | Description                                                                            |
|-------------------------------------------------|---------------------------|----------------------------------------------------------------------------------------|
| `--courses LIST`                                | `courses.selected`        | Comma-separated course IDs or Moodle course URLs; overrides the other course selectors |
| `--skip-courses LIST`                           | `courses.skip`            | Comma-separated course IDs or URLs to exclude                                          |
| `--exclude-course-roles LIST`                   | `courses.exclude_roles`   | Comma-separated directly assigned Moodle role shortnames to exclude                    |
| `--semesters LIST`                              | `courses.semesters`       | Comma-separated semester IDs such as `25ws` or `26ss`                                  |
| `--course-prefix-handling {keep,remove,suffix}` | `courses.prefix_handling` | Transform a leading course prefix such as `(VO)` in local directory names              |

An empty comma-separated value clears the corresponding configured list for one
run:

```shell
syncmymoodle --courses ""
```

### Download and update policy

| Option                                        | Configuration equivalent         | Description                                                                 |
|-----------------------------------------------|----------------------------------|-----------------------------------------------------------------------------|
| `--update-files`                              | `downloads.update_files = true`  | Replace previously downloaded files when their remote source changed        |
| `--no-update-files`                           | `downloads.update_files = false` | Keep existing targets without remote-update replacement                     |
| `--conflict-handling {rename,keep,overwrite}` | `downloads.conflict_handling`    | Select behavior when a remote update and a local edit conflict              |
| `--dry-run`                                   | `downloads.dry_run = true`       | Discover and report planned work without writing downloads or course caches |
| `--no-dry-run`                                | `downloads.dry_run = false`      | Disable a configured dry run for this invocation                            |

### File and content filters

| Option                     | Configuration equivalent    | Description                                                             |
|----------------------------|-----------------------------|-------------------------------------------------------------------------|
| `--allowed-domains LIST`   | `filters.allowed_domains`   | Comma-separated allowlist for discovered HTTP(S) links                  |
| `--max-file-size SIZE`     | `filters.max_file_size`     | Skip known-size files larger than the limit                             |
| `--min-file-size SIZE`     | `filters.min_file_size`     | Skip known-size files smaller than the limit                            |
| `--exclude-filetypes LIST` | `filters.exclude_filetypes` | Comma-separated final filename extensions                               |
| `--exclude-files LIST`     | `filters.exclude_files`     | Comma-separated case-sensitive shell patterns matched against basenames |
| `--exclude-links LIST`     | `filters.exclude_links`     | Comma-separated case-sensitive shell patterns for discovered URLs       |
| `--exclude-sections LIST`  | `filters.exclude_sections`  | Comma-separated section names, IDs, or patterns                         |
| `--exclude-modules LIST`   | `filters.exclude_modules`   | Comma-separated module names, types, IDs, URLs, or patterns             |

Size values accept integer bytes or binary suffixes such as `10K`, `50M`, `2G`,
and `1T`; optional `B` and `iB` forms are accepted. Limits apply only when the
remote size is known or can be estimated.

Command-line pattern lists are global. Use TOML table form for course-specific
rules.

### Linked sources

| Option                                 | Configuration equivalent | Description                                                                 |
|----------------------------------------|--------------------------|-----------------------------------------------------------------------------|
| `--follow-links` / `--no-follow-links` | `links.follow_links`     | Enable or disable all linked-content discovery                              |
| `--youtube` / `--no-youtube`           | `links.youtube`          | Enable or disable YouTube links and embeds                                  |
| `--opencast` / `--no-opencast`         | `links.opencast`         | Enable or disable RWTH Opencast links, embeds, and supported LTI activities |
| `--sciebo` / `--no-sciebo`             | `links.sciebo`           | Enable or disable public Sciebo share downloads                             |
| `--emedia` / `--no-emedia`             | `links.emedia`           | Enable or disable emedia Medizin VEIRA videos                               |

Turning off `follow-links` disables every source-specific linked-content handler,
even if an individual source switch remains true.

### Quiz output

| Option                       | Configuration equivalent | Description                                                      |
|------------------------------|--------------------------|------------------------------------------------------------------|
| `--quiz {off,html,pdf,both}` | `modules.quiz`           | Disable quiz attempts or save them as offline HTML, PDF, or both |

### Output controls

| Option                        | Description                                                         |
|-------------------------------|---------------------------------------------------------------------|
| `-v`, `--verbose`             | Include diagnostic logging                                          |
| `--color {auto,always,never}` | Control colored output                                              |
| `--show-filtered`             | Print each intentionally excluded item and the rule that matched it |

The `NO_COLOR` environment convention is honored. Redirected output uses plain
milestones rather than animated terminal progress.

### Legacy sync-option aliases

The 1.0.0 parser still accepts several compact spellings from earlier versions.
These will be removed in future versions! Switch to the new spellings for
future compatibility.

| Legacy alias                  | Canonical option                 |
|-------------------------------|----------------------------------|
| `--totp SERIAL`               | `--totp-serial SERIAL`           |
| `--secretservicetotpsecret`   | `--keyring-store-totp-secret`    |
| `--basedir DIRECTORY`         | `--sync-directory DIRECTORY`     |
| `--cookiefile FILE`           | `--cookie-file FILE`             |
| `--chromiumpath FILE`         | `--browser FILE`                 |
| `--skipcourses LIST`          | `--skip-courses LIST`            |
| `--semester LIST`             | `--semesters LIST`               |
| `--courseprefix VALUE`        | `--course-prefix-handling VALUE` |
| `--updatefiles`               | `--update-files`                 |
| `--updatefilesconflict VALUE` | `--conflict-handling VALUE`      |
| `--alloweddomains LIST`       | `--allowed-domains LIST`         |
| `--excludefiletypes LIST`     | `--exclude-filetypes LIST`       |
| `--excludefiles LIST`         | `--exclude-files LIST`           |
| `--excludelinks LIST`         | `--exclude-links LIST`           |
| `--excludesections LIST`      | `--exclude-sections LIST`        |
| `--excludemodules LIST`       | `--exclude-modules LIST`         |
| `--nolinks`                   | `--no-follow-links`              |

`setup --totp` selects the terminal setup mode; the top-level legacy
`--totp SERIAL` alias is instead a sync-time spelling for `--totp-serial`.

## `setup`

Create a new global configuration, obtain Moodle tokens, and verify the initial
setup.

```shell
syncmymoodle setup
syncmymoodle setup --browser
syncmymoodle setup --totp
```

| Option      | Description                                       |
|-------------|---------------------------------------------------|
| `--browser` | Use browser-assisted RWTH/Moodle sign-in          |
| `--totp`    | Use terminal-based RWTH password and TOTP sign-in |

Browser-assisted setup is the default. The two mode options are mutually
exclusive.

Setup is intended for a new global installation. To change an existing setup,
edit and validate the configuration, then run `auth login` when account or
sign-in settings changed.

## `config`

### `config path`

Print the global configuration path.

```shell
syncmymoodle config path
```

### `config example`

Print the complete packaged, commented TOML example.

```shell
syncmymoodle config example
syncmymoodle config example > config.toml
```

The example is the authoritative in-package inventory of configuration keys and
accepted values.

### `config check`

Load and validate a configuration without syncing.

```shell
syncmymoodle config check
syncmymoodle --config config.toml config check
```

This command catches unknown keys, invalid values, unsafe secret-provider
configuration, and path conflicts that can be determined without a live sync.

### `config migrate`

Convert a legacy syncMyMoodle 0.5.0 JSON configuration to the 1.0.0 TOML format and
obtain a new Moodle token record.

```shell
syncmymoodle config migrate [OPTIONS]
```

| Option                             | Description                                                    |
|------------------------------------|----------------------------------------------------------------|
| `--input FILE`                     | Legacy JSON input; defaults to the legacy global `config.json` |
| `--output FILE`                    | TOML destination; otherwise derived from the input name        |
| `--force`                          | Replace an existing destination file                           |
| `--token-store {keyring,env-file}` | Destination for the migrated Moodle token record               |
| `--token-env-file FILE`            | Environment file used with `--token-store env-file`            |

The source JSON is not modified. Migration signs in once using the legacy
credentials, writes no plaintext RWTH password or TOTP seed into the new TOML,
and leaves cleanup of the legacy file to the user.

See [Migrating from 0.5](migrating-from-0-5.md).

## `auth`

### `auth status`

Inspect and validate the configured authentication state without signing in.

```shell
syncmymoodle auth status
```

The command reports the token store, validates the Moodle API token, and reports
the cached browser-session state. It exits nonzero when the essential authentication
state is missing, invalid, unavailable, or insufficient for enabled
browser-session features.

### `auth login`

Perform one fresh RWTH sign-in and replace this installation's local Moodle
token record.

```shell
syncmymoodle auth login
syncmymoodle auth login --browser
syncmymoodle auth login --totp-manual
```

| Option          | Description                                                                                            |
|-----------------|--------------------------------------------------------------------------------------------------------|
| `--browser`     | Use browser-assisted sign-in for this login regardless of the configured method                        |
| `--totp-manual` | Use TOTP sign-in but prompt for the current code rather than using the configured reusable TOTP source |

The login is accepted only after Moodle confirms the expected account. Replacing
the local record does not revoke the shared Moodle API token used by other
clients.

### `auth migrate`

Copy the current Moodle token record to another supported local store and update
the configuration.

```shell
syncmymoodle auth migrate --to keyring
syncmymoodle auth migrate --to env-file --env-file FILE
```

| Option                    | Description                                       |
|---------------------------|---------------------------------------------------|
| `--to {keyring,env-file}` | Required destination token store                  |
| `--env-file FILE`         | Destination environment file when `--to env-file` |

The previous token store is left untouched. When no token record exists, a TOTP
configuration may sign in once; a browser configuration requires an explicit
`auth login` first.

### `auth forget`

Remove this installation's local Moodle token record and cached browser session.

```shell
syncmymoodle auth forget
```

The command does not remove the TOML configuration, reusable RWTH sign-in
credentials, or the server-side Moodle API token. A reusable provider can obtain
new local tokens during a later sync.

### `auth reset-token`

Revoke the shared Moodle API token and obtain a replacement.

```shell
syncmymoodle auth reset-token
```

This command is available only for TOTP login configurations.

> [!CAUTION]
> Resetting the shared API token signs out the Moodle mobile app and invalidates
> every other syncMyMoodle installation using that token. Prefer `auth login`
> for ordinary local recovery.

See [Authentication](authentication.md) for provider and token details.

## `clean`

Cleanup commands preview their changes unless `--apply` is supplied.

### `clean conflicts`

Find redundant `.syncconflict...` copies.

```shell
syncmymoodle clean conflicts [--path DIRECTORY] [--apply]
```

A conflict copy is redundant only when its content duplicates the current file
or another conflict copy. A unique differing copy is retained.

### `clean caches`

Find per-course metadata caches that can be reset.

```shell
syncmymoodle clean caches [--path DIRECTORY] [--apply]
```

Deleting caches is a recovery operation. The next sync rebuilds them and may
perform additional work.

For both commands, the target is `paths.sync_directory` unless `--path` is
supplied. Applying cleanup requires either an explicit path or an explicitly
configured sync directory; cleanup does not silently apply to an implicit
current-directory default.

See [Cleanup and troubleshooting](cleanup-and-troubleshooting.md).

## Exit status

| Status | Meaning                                                                             |
|-------:|-------------------------------------------------------------------------------------|
|    `0` | Success                                                                             |
|    `1` | The operation completed with one or more failures or an unhealthy diagnostic result |
|    `2` | Invalid command-line usage                                                          |
|  `130` | Interrupted with Ctrl+C                                                             |

A sync can produce useful partial output and still exit with `1`. Consult the
final summary.
