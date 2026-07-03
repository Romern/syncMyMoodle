"""syncMyMoodle - synchronization client for RWTH Moodle.

syncMyMoodle Module map grouped by role.

Entry / orchestration:
    __main__        thin ``python -m syncmymoodle`` shim -> cli.main
    cli             argparse, config building, keyring, main run(ctx) flow
    sync            walks courses -> sections -> modules into the node tree

Auth:
    rwth            RWTH SSO login flow and service/status checks
    totp            HOTP/TOTP code generation for 2FA login

Moodle API:
    moodle          Moodle web-service client (login token, courses, ...)
    moodle_files    turns Moodle content payloads into file nodes

Content sources / parsing:
    opencast        Opencast LTI launch and episode/track resolution
    sciebo          Sciebo public-share WebDAV (PROPFIND) traversal
    links           scans HTML/text for embedded videos and shared files

Sync tree:
    sync_handlers   per-module-type handlers + @register_handler registry

Download:
    downloader      tree walk, update/conflict policy, ETag/resume handling

Persistence:
    storage         private gzip-JSON and cookie (de)serialization
    course_cache    per-course sync-tree cache (.syncmymoodle_cache)
    pathing         path sanitization, traversal safety, conflict paths

Core (cross-cutting):
    node            Node tree model and name-clash resolution
    context         SyncContext: per-run mutable state (session, caches, ...)
    config          Config: typed, normalized view of user settings
    constants       shared URLs, regexes and option constants
    filters         course/section/module/link skip rules
"""
