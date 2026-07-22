# Configuration reference

This is the complete reference for the syncMyMoodle 1.0.0 TOML configuration.

Print the commented example installed with your version:

```shell
syncmymoodle config example
```

Find and validate the global configuration:

```shell
syncmymoodle config path
syncmymoodle config check
```

Validate another file by placing `--config` before the subcommand:

```shell
syncmymoodle --config config.toml config check
```

## Configuration location and path rules

syncMyMoodle reads one configuration file:

- the platform-specific global file by default; or
- the file selected with `--config`.

It does not search the current working directory automatically.

Typical global configuration directories are:

| Platform | Directory                                                    |
|----------|--------------------------------------------------------------|
| Linux    | `$XDG_CONFIG_HOME/syncmymoodle`, or `~/.config/syncmymoodle` |
| macOS    | `~/Library/Application Support/syncmymoodle`                 |
| Windows  | `%APPDATA%\syncmymoodle`                                     |

Use `syncmymoodle config path` to find it for your system.

Relative paths in TOML resolve from the directory containing that TOML file.
Relative paths supplied as command-line overrides resolve from the current
working directory.

Unknown keys and invalid types are rejected. Run `config check` after manual
changes.

## Defaults

Omitted settings use the defaults documented below and shown by `syncmymoodle config example`.
The migration command preserves the previous default behavior for legacy configurations.

## Complete example

Use `config example` to see the complete example configuration for your currently installed version.
Below is a more in-depth description of the possible settings.

## `[auth]`

### `auth.user`

```toml
[auth]
user = "ab123456"
```

| Property     | Value                                      |
|--------------|--------------------------------------------|
| Type         | String                                     |
| Required     | Yes for setup and normal authenticated use |
| CLI override | `--user USER`                              |
| Meaning      | RWTH Single Sign-On username               |

Changing the username does not transform an existing token record into another
account's record. Run `syncmymoodle auth login` after changing account-related
settings. New tokens are accepted only after the account identity is verified.

## `[auth.tokens]`

This table controls storage of the Moodle token record used for normal syncs.
It is independent of the RWTH credential provider under `[auth.login]`.

### `auth.tokens.store`

```toml
[auth.tokens]
store = "keyring"
```

| Property    | Value                                                |
|-------------|------------------------------------------------------|
| Type        | Enum                                                 |
| Values      | `keyring`, `env-file`                                |
| Fallback    | `keyring`                                            |
| Recommended | `keyring` when a working system backend is available |

`keyring` stores the Moodle API and browser-login tokens in the operating
system's credential store.

`env-file` stores them in a private file managed by syncMyMoodle. Do not edit
that file manually.

### `auth.tokens.env_file`

```toml
[auth.tokens]
store = "env-file"
env_file = "~/.config/syncmymoodle/moodle-tokens.env"
```

| Property                       | Value                                                              |
|--------------------------------|--------------------------------------------------------------------|
| Type                           | Path string                                                        |
| Used when                      | `auth.tokens.store = "env-file"`                                   |
| CLI setup/migration equivalent | Token-store prompts or `auth migrate --to env-file --env-file ...` |

The file is app-managed and contains Moodle tokens, not the RWTH password or
TOTP seed. syncMyMoodle hardens private files where the platform permits and
rejects unsafe private-file symlinks.

Do not point this setting at the same file as `auth.login.env_file`.

## `[auth.login]`

This table controls how new Moodle tokens are obtained through RWTH SSO when an
explicit login is requested or automatic recovery is possible.

### `auth.login.method`

```toml
[auth.login]
method = "browser"
```

| Property | Value             |
|----------|-------------------|
| Type     | Enum              |
| Values   | `browser`, `totp` |
| Default  | `browser`         |

`browser` uses the RWTH/Moodle login page and supports the sign-in methods
offered there. It remains interactive when tokens need replacement.

`totp` performs RWTH username/password/TOTP authentication in the terminal. It
can be interactive or use a reusable credential provider.

Provider settings below apply only to the TOTP method.

### `auth.login.provider`

```toml
[auth.login]
method = "totp"
provider = "prompt"
```

| Property | Value                                                                                         |
|----------|-----------------------------------------------------------------------------------------------|
| Type     | Enum                                                                                          |
| Values   | `prompt`, `keyring`, `env-file`, `1password`, `bitwarden`, `pass`, `rbw`, `gopass`, `command` |
| Fallback | `prompt`                                                                                      |

| Provider    | Password source               | TOTP source                         | Automatic token recovery                         |
|-------------|-------------------------------|-------------------------------------|--------------------------------------------------|
| `prompt`    | Interactive prompt            | Interactive current-code prompt     | No                                               |
| `keyring`   | System keyring                | Prompt, or stored seed when enabled | When the password and enabled seed are available |
| `env-file`  | User-managed environment file | Optional TOTP seed from that file   | When the file contains both required values      |
| `1password` | 1Password CLI reference       | Optional OTP reference              | When both references are configured and usable   |
| `bitwarden` | Bitwarden CLI reference       | Optional OTP reference              | When both references are configured and usable   |
| `pass`      | `pass` reference              | Optional OTP reference              | When both references are configured and usable   |
| `rbw`       | `rbw` reference               | Optional OTP reference              | When both references are configured and usable   |
| `gopass`    | `gopass` reference            | Optional OTP reference              | When both references are configured and usable   |
| `command`   | Explicit argv command         | Optional explicit argv command      | When both commands are configured and usable     |

Interactive setup detects supported external password-manager command-line tools
and offers the detected choices. All schema-supported providers can also be
configured manually.

See [Authentication](authentication.md) for provider-specific behavior.

### `auth.login.totp_serial`

```toml
[auth.login]
totp_serial = "TOTP12345678"
```

| Property     | Value                        |
|--------------|------------------------------|
| Type         | String                       |
| Used when    | `auth.login.method = "totp"` |
| CLI override | `--totp-serial SERIAL`       |

This is the TOTP token identifier shown in the
[RWTH IDM Token Manager](https://idm.rwth-aachen.de/selfservice/MFATokenManager),
not the six-digit current code and not the TOTP seed.

### `auth.login.keyring_store_totp_secret`

```toml
[auth.login]
method = "totp"
provider = "keyring"
keyring_store_totp_secret = true
```

| Property        | Value                                                            |
|-----------------|------------------------------------------------------------------|
| Type            | Boolean                                                          |
| Default         | `false`                                                          |
| Valid only with | `provider = "keyring"`                                           |
| CLI override    | `--keyring-store-totp-secret` / `--no-keyring-store-totp-secret` |

When false, the keyring provider does not use a TOTP seed from the keyring. Any
seed already stored there remains untouched, and signing in requires a current
TOTP code interactively.

When true, it uses a stored TOTP seed or prompts for and stores one if missing,
then generates current codes from it. This enables unattended sign-in recovery
but places the reusable second-factor seed in the same credential backend as
the password. Choose this only after considering the security model of the
machine and keyring.

### `auth.login.env_file`

```toml
[auth.login]
method = "totp"
provider = "env-file"
env_file = "~/.config/syncmymoodle/rwth-login.env"
```

| Property      | Value                   |
|---------------|-------------------------|
| Type          | Path string             |
| Required when | `provider = "env-file"` |
| CLI override  | `--login-env-file FILE` |

The user-managed file contains:

```text
SYNCMYMOODLE_PASSWORD=...
SYNCMYMOODLE_TOTP_SECRET=...
```

This file is separate from the app-managed Moodle token environment file under
`auth.tokens.env_file`.

### `auth.login.password`

```toml
[auth.login]
provider = "1password"
password = "op://Private/RWTH/password"
```

| Property      | Value                                              |
|---------------|----------------------------------------------------|
| Type          | String                                             |
| Meaning       | Provider-native reference to the RWTH password     |
| Security rule | Never place the plaintext password in this setting |

This field is used by the supported external password-manager providers. Its
syntax belongs to the selected provider.

It is not used by `prompt`, `keyring`, `env-file`, or `command`.

### `auth.login.otp`

```toml
[auth.login]
provider = "1password"
otp = "op://Private/RWTH/one-time password"
```

| Property | Value                                                                 |
|----------|-----------------------------------------------------------------------|
| Type     | String                                                                |
| Meaning  | Optional provider-native reference that returns the current TOTP code |

When omitted, an explicit login prompts for a current code. Automatic token
recovery is not unattended without an OTP reference. An explicit
`auth login --totp-manual` always prompts for a current code for that login.

### `auth.login.password_command`

```toml
[auth.login]
method = "totp"
provider = "command"
password_command = ["secret-tool", "lookup", "service", "rwth"]
```

| Property      | Value                                               |
|---------------|-----------------------------------------------------|
| Type          | Array of strings                                    |
| Required when | `provider = "command"`                              |
| Execution     | Direct argv execution; no shell                     |
| Restriction   | Accepted only from the default global configuration |

The command must print the RWTH password. Shell strings, pipes, redirects, and
expansion are not interpreted.

For security, the `command` provider is rejected in a configuration selected
with `--config`.

### `auth.login.otp_command`

```toml
[auth.login]
provider = "command"
otp_command = ["secret-tool", "lookup", "service", "rwth-totp"]
```

| Property  | Value                           |
|-----------|---------------------------------|
| Type      | Array of strings                |
| Required  | No                              |
| Execution | Direct argv execution; no shell |

The command must print a current TOTP code. When omitted, an explicit login can
prompt for the current code, but automatic token recovery is not unattended.

## `[paths]`

### `paths.sync_directory`

```toml
[paths]
sync_directory = "~/Moodle"
```

| Property     | Value                        |
|--------------|------------------------------|
| Type         | Directory path               |
| Fallback     | Current directory            |
| Setup        | Filled interactively         |
| CLI override | `--sync-directory DIRECTORY` |

This is the root below which course directories, downloaded content, and
private synchronization metadata are created.

For cleanup with `--apply`, syncMyMoodle requires either an explicit `--path` or
an explicitly configured sync directory. It does not silently apply destructive
cleanup to an implicit current-directory fallback.

### `paths.cookie_file`

```toml
[paths]
cookie_file = "~/.config/syncmymoodle/session"
```

| Property     | Value                                                |
|--------------|------------------------------------------------------|
| Type         | Private file path                                    |
| Fallback     | A `session` file in the user configuration directory |
| CLI override | `--cookie-file FILE`                                 |

This private cache holds temporary Moodle browser-session state used by features
such as Opencast. It is not a browser cookie export intended for manual editing.

The file is removed by `syncmymoodle auth forget`.

### `paths.browser`

```toml
[paths]
browser = "/usr/bin/chromium"
```

| Property     | Value                                        |
|--------------|----------------------------------------------|
| Type         | Executable path                              |
| Default      | Empty; auto-detect Chrome, Chromium, or Edge |
| CLI override | `--browser FILE`                             |
| Used for     | Quiz PDF rendering                           |

An explicit path is tried before PATH and standard platform locations.

## `[courses]`

### Selection precedence

`courses.selected` is an explicit allowlist. When it is nonempty, it overrides:

- `courses.skip`;
- `courses.exclude_roles`;
- `courses.semesters`.

### `courses.selected`

```toml
[courses]
selected = [12345, "https://moodle.rwth-aachen.de/course/view.php?id=67890"]
```

| Property     | Value                                                       |
|--------------|-------------------------------------------------------------|
| Type         | Array of numeric IDs and/or course URL strings              |
| Default      | Empty: consider all discovered courses before other filters |
| CLI override | `--courses LIST`                                            |

Use this for a stable explicit set of courses.

### `courses.skip`

```toml
[courses]
skip = [12345, 67890]
```

| Property     | Value                                   |
|--------------|-----------------------------------------|
| Type         | Array of numeric IDs and/or course URLs |
| Default      | Empty                                   |
| CLI override | `--skip-courses LIST`                   |

Ignored when `courses.selected` is nonempty.

### `courses.exclude_roles`

```toml
[courses]
exclude_roles = ["tutor", "editingteacher"]
```

| Property     | Value                                                   |
|--------------|---------------------------------------------------------|
| Type         | Array of role shortnames                                |
| Matching     | Case-insensitive against directly assigned course roles |
| Default      | Empty                                                   |
| CLI override | `--exclude-course-roles LIST`                           |

Only roles assigned directly in the course are available through the Moodle
mobile API. Roles inherited from a category or the Moodle system cannot be
matched.

If role lookup fails for a course, the course is kept and the failure is
reported.

Ignored when `courses.selected` is nonempty.

### `courses.semesters`

```toml
[courses]
semesters = ["25ws", "26ss"]
```

| Property     | Value              |
|--------------|--------------------|
| Type         | Array of strings   |
| Default      | Empty              |
| CLI override | `--semesters LIST` |

The semester identifier is taken from the first four characters of Moodle's
course `idnumber`.

Ignored when `courses.selected` is nonempty.

### `courses.prefix_handling`

```toml
[courses]
prefix_handling = "suffix"
```

| Value    | Moodle course name | Local directory |
|----------|--------------------|-----------------|
| `keep`   | `(VO) Analysis`    | `(VO) Analysis` |
| `remove` | `(VO) Analysis`    | `Analysis`      |
| `suffix` | `(VO) Analysis`    | `Analysis (VO)` |

| Property     | Value                            |
|--------------|----------------------------------|
| Type         | Enum: `keep`, `remove`, `suffix` |
| Default      | `suffix`                         |
| CLI override | `--course-prefix-handling ...`   |

With `remove`, stable suffixes are added if otherwise identical local directory
names would collide.

## `[downloads]`

### `downloads.update_files`

```toml
[downloads]
update_files = true
```

| Property     | Value                                  |
|--------------|----------------------------------------|
| Type         | Boolean                                |
| Default      | `true`                                 |
| CLI override | `--update-files` / `--no-update-files` |

When enabled, syncMyMoodle uses available source metadata and cached state to
replace a previously downloaded target when its remote version changed.

When disabled, an existing target is left in place even if the remote source is
newer.

### `downloads.conflict_handling`

```toml
[downloads]
conflict_handling = "rename"
```

| Value       | Behavior when remote and local versions both changed                                   |
|-------------|----------------------------------------------------------------------------------------|
| `rename`    | Preserve the local version as a `.syncconflict...` copy and install the remote version |
| `keep`      | Keep the local version and skip the remote update                                      |
| `overwrite` | Replace the local version                                                              |

| Property     | Value                               |
|--------------|-------------------------------------|
| Type         | Enum: `rename`, `keep`, `overwrite` |
| Default      | `rename`                            |
| CLI override | `--conflict-handling ...`           |

> [!WARNING]
> `overwrite` can permanently discard local edits.

### `downloads.dry_run`

```toml
[downloads]
dry_run = false
```

| Property     | Value                        |
|--------------|------------------------------|
| Type         | Boolean                      |
| Default      | `false`                      |
| CLI override | `--dry-run` / `--no-dry-run` |

A dry run performs authentication, discovery, filtering, and planning but does
not write downloads or course metadata caches. It can still make network
requests.

## `[filters]`

### Shared pattern syntax

The following settings accept either a global array or a table of global and
course-specific arrays:

- `filters.allowed_domains`;
- `filters.exclude_links`;
- `filters.exclude_sections`;
- `filters.exclude_modules`.

Global form:

```toml
[filters]
exclude_modules = ["forum", "*Evaluation*"]
```

Course-specific form:

```toml
[filters.exclude_modules]
"*" = ["forum"]
"12345" = ["*Evaluation*"]
"67890" = ["999999"]
```

`"*"` applies to every course. A numeric key applies only to that Moodle course
ID. Global and matching course-specific rules are combined.

Command-line overrides create only a global list. Use TOML for per-course
rules.

Except for domain matching and file-extension matching, pattern matching is
case-sensitive and uses shell-style `fnmatchcase` semantics. A value without
wildcards also works as an exact match.

### `filters.max_file_size`

```toml
[filters]
max_file_size = "500M"
```

| Property     | Value                                       |
|--------------|---------------------------------------------|
| Type         | Empty string, integer bytes, or size string |
| Default      | Empty: no maximum                           |
| CLI override | `--max-file-size SIZE`                      |

Supported binary suffixes include `K`, `M`, `G`, and `T`, optionally followed by
`B` or `iB`. For example, `50M` means 50 MiB.

The limit applies only when a source reports or exposes enough information to
know or estimate the remote size.

### `filters.min_file_size`

```toml
[filters]
min_file_size = "10K"
```

Same syntax as `max_file_size`. Known-size files smaller than the limit are
excluded.

### `filters.exclude_filetypes`

```toml
[filters]
exclude_filetypes = ["mp4", ".mkv"]
```

| Property     | Value                                              |
|--------------|----------------------------------------------------|
| Type         | Array of extensions                                |
| Matching     | Case-insensitive against the final filename suffix |
| Leading dot  | Optional                                           |
| CLI override | `--exclude-filetypes LIST`                         |

This setting matches filename extensions, not MIME types and not Moodle module
types.

To exclude a Moodle activity type such as `forum`, use
`filters.exclude_modules`.

### `filters.exclude_files`

```toml
[filters]
exclude_files = ["Lecture*.mp4", "*.tmp"]
```

| Property     | Value                                    |
|--------------|------------------------------------------|
| Type         | Array of shell-style patterns            |
| Matching     | Case-sensitive against the basename only |
| CLI override | `--exclude-files LIST`                   |

The generated directory path is not included in the match. A pattern cannot
select one course by embedding its directory name; use course-specific pattern
tables on a structured filter instead.

### `filters.exclude_links`

```toml
[filters]
exclude_links = ["*tracking.example/*", "*playlist?list=*"]
```

| Property     | Value                                         |
|--------------|-----------------------------------------------|
| Type         | Global array or per-course pattern table      |
| Matching     | Case-sensitive against discovered URL strings |
| CLI override | `--exclude-links LIST`                        |

These rules apply to discovered linked content before a supported handler
follows the URL.

### `filters.allowed_domains`

```toml
[filters]
allowed_domains = ["youtube.com", "youtu.be", "*.sciebo.de"]
```

| Property     | Value                                         |
|--------------|-----------------------------------------------|
| Type         | Global array or per-course domain table       |
| Matching     | Case-insensitive for discovered HTTP(S) links |
| Default      | Empty: no domain allowlist                    |
| CLI override | `--allowed-domains LIST`                      |

A plain entry such as `example.org` permits the exact domain and its subdomains.
An entry such as `*.example.org` permits subdomains.

This setting governs discovered HTTP(S) links. It is not a general firewall and
does not replace source-specific enable/disable settings.

### `filters.exclude_sections`

```toml
[filters]
exclude_sections = ["General", "0", "*Archived*"]
```

| Property         | Value                                    |
|------------------|------------------------------------------|
| Type             | Global array or per-course pattern table |
| Matching targets | Section name and numeric ID              |
| CLI override     | `--exclude-sections LIST`                |

Excluding a section removes every activity and resource beneath that section
from the planned sync.

### `filters.exclude_modules`

```toml
[filters]
exclude_modules = ["forum", "*Evaluation*", "123456"]
```

| Property         | Value                                                                                            |
|------------------|--------------------------------------------------------------------------------------------------|
| Type             | Global array or per-course pattern table                                                         |
| Matching targets | Module ID, display name, Moodle module type, explicit URL, and synthesized view/launch URL forms |
| CLI override     | `--exclude-modules LIST`                                                                         |

Common Moodle module-type values include `assign`, `folder`, `quiz`, `resource`,
`page`, `label`, `h5pactivity`, `lti`, `book`, `url`, and `pdfannotator`.

This is the general way to suppress a module type that does not have a dedicated
switch under `[modules]`.

## `[links]`

### `links.follow_links`

```toml
[links]
follow_links = true
```

| Property     | Value                                  |
|--------------|----------------------------------------|
| Type         | Boolean                                |
| Default      | `true`                                 |
| CLI override | `--follow-links` / `--no-follow-links` |

When false, all linked-content discovery is disabled, including every
source-specific setting below.

Direct Moodle files remain available through their normal module handlers.

### `links.youtube`

```toml
[links]
youtube = true
```

Enables supported YouTube links and embeds when link following is enabled.

CLI override: `--youtube` / `--no-youtube`.

### `links.opencast`

```toml
[links]
opencast = true
```

Enables RWTH Opencast links, embeds, and supported Opencast LTI activities when
link following is enabled. Opencast access can require a temporary Moodle
browser session and therefore a browser-login token.

CLI override: `--opencast` / `--no-opencast`.

### `links.sciebo`

```toml
[links]
sciebo = true
```

Enables downloads from supported public Sciebo share links when link following
is enabled.

CLI override: `--sciebo` / `--no-sciebo`.

### `links.emedia`

```toml
[links]
emedia = true
```

Enables supported emedia Medizin VEIRA videos when link following is enabled.

CLI override: `--emedia` / `--no-emedia`.

## `[modules]`

These settings control selected core Moodle module handlers. Other module types
can contribute direct files or linked content and can be suppressed with
`filters.exclude_modules`.

### `modules.assignment`

```toml
[modules]
assignment = true
```

Includes assignment attachments, the configured user's or team's submissions,
and feedback files. Assignment descriptions can also contribute linked content
when link following is enabled.

Default: `true`.

### `modules.resource`

```toml
[modules]
resource = true
```

Includes Moodle file resources.

Default: `true`.

### `modules.folder`

```toml
[modules]
folder = true
```

Includes files contained in Moodle folder activities. Folder descriptions can
also contribute linked content when link following is enabled.

Default: `true`.

### `modules.quiz`

```toml
[modules]
quiz = "html"
```

| Value  | Behavior                                               |
|--------|--------------------------------------------------------|
| `off`  | Do not save quiz attempts                              |
| `html` | Save self-contained offline HTML                       |
| `pdf`  | Render PDF; retain HTML as fallback if rendering fails |
| `both` | Keep offline HTML and render PDF                       |

Default: `html`.

CLI override: `--quiz {off,html,pdf,both}`.

PDF output requires Chrome, Chromium, or Microsoft Edge. See
[Quizzes and linked content](quizzes-and-linked-content.md).

## Command-line override rules

Sync options override the TOML value for one run only.

Boolean settings have positive and negative forms:

```shell
syncmymoodle --opencast
syncmymoodle --no-opencast
```

Comma-separated list options replace the configured list. An empty string clears
it for one run:

```shell
syncmymoodle --exclude-filetypes ""
```

CLI pattern lists are global and cannot represent course-specific TOML tables.

## Validation and safe editing workflow

1. Find the active file:

   ```shell
   syncmymoodle config path
   ```

2. Make a backup.
3. Edit the TOML.
4. Validate it:

   ```shell
   syncmymoodle config check
   ```

5. Preview the result:

   ```shell
   syncmymoodle --dry-run --show-filtered
   ```

6. When account, sign-in method, or account-bound token settings changed, obtain
   a fresh matching token record:

   ```shell
   syncmymoodle auth login
   ```

## Related documentation

- [Getting started](getting-started.md)
- [Everyday recipes](everyday-recipes.md)
- [How synchronization works](how-sync-works.md)
- [CLI reference](cli-reference.md)
- [Authentication](authentication.md)
