# syncMyMoodle documentation

The main [README](../README.md) is the shortest path from installation to the
first successful sync. Use the guides below when you need more detail or want
to configure advanced behavior.

## Start here

- [Getting started](getting-started.md): installation, browser and TOTP setup,
  the first sync, and what to expect on disk
- [Everyday recipes](everyday-recipes.md): common command-line overrides and
  configuration examples
- [How synchronization works](how-sync-works.md): authentication, discovery,
  filtering, caching, update detection, conflicts, and exit behavior

## Reference

- [CLI reference](cli-reference.md): every command, subcommand, and sync option
- [Configuration reference](configuration.md): every TOML setting, accepted
  values, defaults, matching rules, and interactions
- [Authentication reference](authentication.md): Moodle tokens, RWTH sign-in,
  token stores, credential providers, and recovery commands
- [Quizzes and linked content](quizzes-and-linked-content.md): behavior by
  Moodle activity type and supported external service

## Maintenance and recovery

- [Cleanup and troubleshooting](cleanup-and-troubleshooting.md): diagnostic
  workflow, common failures, conflict cleanup, and cache reset
- [Migrating from syncMyMoodle <=0.5.0](migrating-from-0-5.md): converting the
  legacy JSON configuration and credentials safely

## Maintainers

- [Releasing syncMyMoodle](releasing.md): versioning, release notes, tags,
  publishing, verification, and recovery
