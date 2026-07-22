# Everyday recipes

The examples below use command-line overrides for one-off runs and TOML for
persistent behavior. Command-line sync options override the selected
configuration only for that invocation.

For every option and precedence rule, see the
[CLI reference](cli-reference.md) and
[configuration reference](configuration.md).

## Preview a sync

```shell
syncmymoodle --dry-run
```

A dry run does not write downloads or per-course metadata caches. Add
`--show-filtered` to print the exact rule responsible for each intentional
exclusion:

```shell
syncmymoodle --dry-run --show-filtered
```

Use `--verbose` when diagnosing unexpected behavior:

```shell
syncmymoodle --dry-run --show-filtered --verbose
```

## Sync selected courses

Course selectors accept numeric Moodle course IDs or URLs whose `id` query
parameter contains the course ID.

One run:

```shell
syncmymoodle --courses 12345,67890
```

Persistent configuration:

```toml
[courses]
selected = [
  "12345",
  "https://moodle.rwth-aachen.de/course/view.php?id=67890",
]
```

A nonempty `courses.selected` list takes priority over semester, skipped-course,
and excluded-role settings.

Clear a configured selection for one run:

```shell
syncmymoodle --courses ""
```

## Sync one or more semesters

```shell
syncmymoodle --semesters 25ws,26ss
```

```toml
[courses]
semesters = ["25ws", "26ss"]
```

Semester IDs are matched against the first four characters of Moodle's course
`idnumber`.

## Skip individual courses

```shell
syncmymoodle --skip-courses 12345,67890
```

```toml
[courses]
skip = ["12345", "67890"]
```

`courses.skip` is ignored when `courses.selected` is nonempty.

## Exclude courses where you have a particular role

```shell
syncmymoodle --exclude-course-roles tutor,editingteacher
```

```toml
[courses]
exclude_roles = ["tutor", "editingteacher"]
```

Role shortnames are normalized case-insensitively. Moodle exposes only roles
assigned directly in the course; inherited category or system roles cannot be
matched. If the role lookup fails for a course, syncMyMoodle keeps the course
and records the failure rather than excluding it silently.

## Choose course-directory naming

```toml
[courses]
prefix_handling = "suffix"
```

For a Moodle course named `(VO) Analysis`:

| Value | Local directory |
| --- | --- |
| `keep` | `(VO) Analysis` |
| `remove` | `Analysis` |
| `suffix` | `Analysis (VO)` |

`suffix` is the default.

## Exclude sections globally

```shell
syncmymoodle --exclude-sections 'Announcements,*Exercise*'
```

```toml
[filters]
exclude_sections = ["Announcements", "*Exercise*"]
```

Section rules match the section name or numeric section ID using exact or
case-sensitive shell-style matching.

## Use course-specific section rules

```toml
[filters.exclude_sections]
"*" = ["Announcements"]
"12345" = ["Solutions", "*Optional*"]
```

The `*` entry applies to every course. Rules under a numeric course ID are added
to the global rules for that course.

The same global-list or per-course-table form is supported by:

- `filters.allowed_domains`
- `filters.exclude_links`
- `filters.exclude_sections`
- `filters.exclude_modules`

Command-line overrides for these options create a global list for that run;
use TOML for per-course rules.

## Exclude module types or named activities

Skip all Moodle URL activities and any module whose name starts with
`Optional`:

```toml
[filters]
exclude_modules = ["url", "Optional*"]
```

Module rules can match:

- module ID;
- module name;
- Moodle module type such as `assign`, `folder`, `label`, `page`, `quiz`, or
  `url`;
- the URL supplied by Moodle;
- the synthesized Moodle `view.php` or `launch.php` URL.

To exclude a module in only one course:

```toml
[filters.exclude_modules]
"12345" = ["quiz", "Recording 01"]
```

## Exclude filename extensions

```shell
syncmymoodle --exclude-filetypes mp4,mkv,zip
```

```toml
[filters]
exclude_filetypes = ["mp4", "mkv", ".zip"]
```

Extension matching is case-insensitive and accepts values with or without a
leading dot. It uses the final filename extension, not a MIME type or Moodle
module type.

## Exclude filenames by pattern

```shell
syncmymoodle --exclude-files '*.bak,Temporary*,Thumbs.db'
```

```toml
[filters]
exclude_files = ["*.bak", "Temporary*", "Thumbs.db"]
```

Patterns are matched case-sensitively against the final basename only, not the
complete local path.

## Apply known-size limits

```shell
syncmymoodle --max-file-size 500M
syncmymoodle --min-file-size 10K
```

```toml
[filters]
min_file_size = "10K"
max_file_size = "500M"
```

Accepted suffixes are `K`, `M`, `G`, and `T`, optionally followed by `B` or
`iB`. The values use powers of 1024. Plain integer values are interpreted as
bytes.

Size policy is best-effort and applies only when a reliable size is known
before transfer. This includes many Moodle and Sciebo files and yt-dlp videos
for which a size can be estimated. Unknown-size items are not rejected by a
size limit.

## Disable all linked-content discovery

One run:

```shell
syncmymoodle --no-follow-links
```

Persistent configuration:

```toml
[links]
follow_links = false
```

This disables link inspection and therefore disables YouTube, Opencast,
Sciebo, and emedia discovery, even when their individual source settings remain
`true`.

## Disable one linked source

```shell
syncmymoodle --no-youtube
syncmymoodle --no-opencast
syncmymoodle --no-sciebo
syncmymoodle --no-emedia
```

```toml
[links]
youtube = false
opencast = true
sciebo = true
emedia = true
```

## Restrict discovered HTTP links to selected domains

```toml
[filters]
allowed_domains = [
  "moodle.rwth-aachen.de",
  "engage.streaming.rwth-aachen.de",
  "rwth-aachen.sciebo.de",
]
```

Domain matching is case-insensitive. An entry such as `example.org` allows the
exact host and its subdomains. An entry such as `*.example.org` allows
subdomains. Non-HTTP schemes are not rejected by this allowlist check.

Use course-specific allowlists when necessary:

```toml
[filters.allowed_domains]
"*" = ["moodle.rwth-aachen.de"]
"12345" = ["media.example.org"]
```

The global and course-specific entries are combined.

## Exclude links by URL pattern

```toml
[filters]
exclude_links = [
  "*tracking.example/*",
  "*download.php?temporary=*",
]
```

URL patterns use exact or case-sensitive shell-style matching. They are checked
before the domain allowlist and are also applied to relevant redirects and
final linked-resource URLs.

## Select quiz output

```shell
syncmymoodle --quiz off
syncmymoodle --quiz html
syncmymoodle --quiz pdf
syncmymoodle --quiz both
```

```toml
[modules]
quiz = "both"
```

PDF rendering needs a Chromium-family browser. Configure a specific executable
when auto-detection is unsuitable:

```toml
[paths]
browser = "/path/to/chrome"
```

In `pdf` mode, syncMyMoodle creates the HTML snapshot first and removes it only
after a PDF is produced successfully. If rendering fails, the HTML fallback is
kept.

## Disable assignment, resource, or folder handling

These settings are available in TOML:

```toml
[modules]
assignment = false
resource = true
folder = false
quiz = "html"
```

There are no direct command-line overrides for the first three module toggles.
Use `filters.exclude_modules` for a temporary module-type exclusion.

## Control remote updates

Disable remote updates for one run:

```shell
syncmymoodle --no-update-files
```

With updates disabled, an existing target path is left unchanged and counted as
unchanged. New paths can still be downloaded.

Enable updates persistently:

```toml
[downloads]
update_files = true
```

## Choose a conflict policy

```shell
syncmymoodle --conflict-handling rename
syncmymoodle --conflict-handling keep
syncmymoodle --conflict-handling overwrite
```

```toml
[downloads]
update_files = true
conflict_handling = "rename"
```

`rename` is the safest general choice. It preserves the locally changed copy as
a `.syncconflict...` file before installing the remote update.

## Use a separate configuration

Place `--config` before any subcommand:

```shell
syncmymoodle --config ~/configs/study.toml
syncmymoodle --config ~/configs/study.toml config check
syncmymoodle --config ~/configs/study.toml auth status
```

`setup` always writes the global configuration and cannot be used with
`--config`. Other `config` subcommands besides `config check` also reject
`--config`.

## Temporarily use another sync directory

```shell
syncmymoodle --sync-directory ./moodle-preview --dry-run
```

Relative command-line paths resolve from the current working directory.
Relative TOML paths resolve from the directory containing the TOML file.

## Use an environment file for one TOTP-based sync

The `--login-env-file` override selects the environment-file credential
provider for that run:

```shell
syncmymoodle --login-env-file /secure/rwth-login.env
```

The file can contain:

```text
SYNCMYMOODLE_PASSWORD=your-rwth-password
SYNCMYMOODLE_TOTP_SECRET=your-base32-totp-seed
```

The TOTP seed is optional for interactive use; when absent, a current code is
prompted for if a new RWTH sign-in is needed. For fully unattended token
recovery, both values are required.

This credential file is separate from the Moodle token environment file. Never
use the same path for configuration, browser-session cache, login credentials,
or Moodle tokens.

## Configure an unattended TOTP installation

A typical headless configuration uses distinct environment files:

```toml
[auth]
user = "ab123456"

[auth.tokens]
store = "env-file"
env_file = "moodle-tokens.env"

[auth.login]
method = "totp"
provider = "env-file"
totp_serial = "TOTP12345678"
env_file = "rwth-login.env"

[paths]
sync_directory = "/srv/moodle"
```

`moodle-tokens.env` is managed by syncMyMoodle. `rwth-login.env` is managed by
you and contains the RWTH password and TOTP seed.

Before scheduling syncs, verify:

```shell
syncmymoodle config check
syncmymoodle auth status
syncmymoodle --dry-run --color never
```

A normal sync exits with status `1` when any course, module, or download failed,
which makes it suitable for scheduler or service monitoring.

## Produce plain script-friendly output

```shell
syncmymoodle --color never
```

You can also set the standard `NO_COLOR` environment variable. When stdout is
not interactive, syncMyMoodle automatically replaces animated progress with
plain numbered course and item milestones.
