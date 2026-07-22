# Cleanup and troubleshooting

Start with read-only diagnostics. Apply cleanup only after you understand what
will be removed.

```shell
syncmymoodle config check
syncmymoodle auth status
syncmymoodle --dry-run --verbose
syncmymoodle --dry-run --show-filtered
```

## Diagnostic sequence

1. Confirm which program version is running:

   ```shell
   syncmymoodle --version
   ```

2. Confirm the active configuration and validate it:

   ```shell
   syncmymoodle config path
   syncmymoodle config check
   ```

   For an alternate file:

   ```shell
   syncmymoodle --config config.toml config check
   ```

3. Check token and browser-session state:

   ```shell
   syncmymoodle auth status
   ```

4. Preview the sync with diagnostics:

   ```shell
   syncmymoodle --dry-run --verbose
   ```

5. When content appears to be missing, show intentional exclusions:

   ```shell
   syncmymoodle --dry-run --show-filtered
   ```

A dry run can still contact Moodle and linked services. It does not write normal
downloads or course metadata caches.

## Cleanup commands

Both cleanup subcommands are previews by default. Add `--apply` only after
reviewing the listed paths.

### Choosing the cleanup root

Without `--path`, cleanup uses an explicitly configured
`paths.sync_directory`.

```shell
syncmymoodle clean conflicts
syncmymoodle clean caches
```

To inspect another root:

```shell
syncmymoodle clean conflicts --path DIRECTORY
syncmymoodle clean caches --path DIRECTORY
```

Applying cleanup requires either:

- `--path DIRECTORY`; or
- an explicit `paths.sync_directory` in the active configuration.

syncMyMoodle does not silently apply destructive cleanup to the parser's
implicit current-directory fallback.

The root must exist and be a directory. Applying cleanup acquires the same
writer lock used by a normal sync.

### Redundant conflict copies

With conflict mode `rename`, a local edit can be preserved as a file whose name
contains `.syncconflict`.

Preview redundant copies:

```shell
syncmymoodle clean conflicts
```

Delete only the listed redundant copies:

```shell
syncmymoodle clean conflicts --apply
```

A conflict copy is considered redundant only when its content is identical to:

- the current main file; or
- another conflict copy for the same target.

At least one unique differing copy is retained. The cleanup command does not
choose which version is semantically correct and does not merge files.

Recommended workflow:

1. Preview the candidates.
2. Manually review any conflict files you still care about.
3. Back up important local edits.
4. Run the same command with `--apply`.

### Course metadata caches

Preview per-course cache removal:

```shell
syncmymoodle clean caches
```

Apply it:

```shell
syncmymoodle clean caches --apply
```

Cache cleanup is a recovery action, not routine maintenance. It removes private
per-course inventory/update state. The next sync rebuilds that state and can:

- make additional Moodle and linked-service requests;
- repeat discovery work;
- lose some previous-run evidence used to recognize remote changes or local
  conflicts conservatively.

Ordinary course files are not intentionally removed by this command.

Use cache cleanup when diagnostics indicate damaged or irreconcilable course
metadata, not merely because the directory exists.

## Troubleshooting by symptom

### `syncmymoodle setup` enters the wrong flow

The default is browser-assisted setup:

```shell
syncmymoodle setup
```

Terminal TOTP setup is explicit:

```shell
syncmymoodle setup --totp
```

Check `syncmymoodle --version` if the installed command exposes the old 0.5
interface or different options.

### `--config` or another option is rejected

`--config` is a top-level option and must precede a subcommand:

```shell
syncmymoodle --config config.toml auth status
```

not:

```shell
syncmymoodle auth status --config config.toml
```

Sync options apply only when no subcommand is present. For example,
`--courses` cannot be combined with `auth status`.

`setup` creates the global configuration and does not accept `--config`.

### The program is using an unexpected configuration

syncMyMoodle does not search the current working directory for TOML files.

Show the default location:

```shell
syncmymoodle config path
```

Use another file explicitly:

```shell
syncmymoodle --config /absolute/path/config.toml
```

Remember that relative paths inside TOML resolve from the TOML directory, while
relative CLI paths resolve from the current working directory.

### Configuration validation fails

Run:

```shell
syncmymoodle config check
```

Common causes include:

- misspelled or unknown keys;
- a key under the wrong TOML table;
- a string where an array or Boolean is required;
- an unsupported enum value;
- provider-specific fields used with the wrong provider;
- missing `auth.login.env_file` for the environment-file provider;
- missing `password_command` for the command provider;
- enabling `keyring_store_totp_secret` without the keyring provider;
- using the command provider from a file selected with `--config`;
- conflicting private paths.

Compare the file with:

```shell
syncmymoodle config example
```

### Token store is unavailable

Run:

```shell
syncmymoodle auth status
```

For a keyring store, confirm that the platform has a usable keyring backend and
that the current desktop/session can unlock it.

For an environment-file store, confirm that:

- the configured path is correct;
- the file is readable by the current user;
- the path is not an unsafe symlink;
- the file has not been manually reformatted.

To move a usable record to another store:

```shell
syncmymoodle auth migrate --to keyring
syncmymoodle auth migrate --to env-file --env-file FILE
```

The old store remains untouched after migration.

### Moodle API token is missing or invalid

Run an explicit fresh login:

```shell
syncmymoodle auth login
```

For a one-off browser login:

```shell
syncmymoodle auth login --browser
```

For a one-off manual current TOTP code:

```shell
syncmymoodle auth login --totp-manual
```

Do not use `auth reset-token` for an ordinary local-token problem. It revokes
the shared API token for every client.

### The sync asks for `auth login` instead of recovering automatically

Automatic replacement is available only for TOTP login with a reusable provider
that can supply every required secret.

The following configurations intentionally require an explicit login:

- browser-assisted method;
- TOTP with `provider = "prompt"`;
- a reusable provider that is unavailable, locked, or incompletely configured.

A network/server validation failure also avoids automatic replacement because
the token's validity is unknown.

### Browser-assisted login does not provide a browser-login token

During `setup` or `auth login --browser`:

1. Complete the RWTH/Moodle sign-in.
2. Copy the complete address of the blue app-launch link.
3. Paste it only into the hidden syncMyMoodle prompt.
4. Confirm the account shown by syncMyMoodle.

The resulting token record normally contains both a Moodle API token and a
private browser-login token. The API token is sufficient for most Moodle
mobile-service downloads, but features that require a temporary Moodle browser
session, such as Opencast, also need the browser-login token.

If Moodle does not provide the browser-login token:

1. Follow syncMyMoodle's prompt to retry with a fresh login in a new private or
   incognito browser window.
2. If the private-window retry still fails, the account may have a legacy
   Moodle mobile-app token that predates browser-login tokens. Revoke it on
   Moodle's [Security keys page](https://moodle.rwth-aachen.de/user/managetoken.php).
3. Run `syncmymoodle auth login` and repeat the browser-assisted flow.

For TOTP configurations, `auth reset-token` can perform the revocation and
replacement as a last resort. Either form of revocation invalidates the Moodle
mobile app and other syncMyMoodle installations using the shared token, so do
not use it for an ordinary local login problem.

Never post or share the app-launch address. It contains your Moodle tokens.

### Opencast temporarily fails after authentication

Moodle rate-limits creation of private-token browser sessions across devices and
installations. A recent session creation elsewhere can produce a retry delay of
several minutes.

Run:

```shell
syncmymoodle auth status
```

Wait for the reported retry interval and run the sync again. Repeated token
resets can disrupt other clients.

### RWTH password-manager provider is unavailable

Confirm the provider CLI is installed and usable by the same user/session that
runs syncMyMoodle. Some providers require a separate login, unlock, or session
environment.

Also confirm that:

- `auth.login.provider` names the intended provider;
- `auth.login.password` contains a provider-native reference, not plaintext;
- `auth.login.otp`, when used, points to a current-code/OTP field;
- the reference works when passed to the provider's own CLI.

For immediate interactive recovery:

```shell
syncmymoodle auth login --totp-manual
```

### A course is missing

First inspect intentional selection and exclusions:

```shell
syncmymoodle --dry-run --show-filtered
```

Check:

- `courses.selected` — when nonempty, it overrides all other course selectors;
- `courses.semesters` — values must match the first four characters of Moodle's
  course `idnumber`;
- `courses.skip`;
- `courses.exclude_roles` — matches directly assigned course-role shortnames
  only;
- command-line overrides such as `--courses` or `--semesters`.

Try selecting the numeric course ID explicitly:

```shell
syncmymoodle --courses 12345 --dry-run --verbose
```

A course cannot be downloaded if the configured Moodle account cannot access it.

### A section or activity is missing

Run:

```shell
syncmymoodle --dry-run --show-filtered
```

Check `filters.exclude_sections` and `filters.exclude_modules`, including the
`"*"` rules and the table entry for that course ID.

Pattern matching is case-sensitive. Module rules can match more than the display
name: IDs, Moodle module types, explicit URLs, and synthesized view/launch URLs
are also tested.

Also check dedicated module settings:

```toml
[modules]
assignment = true
resource = true
folder = true
quiz = "html"
```

### A linked video or file is missing

Check in this order:

1. `links.follow_links` is true.
2. The source switch (`youtube`, `opencast`, `sciebo`, or `emedia`) is true.
3. No `exclude_links` pattern matches.
4. A nonempty `allowed_domains` table permits the host.
5. The activity/module itself was not excluded.
6. The URL is a supported form.
7. Required external software or authentication is available.

Use:

```shell
syncmymoodle --dry-run --show-filtered --verbose
```

syncMyMoodle is not a general recursive web downloader. Unsupported arbitrary
web links may intentionally produce no local item.

### A filename filter does not behave as expected

`filters.exclude_files`:

- is case-sensitive;
- uses shell-style patterns;
- matches the basename only, not the full generated path.

`filters.exclude_filetypes`:

- matches the final filename extension;
- is case-insensitive;
- accepts entries with or without a leading dot;
- does not match MIME types or Moodle module types.

For a module type such as `forum` or `label`, use `exclude_modules`.

### A size filter does not exclude a file

Minimum and maximum limits apply only when the remote size is known or can be
estimated before transfer.

A source that does not expose a size can pass the pre-download filter. The
summary or verbose output should be used to distinguish an unknown-size item
from a rule mismatch.

Suffixes are binary: `50M` means 50 MiB.

### A remote update does not replace an existing file

Check:

```toml
[downloads]
update_files = true
```

or override it once:

```shell
syncmymoodle --update-files
```

When the remote source changed and the local file also changed, conflict policy
applies:

- `rename` preserves the local copy and installs the remote version;
- `keep` leaves the local file and skips the update;
- `overwrite` replaces the local file.

With updates disabled, existing targets are intentionally left in place.

### Unexpected `.syncconflict` files appear

They indicate that syncMyMoodle detected both a remote update and local changes
while conflict mode was `rename`.

Compare the conflict copy with the current remote-derived file. Keep or merge
any local work you need, then preview redundant copies:

```shell
syncmymoodle clean conflicts
```

Only add `--apply` after review.

### Moodle removed a file but it remains locally

This is expected. syncMyMoodle reports remote removals but keeps local files.
It does not automatically delete ordinary course material.

Remove obsolete files manually after confirming they are no longer needed.

### Quiz HTML works but PDF does not

PDF rendering requires Chrome, Chromium, or Microsoft Edge.

Check:

```shell
syncmymoodle --browser /path/to/browser --quiz pdf --dry-run --verbose
```

or configure:

```toml
[paths]
browser = "/path/to/browser"
```

PDF mode creates the offline HTML first. If the browser is unavailable or
rendering fails, the HTML fallback is retained.

### YouTube download fails with JavaScript or challenge errors

Use a current supported syncMyMoodle/yt-dlp installation and install a supported
JavaScript runtime. The project compatibility tests use Deno.

```shell
deno --version
syncmymoodle --dry-run --verbose
```

YouTube changes can require a newer yt-dlp even when the syncMyMoodle
configuration is unchanged.

### A sync finishes but exits nonzero

A course, module, or download failure normally does not stop all remaining
work. syncMyMoodle continues, prints a final summary, and exits with status `1`.

Review:

- the first error for each affected course/module;
- the final failed-item counts;
- authentication diagnostics;
- verbose output for the same narrowed course selection.

A status of `1` can accompany successfully downloaded material from unaffected
courses.

### Another sync is already running

Writing runs use a lock under the sync directory. Do not bypass the lock or
manually delete it while another process is active.

Confirm that no sync or cleanup operation is using the same root. After an
abnormal process termination, rerun the command; the lock implementation should
allow recovery when the owning process is no longer active.

A dry run is read-only and does not take the writer lock.

### Cache-related behavior appears inconsistent

Do not edit `.syncmymoodle-cache` or `.syncmymoodle_cache` files manually.
First reproduce with:

```shell
syncmymoodle --dry-run --verbose
```

When there is strong evidence that per-course metadata is damaged, preview:

```shell
syncmymoodle clean caches
```

Apply cache deletion only as a recovery operation. It removes previous inventory
state that helps update and conflict decisions.

## Reporting a problem

A useful issue includes:

- operating system;
- Python version;
- `syncmymoodle --version` output;
- the exact command, with secrets removed;
- the relevant error text;
- whether `config check` succeeds;
- whether `auth status` succeeds;
- a narrowed `--dry-run --verbose` result where safe.

Remove all passwords, TOTP seeds/codes, Moodle tokens, app-launch links,
environment-file contents, password-manager values, and private course/account
information before posting.

## Related documentation

- [CLI reference](cli-reference.md)
- [Configuration reference](configuration.md)
- [Authentication](authentication.md)
- [How synchronization works](how-sync-works.md)
- [Quizzes and linked content](quizzes-and-linked-content.md)
