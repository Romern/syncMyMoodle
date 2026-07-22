# Getting started

This guide covers installation, the two setup modes, the first sync, and the
local directory layout. For a compact overview, see the main
[README](../README.md).

## 1. Install syncMyMoodle

Python 3.11 or newer is required. Install the command as an isolated tool with
one of the following methods.

### uv

```shell
uv tool install syncmymoodle
```

### pipx

```shell
pipx install syncmymoodle
```

Check that the command is available:

```shell
syncmymoodle --version
```

Optional features need additional software:

- Install Chrome, Chromium, or Microsoft Edge to render quiz attempts as PDF.
- Install a JavaScript runtime supported by yt-dlp for reliable YouTube
  extraction. The project compatibility workflow uses Deno.

## 2. Choose a setup mode

Setup writes the global `config.toml`, obtains a Moodle token record, and
verifies the selected token store.

| Mode | Command | Characteristics |
| --- | --- | --- |
| Browser-assisted | `syncmymoodle setup` | Default; supports every MFA method offered on the RWTH login page; replacement login remains interactive |
| Terminal TOTP | `syncmymoodle setup --totp` | Uses RWTH password and TOTP in the terminal; can use a reusable provider for automatic token recovery |

Setup is intended for a new installation. It refuses to overwrite an existing
global configuration. To change an existing installation, edit the
configuration and validate it with `syncmymoodle config check`.

If a legacy global `config.json` exists, setup stops and tells you to migrate it
first. See [Migrating from syncMyMoodle 0.5](migrating-from-0-5.md).

## 3A. Browser-assisted setup

Run:

```shell
syncmymoodle setup
```

Setup asks for:

1. Your RWTH SSO username.
2. The local sync directory.
3. Whether to use the system keyring for Moodle tokens.
4. An environment-file path if a usable keyring is not selected.

It then creates a one-use Moodle mobile-app launch request and opens the RWTH
login page when possible. Complete the login using the MFA method offered by
RWTH SSO.

Moodle displays a blue app-launch link when it cannot open an app automatically:

1. Right-click the blue link.
2. Copy its complete link address.
3. Paste it into the hidden syncMyMoodle prompt.
4. Confirm the Moodle account that will be associated with the configured RWTH
   username.

> [!CAUTION]
> The app-launch link contains your Moodle tokens. Treat it as a password. Do
> not save it in shell history, chat, screenshots, notes, or issue reports.

Moodle normally returns both:

- an API token used for normal sync operations;
- a browser-login token used to create temporary Moodle browser sessions for
  features such as embedded Opencast.

If the browser-login token is missing, setup offers a private/incognito-window
retry. You may finish setup with limited browser-session support, but enabled
features that need that session can fail later. Run `syncmymoodle auth login`
to retry browser-assisted token acquisition.

## 3B. Terminal TOTP setup

Run:

```shell
syncmymoodle setup --totp
```

Setup asks for:

1. Your RWTH SSO username.
2. Your TOTP serial, for example `TOTP12345678`.
3. The local sync directory.
4. An optional detected password-manager provider.
5. The Moodle token store.
6. Any credentials needed for the initial login.

The TOTP serial is the identifier shown in the
[RWTH IDM Token Manager](https://idm.rwth-aachen.de/selfservice/MFATokenManager).
It is not the current six-digit code and not the TOTP seed.

### Default interactive provider

When no reusable provider is selected, setup uses
`auth.login.provider = "prompt"`. It asks for the RWTH password and a current
six-digit TOTP code for the initial login.

Future normal syncs still use the stored Moodle tokens without asking for these
credentials. If the tokens become invalid, the sync cannot recover
non-interactively and directs you to:

```shell
syncmymoodle auth login
```

### Password-manager provider

During terminal setup, syncMyMoodle detects supported password-manager command
line clients that are installed. Detection checks executable availability; it
does not read secrets.

When you select a provider, setup asks for provider-native references, then
verifies them during the initial login. The supported detected providers are:

- 1Password (`op`)
- Bitwarden (`bw`)
- pass
- rbw
- gopass

The password reference is required. The TOTP reference is optional; leaving it
blank means the password can be retrieved automatically but a current code is
still prompted for during an interactive login.

Other provider types, including environment-file, keyring, and custom-command
providers, can be configured manually. See the
[authentication reference](authentication.md).

## 4. Start the first sync

Run:

```shell
syncmymoodle
```

A normal run:

1. Loads and validates the selected configuration.
2. Loads and validates the stored Moodle token record.
3. Discovers your Moodle courses.
4. Applies course, role, semester, section, module, link, and file policy.
5. Builds a local download plan.
6. Downloads or updates the selected content.
7. Stores per-course metadata for the next run.
8. Prints filtered items, remote removals, and a final summary.

The default configuration uses these important values:

```toml
[courses]
prefix_handling = "suffix"

[downloads]
update_files = true
conflict_handling = "rename"

[modules]
quiz = "html"
```

This means later syncs update remote changes, preserve locally changed files as
conflict copies, and save quiz attempts as offline HTML.

## 5. Preview before writing

A dry run performs discovery and planning but does not write downloaded files,
course metadata caches, or a newly created browser-session cache:

```shell
syncmymoodle --dry-run
```

To see why items were excluded:

```shell
syncmymoodle --dry-run --show-filtered
```

A dry run may still make network requests needed to authenticate, discover
courses, inspect links, or estimate remote sizes.

## 6. Understand the local layout

The normal layout is:

```text
<sync directory>/
├── <semester>/
│   ├── <course>/
│   │   ├── <section>/
│   │   │   └── downloaded files
│   │   └── ...
│   └── ...
└── .syncmymoodle-cache/
    └── account-bound per-course metadata
```

The semester is taken from the first four characters of Moodle's course
`idnumber`. When no semester identifier is available, the directory is named
`unknown-semester`.

Names supplied by Moodle or linked services are sanitized for supported
filesystems. Invalid path characters are removed, reserved Windows names are
protected, long path components are shortened with a stable hash, and name
collisions receive stable suffixes.

The hidden `.syncmymoodle-cache` directory stores metadata used for change
detection and incremental discovery. It does not contain your RWTH password or
TOTP seed. Do not delete it as routine maintenance; use
`syncmymoodle clean caches` only for recovery.

## 7. Check the result

Every sync ends with a summary. A successful writing run resembles:

```text
Sync complete in ...: ... courses, ... downloaded, ... updated, ... unchanged,
... filtered, 0 failed, ... transferred.
```

When an individual course, module, or download fails, syncMyMoodle normally
continues with the remaining items and exits with status `1` at the end.

If content recorded by the previous complete inventory is no longer present in
Moodle, syncMyMoodle lists it under:

```text
No longer present in Moodle (...; local files kept)
```

No local file is removed automatically.

## Next steps

- Use [Everyday recipes](everyday-recipes.md) for common selections and filters.
- Read [How synchronization works](how-sync-works.md) before choosing update and
  conflict policies.
- Use the [configuration reference](configuration.md) for every TOML setting.
- Use the [authentication reference](authentication.md) for unattended token
  recovery and token-store choices.
