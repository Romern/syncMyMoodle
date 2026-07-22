# Migrating from syncMyMoodle <=0.5.0

Starting with version 1.0.0, syncMyMoodle uses a new command-line interface,
TOML-based configuration, and a separate store for Moodle tokens. The new
format is easier to read, validate, and maintain, and keeps credentials out of
configuration files, shared command examples, and shell history. Legacy JSON
configurations can no longer be used directly for syncs and must first be
migrated.

Use the explicit migration command before replacing an existing 0.5.0 setup.

## Before migration

1. Keep a backup of the legacy `config.json` and existing download directory.
2. Confirm that the legacy RWTH account and TOTP token still work.
3. Install syncMyMoodle >=1.0.0 without deleting the old configuration.
4. Choose a destination for the new Moodle token record using `--token-store {keyring,env-file}`:
    - system keyring (recommended); or
    - a private environment file (for headless systems).

The migration performs one live RWTH/Moodle login to retrieve the tokens. It therefore needs network
access and valid legacy sign-in material.

One change to be aware of is that relative paths are now resolved from the
configuration file's directory.

The first writing sync automatically moves compatible 0.5.0 course caches into
the new account-bound cache layout; a dry run can reuse them without moving
them. Account-specific and unsupported legacy cache data is rebuilt, so the
first sync can make extra requests or transfer some files whose previous state
cannot be reused safely.

## Basic migration

When the legacy JSON is in the old global location:

```shell
syncmymoodle config migrate
```

Choose it explicitly when necessary:

```shell
syncmymoodle config migrate --input /path/to/config.json
```

By default:

- the output path is the input path with `.json` replaced by `.toml`;
- Moodle tokens are stored in the system keyring;
- the source JSON remains unchanged.

Example:

```text
/path/to/config.json  ->  /path/to/config.toml
```

## Select the output file

```shell
syncmymoodle config migrate \
  --input /path/to/config.json \
  --output /path/to/config.toml
```

Migration refuses to overwrite an existing TOML file unless `--force` is
supplied:

```shell
syncmymoodle config migrate \
  --input /path/to/config.json \
  --output /path/to/config.toml \
  --force
```

The input, TOML output, browser-session file, and token environment file must not
resolve to unsafe aliases of the same path.

## Use an environment-file token store

For a system without a usable keyring:

```shell
syncmymoodle config migrate \
  --input /path/to/config.json \
  --token-store env-file \
  --token-env-file /private/path/moodle-tokens.env
```

`--token-env-file` is required with `--token-store env-file`.

The token environment file is managed by syncMyMoodle and stores Moodle API and
browser-login tokens. It is not the same as a user-managed RWTH password/TOTP
environment file.

## What migration does

The command:

1. Reads the legacy JSON only through the explicit migration path.
2. Converts known legacy keys to the new 1.0.0 schema.
3. Resolves legacy relative paths from the JSON file's directory.
4. Obtains the legacy RWTH password and TOTP seed from the JSON or old keyring
   entries where configured.
5. Performs one RWTH login.
6. Obtains and validates a new Moodle token record.
7. Stores the Moodle tokens in the selected token store.
8. Writes a private TOML configuration.
9. Leaves the source JSON unchanged.

The TOML uses `auth.login.provider = "prompt"`. Legacy-configured plaintext password and
TOTP-secret values are not copied into the new configuration.

The new TOML is based on the packaged commented example, with values adjusted to
preserve the legacy behavior where possible.

In particular, migration writes the old defaults explicitly when the legacy
configuration omits them: TOTP login, unchanged course-name prefixes, and
disabled remote-file updates.

## Legacy key mapping

| Legacy JSON key                       | 1.0 key or behavior                                             |
|---------------------------------------|-----------------------------------------------------------------|
| `user`                                | `auth.user`                                                     |
| `totp`                                | `auth.login.totp_serial`                                        |
| `basedir`                             | `paths.sync_directory`                                          |
| `cookie_file`                         | `paths.cookie_file`                                             |
| `chromium_path`                       | `paths.browser`                                                 |
| `selected_courses`                    | `courses.selected`                                              |
| `skip_courses`                        | `courses.skip`                                                  |
| `only_sync_semester`                  | `courses.semesters`                                             |
| `course_prefix_handling`              | `courses.prefix_handling`                                       |
| `updatefiles` or `update_files`       | `downloads.update_files`                                        |
| `update_files_conflict`               | `downloads.conflict_handling`; legacy `none` becomes `keep`     |
| `exclude_filetypes`                   | `filters.exclude_filetypes`                                     |
| `exclude_files`                       | `filters.exclude_files`                                         |
| `exclude_links`                       | `filters.exclude_links`                                         |
| `allowed_domains`                     | `filters.allowed_domains`                                       |
| `exclude_sections` or `skip_sections` | `filters.exclude_sections`                                      |
| `exclude_modules` or `skip_modules`   | `filters.exclude_modules`                                       |
| `no_links` or `nolinks`               | Inverted into `links.follow_links`                              |
| `use_secret_service`                  | Used for the migration login; new provider is reset to `prompt` |
| `secret_service_store_totp_secret`    | Not retained as an enabled 1.0 key during migration             |
| `password`                            | Used once; omitted from TOML                                    |
| `totpsecret`                          | Used once; omitted from TOML                                    |

## Legacy `used_modules`

The old nested `used_modules` tree is converted to the new `[modules]` and
`[links]` settings.

| Legacy entry                | 1.0 key              |
|-----------------------------|----------------------|
| `used_modules.assign`       | `modules.assignment` |
| `used_modules.resource`     | `modules.resource`   |
| `used_modules.folder`       | `modules.folder`     |
| `used_modules.url.youtube`  | `links.youtube`      |
| `used_modules.url.opencast` | `links.opencast`     |
| `used_modules.url.sciebo`   | `links.sciebo`       |
| `used_modules.url.quiz`     | `modules.quiz`       |

Legacy `used_modules` had allowlist-like semantics: when the tree is present,
omitted known entries remain disabled during conversion rather than taking new
1.0 defaults.

Legacy quiz values are converted as follows:

| Legacy value                 | 1.0 `modules.quiz` |
|------------------------------|--------------------|
| `true`, `yes`                | `both`             |
| `false`, `no`, `none`        | `off`              |
| `off`, `html`, `pdf`, `both` | Preserved          |

Unrecognized values are left for normal 1.0 validation to report.

## Secret-service migration

When the legacy configuration used its secret-service option, migration reads
the old keyring entries for the account and TOTP token where available.

The generated TOML still uses:

```toml
[auth.login]
provider = "prompt"
```

This is intentional. Migration uses the old stored secrets for one login but
does not silently opt the new configuration into unattended credential reuse.

After migration, configure a reusable provider explicitly when desired. See
[Authentication](authentication.md#totp-credential-providers).

## Failure and rollback behavior

The source JSON is never modified.

The TOML and new token record are committed as one migration operation as far as
possible. When validation, authentication, token storage, or private-file
writing fails, the command exits with an error.

An existing destination is preserved unless `--force` was explicitly supplied.

Because the old JSON remains available, rollback is straightforward:

1. Do not delete or alter the legacy file.
2. Correct the reported problem.
3. Remove or choose a different incomplete output only after inspecting it.
4. Run the migration again.

## Post-migration checklist

The migrated config should work out of the box, but you may want to check the
following before doing a full sync:

1. Validate the generated TOML:

   ```shell
   syncmymoodle --config /path/to/config.toml config check
   ```

2. Check the token record:

   ```shell
   syncmymoodle --config /path/to/config.toml auth status
   ```

3. Preview course selection and filters:

   ```shell
   syncmymoodle --config /path/to/config.toml --dry-run --show-filtered
   ```

4. Compare important behavior with the legacy setup:
    - sync directory;
    - selected/skipped courses;
    - semester filters;
    - linked sources;
    - module switches;
    - update and conflict policy;
    - quiz output.

5. Run a narrowed real sync if practical:

   ```shell
   syncmymoodle --config /path/to/config.toml --courses COURSE_ID
   ```

6. Move or install the TOML as the global configuration when required.
7. Delete the legacy JSON only after the new setup is confirmed.

> [!WARNING]
> Legacy JSON files can contain the RWTH password and reusable TOTP seed in
> plaintext. Remove old copies, backups, shell history, and shared archives
> carefully after successful migration.

## Differences to review manually

The 1.0.0 configuration has capabilities that do not map directly from legacy setups, including:

- browser-assisted login;
- separate token and login credential stores;
- per-course pattern tables;
- explicit `dry_run` configuration;
- emedia linked-source control;
- new authentication and cleanup subcommands.

Review the [complete configuration reference](configuration.md) to configure those
capabilities.

## Old documentation

For the old command-line interface and JSON format, see the
[syncMyMoodle 0.5.0 documentation on PyPI](https://pypi.org/project/syncMyMoodle/0.5.0/).

## Related documentation

- [Getting started](getting-started.md)
- [Configuration reference](configuration.md)
- [Authentication](authentication.md)
- [CLI reference](cli-reference.md)
