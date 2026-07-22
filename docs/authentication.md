# Authentication and token management

syncMyMoodle separates everyday Moodle access from RWTH Single Sign-On. This
page explains both layers, their local storage, recovery behavior, and the
available commands.

## Two authentication layers

| Data                       | Used for                                                                   | Configuration   |
|----------------------------|----------------------------------------------------------------------------|-----------------|
| Moodle token record        | Normal Moodle API access and creation of temporary Moodle browser sessions | `[auth.tokens]` |
| RWTH sign-in configuration | Obtaining a new Moodle token record through RWTH SSO                       | `[auth.login]`  |

A normal sync requires only the stored Moodle token record. TOTP-based sign-in
needs your RWTH password and a current TOTP code only when syncMyMoodle must
perform a new SSO login.

Moodle does not report the API token's expiry to syncMyMoodle. The stored token
is reused until Moodle rejects it, which may happen because it expired, was
revoked or reset, or otherwise became invalid. A reusable TOTP provider is
therefore useful for unattended recovery even when normal syncs need no RWTH
credentials.

### Moodle token record

The local record contains:

- a Moodle API token;
- when Moodle supplies one, a private/browser-login token.

The API token is used for normal Moodle mobile-service requests. The
browser-login token can be exchanged for a temporary Moodle browser session by
features that cannot operate only through the mobile API, notably supported
Opencast access.

### RWTH sign-in material

TOTP-based sign-in requires:

- the RWTH username;
- the RWTH password;
- the configured TOTP serial;
- either a current TOTP code or a reusable TOTP seed from which a current code
  can be generated.

Browser-assisted sign-in delegates authentication to the RWTH/Moodle web login
page and does not use the configured TOTP secret providers.

## Authentication during a sync

A sync follows these rules:

1. Load the configured Moodle token store.
2. Validate or use the stored API token.
3. Continue without contacting RWTH SSO when the token is usable.
4. If it is missing or confirmed invalid, determine whether automatic recovery
   is possible.
5. Make at most one automatic RWTH SSO attempt during that sync.

Automatic recovery depends on the login configuration:

| Login method/provider       | Automatic replacement during a sync                     |
|-----------------------------|---------------------------------------------------------|
| Browser-assisted            | No; run `syncmymoodle auth login`                       |
| TOTP with `prompt`          | No; run `syncmymoodle auth login`                       |
| TOTP with reusable provider | Yes, when the provider can supply every required secret |

A network or server error does not trigger token replacement because it does
not prove that the token is invalid.

## Initial setup

### Browser-assisted setup

```shell
syncmymoodle setup
```

This is the default setup mode. It asks for the RWTH username, sync directory,
and Moodle token store, then opens an RWTH/Moodle sign-in page.

After signing in, Moodle displays a blue app-launch link. Copy the complete link
address and paste it into the hidden syncMyMoodle prompt. syncMyMoodle verifies
the account before storing the tokens.

> [!CAUTION]
> The app-launch address contains Moodle tokens. Do not save, publish, or paste
> it anywhere except the syncMyMoodle prompt.

Moodle may omit the private/browser-login token for two common reasons:

- The Moodle login is not recent enough. Retry with a fresh login in a new
  private or incognito browser window.
- The account has a legacy Moodle mobile-app token that predates browser-login
  tokens. If a fresh private-window login still fails, revoke the old token on
  Moodle's [Security keys page](https://moodle.rwth-aachen.de/user/managetoken.php),
  then run `syncmymoodle auth login` again.

Setup guides you through the private-window retry and prints the Security keys
URL if it still cannot obtain the token. syncMyMoodle can function without the
browser token, but browser-session features such as Opencast will not work.

Revoking the shared mobile-app token also signs out the Moodle mobile app and
invalidates other syncMyMoodle installations that use it.

### Terminal TOTP setup

```shell
syncmymoodle setup --totp
```

Setup asks for:

- the RWTH SSO username;
- the TOTP serial, such as `TOTP12345678`;
- the sync directory;
- an optional detected password-manager integration;
- the Moodle token store.

The serial is the token identifier in the
[RWTH IDM Token Manager](https://idm.rwth-aachen.de/selfservice/MFATokenManager),
not the current six-digit code.

With the default `prompt` provider, setup prompts for the RWTH password and a
current TOTP code. A reusable provider may instead retrieve the password and
retrieve or generate future codes.

## Moodle token stores

### System keyring

```toml
[auth.tokens]
store = "keyring"
```

This is the recommended token store when the operating system has a working
keyring backend.

A desktop keyring viewer may show a service named `syncmymoodle` and an entry
similar to:

```text
mobile-tokens:moodle.rwth-aachen.de:<username>
```

The grouping and display name depend on the platform and keyring backend.

### Managed environment file

```toml
[auth.tokens]
store = "env-file"
env_file = "/private/path/moodle-tokens.env"
```

syncMyMoodle creates and manages this private file. It stores only the Moodle tokens.

Do not edit or source it manually. Use `auth migrate`, `auth login`, and
`auth forget` to manage the record.

Private files are permission-hardened where supported. Unsafe symlinks and
similar path redirections are rejected for syncMyMoodle-managed secret paths.

## RWTH sign-in methods

### Browser method

```toml
[auth.login]
method = "browser"
```

Use browser-assisted login when:

- you want the simplest interactive experience;
- you use a passkey, security key, or another MFA method offered by the web
  login page;
- you do not need unattended token replacement.

A normal sync still uses stored Moodle tokens. When replacement is required,
run:

```shell
syncmymoodle auth login
```

### TOTP method

```toml
[auth.login]
method = "totp"
totp_serial = "TOTP12345678"
provider = "prompt"
```

Use TOTP login when:

- the entire sign-in should happen in the terminal;
- you need a headless setup;
- you want a reusable provider to recover from expired or invalid Moodle tokens
  automatically.

## TOTP credential providers

`auth.login.provider` controls where the RWTH password and TOTP information come
from.

### `prompt`

```toml
[auth.login]
provider = "prompt"
```

The program asks for the RWTH password and current TOTP code when an explicit
login is run.

Because prompts cannot be answered safely in the middle of an unattended sync,
this provider does not perform automatic token recovery. The sync stops and
asks you to run `auth login`.

### `keyring`

```toml
[auth.login]
provider = "keyring"
keyring_store_totp_secret = false
```

This is a separate use of the system keyring from the Moodle token store:

- `[auth.tokens]` can store Moodle API/browser tokens;
- `[auth.login]` can supply the RWTH password and optionally the TOTP seed.

With `keyring_store_totp_secret = false`, syncMyMoodle does not use a TOTP seed
from the keyring and may prompt for a current TOTP code. Any existing keyring
seed is left untouched. With the setting enabled, syncMyMoodle uses the stored
seed or prompts for and stores one if missing.

> [!IMPORTANT]
> Storing both the password and TOTP seed in one credential backend weakens the
> separation between authentication factors. Use unattended TOTP recovery only
> on a machine and keyring you trust.

### `env-file`

```toml
[auth.login]
provider = "env-file"
env_file = "/private/path/rwth-login.env"
```

The user-managed file contains:

```text
SYNCMYMOODLE_PASSWORD=...
SYNCMYMOODLE_TOTP_SECRET=...
```

The second value is the reusable TOTP seed, not a current six-digit code. It is
optional for an explicit interactive login, but both values are required for
unattended token recovery.

This file is distinct from the app-managed Moodle token environment file under
`[auth.tokens]`.

For a one-run override:

```shell
syncmymoodle --login-env-file /private/path/rwth-login.env
```

### External password managers

Supported provider names are:

- `1password`;
- `bitwarden`;
- `pass`;
- `rbw`;
- `gopass`.

Example structure:

```toml
[auth.login]
method = "totp"
provider = "1password"
totp_serial = "TOTP12345678"
password = "PROVIDER-NATIVE-PASSWORD-REFERENCE"
otp = "PROVIDER-NATIVE-OTP-REFERENCE"
```

`password` and `otp` are references interpreted by the selected provider. They
must not contain the plaintext RWTH password or TOTP seed. The OTP reference is
optional for an explicit interactive login, but unattended token recovery
requires both references.

Interactive setup detects installed supported provider CLIs without retrieving
secrets, lets you choose among those detected, asks for provider-native
references, and verifies them during the initial login.

Provider CLIs can have their own login, unlock, or session requirements. Run the
provider's normal status/login command first when its integration reports that
it is unavailable.

### `command`

```toml
[auth.login]
method = "totp"
provider = "command"
totp_serial = "TOTP12345678"
password_command = ["program", "arg1", "arg2"]
otp_command = ["other-program", "arg1"]
```

The commands are executed directly as argument arrays. No shell is used, so
pipes, redirects, shell variables, and quoting syntax are not interpreted.

- `password_command` is required and must print the RWTH password.
- `otp_command` is optional and must print a current TOTP code.
- Without `otp_command`, an explicit login can prompt for the current code, but
  unattended token recovery is not available.

For security, the command provider is accepted only from the default global
configuration. It is rejected from files selected with `--config`.

## Authentication commands

### `auth status`

```shell
syncmymoodle auth status
```

This read-only diagnostic reports:

- the active Moodle token store;
- whether a token record can be loaded;
- the result of Moodle API-token validation;
- whether a browser-login token is present;
- the cached Moodle browser-session state.

It does not sign in.

The command exits nonzero when required state is missing, invalid, inaccessible,
or insufficient. For example, enabling Opencast without a usable browser-login
token can make the status unhealthy even when basic API access works.

This makes it suitable for a scheduled health check:

```shell
syncmymoodle auth status >/var/log/syncmymoodle-auth.log 2>&1
```

### `auth login`

```shell
syncmymoodle auth login
```

Perform one fresh sign-in using the configured method and replace this
installation's local Moodle token record.

One-off method overrides are available:

```shell
syncmymoodle auth login --browser
syncmymoodle auth login --totp-manual
```

`--totp-manual` uses TOTP login but prompts for a current code instead of
obtaining it from the configured reusable TOTP source.

When an older local token record exists, syncMyMoodle verifies that the new
login belongs to the same Moodle account before accepting the replacement.

`auth login` changes only the local record. It does not revoke the shared Moodle
API token and does not sign out other clients.

Use this command after changing:

- `auth.user`;
- the login method or provider;
- account-related authentication settings;
- a token store that currently has no matching record.

### `auth migrate`

Copy the Moodle token record to another local store and update the
configuration:

```shell
syncmymoodle auth migrate --to keyring
syncmymoodle auth migrate --to env-file --env-file /private/path/tokens.env
```

The previous store is left untouched so migration is recoverable.

When no source token record exists:

- a TOTP configuration may perform one login and then store the new record;
- a browser configuration asks you to run `auth login` first.

After verifying the destination, remove the old record manually only when you
intend to retire that store.

### `auth forget`

```shell
syncmymoodle auth forget
```

Removes:

- this installation's local Moodle token record;
- the cached Moodle browser session.

It leaves:

- the TOML configuration;
- configured reusable RWTH sign-in credentials;
- the server-side Moodle API token.

Consequently, a later sync with a reusable TOTP provider can obtain a new local
record automatically.

Use `auth forget` when retiring a local installation or clearing its local
session state. It is not a server-side token revocation command.

### `auth reset-token`

```shell
syncmymoodle auth reset-token
```

This TOTP-only recovery command revokes the shared Moodle API token and obtains
a replacement.

> [!CAUTION]
> The reset signs out the Moodle mobile app and invalidates every other
> syncMyMoodle installation using the same shared token.

Use it only when:

- recovering from a legacy shared token that cannot create the required browser
  sessions; or
- responding to suspected token exposure.

For ordinary local problems, prefer `auth login` or `auth forget`.

## Temporary Moodle browser sessions

Features such as supported Opencast access can require a Moodle web session.
syncMyMoodle creates that session from the browser-login token and caches it in
`paths.cookie_file`.

Moodle rate-limits creation of private-token browser sessions across devices.
A new session can therefore be temporarily unavailable after another client or
installation recently created one. The retry window can extend to several
minutes.

When this occurs:

1. Do not repeatedly reset the shared API token.
2. Run `syncmymoodle auth status` to inspect the token and session state.
3. Wait for the reported retry window where applicable.
4. Retry the sync.

The cached session is removed by `auth forget`.

## Choosing an authentication model

| Situation                            | Recommended method                                |
|--------------------------------------|---------------------------------------------------|
| Personal desktop, easiest setup      | Browser-assisted setup and keyring token store    |
| Passkey/security key or non-TOTP MFA | Browser-assisted setup                            |
| Interactive terminal use             | TOTP with `prompt`                                |
| Headless scheduled job               | TOTP with a reusable provider on a trusted host   |
| System keyring unavailable           | Environment-file token store                      |
| Existing password-manager workflow   | TOTP with the matching external provider          |

A scheduled sync does not need reusable RWTH credentials while the stored
Moodle token remains valid. Reusable credentials are needed only for unattended
replacement when the token becomes unusable.

## Secret-handling for issue reporting

Never publish or paste into an issue:

- RWTH passwords;
- TOTP seeds;
- current TOTP codes;
- Moodle API tokens;
- Moodle browser-login/private tokens;
- Moodle app-launch addresses;
- environment-file contents;
- password-manager secret values.

References such as an item path can also reveal account or organizational
information. Redact them when they are not needed to reproduce a problem.

## Troubleshooting authentication

Start with:

```shell
syncmymoodle config check
syncmymoodle auth status
```

Then consult [Cleanup and troubleshooting](cleanup-and-troubleshooting.md) for
symptom-based diagnostics.

## Related documentation

- [Getting started](getting-started.md)
- [Configuration reference](configuration.md)
- [CLI reference](cli-reference.md)
- [How synchronization works](how-sync-works.md)
