# Supported content, quizzes, and linked services

This page describes which Moodle activities syncMyMoodle processes, what is
saved locally, and which settings control linked services and quiz output.

## Content model

Moodle exposes course material through different activity and resource types.
syncMyMoodle uses a dedicated handler for supported types and can also inspect
selected HTML descriptions for known links and embeds.

Not every handled Moodle activity becomes a standalone local HTML page. For
example, pages, labels, and H5P activities can be inspected for files and
supported media while their source HTML or package is kept only as internal
processing/cache data.

## Moodle activity matrix

| Moodle type                    | Local result                                                                              | Primary control                          |
|--------------------------------|-------------------------------------------------------------------------------------------|------------------------------------------|
| Assignment (`assign`)          | Attachments, your or your team's submission files, and feedback files                     | `modules.assignment`                     |
| Book (`book`)                  | Files exposed by the activity and supported linked content discovered from available HTML | `filters.exclude_modules`, link settings |
| Folder (`folder`)              | Files contained in the Moodle folder; supported links from its description                | `modules.folder`                         |
| H5P activity (`h5pactivity`)   | Supported links and embedded media found while inspecting the H5P package                 | Link settings                            |
| Label (`label`)                | Supported links and embedded media from the label description                             | Link settings                            |
| LTI (`lti`)                    | Supported RWTH Opencast LTI series or episode content                                     | `links.opencast`                         |
| Page (`page`)                  | Moodle-exposed attachments and supported links/media found in the page                    | Link settings                            |
| PDF annotator (`pdfannotator`) | Files and supported content exposed by the activity                                       | `filters.exclude_modules`, link settings |
| Quiz (`quiz`)                  | Completed attempts as offline HTML, PDF, or both                                          | `modules.quiz`                           |
| File resource (`resource`)     | The Moodle file                                                                           | `modules.resource`                       |
| URL activity (`url`)           | Direct file where resolvable, or supported linked content found at the target             | Link settings                            |

All module types can also be excluded by name, ID, URL, or Moodle type with
`filters.exclude_modules`.

## Assignments

When `modules.assignment = true`, syncMyMoodle can include:

- files attached to the assignment description;
- the configured user's submitted files;
- team submission files where Moodle exposes them for that user;
- feedback files.

When link following is enabled, the assignment description is also scanned for
supported links and embedded media.

syncMyMoodle does not submit work, change submission status, or upload local
edits.

## File resources and folders

`modules.resource` controls ordinary Moodle file resources.

`modules.folder` controls the file inventory of Moodle folder activities. When
link following is enabled, the folder description can also contribute supported
linked content.

Filename, extension, and known-size filters apply after a local target name and
available source metadata have been determined.

## Pages, labels, books, and related HTML content

Pages and labels are used as sources of links and embedded media:

- direct attachments exposed by Moodle can be downloaded;
- supported YouTube, Opencast, public Sciebo, and emedia references can be
  discovered;
- selected same-origin or direct-file URLs can be resolved by the URL/resource
  handler.

syncMyMoodle does not generally save each Moodle page or label itself as a
standalone offline HTML document. That behavior is specific to quiz-attempt
snapshots.

Page HTML needed for discovery can be stored in private course metadata so that
future runs can compare or reuse source information.

## H5P activities

The H5P handler retrieves the package temporarily, inspects its content, and
extracts supported links or embeds. The `.h5p` package itself is not added to
the normal local course tree.

Consequently, H5P support means **linked-content discovery inside supported H5P
content**, not a complete offline H5P player or retained H5P package archive.

## URL activities

A Moodle URL activity can lead to different results:

- a direct downloadable file;
- a known supported linked service;
- an HTML page that contains a supported link or embed;
- no downloaded item when the target is unsupported or excluded.

syncMyMoodle is not a general-purpose recursive web crawler. It follows the
handlers and filters described on this page.

## Linked-content controls

```toml
[links]
follow_links = true
youtube = true
opencast = true
sciebo = true
emedia = true
```

`links.follow_links = false` disables all linked-content processing, regardless
of the individual source settings.

Source-specific switches can be overridden for one run:

```shell
syncmymoodle --no-youtube
syncmymoodle --no-opencast
syncmymoodle --no-sciebo
syncmymoodle --no-emedia
```

### Link and domain filters

`filters.exclude_links` uses case-sensitive shell-style patterns against the
complete discovered URL string.

`filters.allowed_domains`, when nonempty, acts as a case-insensitive allowlist
for discovered HTTP(S) links. A plain domain permits the exact host and its
subdomains; `*.example.org` permits subdomains.

Example:

```toml
[filters]
exclude_links = ["*tracking*", "*playlist?list=*"]
allowed_domains = [
  "youtube.com",
  "youtu.be",
  "video.fsmpi.rwth-aachen.de",
  "*.sciebo.de",
]
```

Both settings also support course-specific TOML tables. See the
[configuration reference](configuration.md#shared-pattern-syntax).

## YouTube

When enabled, supported YouTube links and embeds are downloaded through yt-dlp.
The output name includes the video title and stable video ID.

Important behavior:

- Live streams are excluded.
- Existing output files are not blindly overwritten by yt-dlp.
- Normal syncMyMoodle update and conflict policy still governs managed targets
  where source metadata supports it.
- YouTube extraction can change as the service changes; use a current supported
  yt-dlp version.

### JavaScript runtime requirement

Current yt-dlp YouTube extraction uses an external JavaScript runtime for
reliable challenge handling: [Deno](https://docs.deno.com/runtime/getting_started/installation/).

Install Deno separately when YouTube extraction reports missing JavaScript
runtime or challenge-solver support.

Diagnostics:

```shell
syncmymoodle --dry-run --verbose
```

Also verify the runtime directly:

```shell
deno --version
```

## RWTH Opencast

syncMyMoodle recognizes supported RWTH Opencast links, embeds, and Opencast LTI
activities. Depending on the activity, it can enumerate an episode or a series
and download the exposed media.

Opencast access generally requires a temporary Moodle browser session. To create
one, the Moodle token record must contain a browser-login/private token.

Check the required state with:

```shell
syncmymoodle auth status
```

### Browser-session rate limiting

Moodle limits how frequently a private token can be exchanged for a new browser
session across clients and devices. If another installation recently created a
session, syncMyMoodle can report a temporary retry delay that extends to several
minutes.

Do not repeatedly use `auth reset-token` for this condition. Wait for the
reported interval and retry. See
[Authentication](authentication.md#temporary-moodle-browser-sessions).

## Public Sciebo shares

Supported public Sciebo share links are inspected through the share's WebDAV
interface. Folder shares can be enumerated recursively, preserving the exposed
folder/file structure below the corresponding local module directory.

Sciebo metadata can provide remote sizes and modification information used by
filters and update detection.

Only supported public-share forms are handled. Private Sciebo account login is
not part of syncMyMoodle's authentication model.

## emedia Medizin VEIRA

Supported emedia Medizin VEIRA pages are processed through the dedicated
handler and yt-dlp. The handler selects an available media stream and creates a
normal managed download target.

As with YouTube, upstream website or extractor changes can require an updated
syncMyMoodle/yt-dlp installation.

## Quiz attempts

### Output modes

```toml
[modules]
quiz = "html"
```

| Value  | Output                                                                     |
|--------|----------------------------------------------------------------------------|
| `off`  | Do not save quiz attempts                                                  |
| `html` | Save a self-contained offline HTML snapshot                                |
| `pdf`  | Render a PDF; remove the intermediate HTML only after successful rendering |
| `both` | Keep the offline HTML and render a PDF                                     |

Override the setting for one run:

```shell
syncmymoodle --quiz off
syncmymoodle --quiz html
syncmymoodle --quiz pdf
syncmymoodle --quiz both
```

syncMyMoodle processes completed attempts that Moodle exposes to the configured
account. It does not take quizzes, submit answers, or bypass Moodle review
permissions.

### Offline HTML safety and portability

The quiz snapshot is designed to be opened without contacting Moodle. During
conversion, syncMyMoodle:

- removes scripts, frames, and other active or network-bearing content;
- adds a restrictive Content Security Policy;
- inlines supported same-origin assets within safety and size budgets;
- removes or neutralizes URLs that would otherwise request remote content;
- converts supported mathematical markup where possible.

The result is a static record of the reviewed attempt. Interactive question
behavior and Moodle controls are not preserved.

Quiz review pages contain content supplied by course authors or question banks.
Treat downloaded HTML with the same care as other course material, even though
active network behavior is stripped.

### PDF rendering

PDF mode first builds the same offline HTML snapshot, then opens it in a
headless Chrome-family browser.

Supported browser families are:

- Google Chrome;
- Chromium;
- Microsoft Edge.

Detection order is:

1. `paths.browser` or `--browser` when explicitly set;
2. executable names on PATH;
3. known installation locations for the current platform.

Configure an explicit executable when auto-detection fails:

```toml
[paths]
browser = "/path/to/chrome-or-chromium"
```

or:

```shell
syncmymoodle --browser /path/to/browser --quiz pdf
```

In `pdf` mode:

- successful rendering removes the intermediate HTML;
- a missing browser or rendering failure leaves the HTML snapshot in place so
  the attempt is not lost.

In `both` mode, the HTML is retained regardless of PDF success.

### Quiz filenames and multiple attempts

Attempts are stored below the quiz activity using names based on the module and
attempt index, for example:

```text
Quiz name, Versuch 1.html
Quiz name, Versuch 1.pdf
```

Multiple completed attempts receive distinct attempt numbers.

### Quiz updates and local conflicts

Quiz snapshots are generated artifacts. syncMyMoodle records artifact hashes
and source information in the course metadata cache so that unchanged attempts
can remain untouched and changed review output can participate in normal update
handling.

If both a generated quiz artifact and its local copy changed, the configured
`downloads.conflict_handling` mode applies.

## Filtering content types

Dedicated switches exist for assignments, resources, folders, quizzes, and the
four linked sources. Other Moodle types can be excluded through module rules:

```toml
[filters]
exclude_modules = ["book", "label", "h5pactivity", "pdfannotator"]
```

Use `--dry-run --show-filtered` to verify the result before syncing:

```shell
syncmymoodle --dry-run --show-filtered
```

## Diagnosing linked-content failures

Start with:

```shell
syncmymoodle config check
syncmymoodle auth status
syncmymoodle --dry-run --verbose
```

Then check:

- whether `links.follow_links` is enabled;
- whether the individual source switch is enabled;
- whether an `exclude_links` rule matched;
- whether `allowed_domains` rejected the URL;
- whether required external software is installed;
- whether Moodle provided the browser-login token needed for Opencast;
- whether a browser-session retry delay is active;
- whether the link form is one of the supported public/service-specific forms.

See [Cleanup and troubleshooting](cleanup-and-troubleshooting.md) for a
symptom-based guide.

## Related documentation

- [Configuration reference](configuration.md)
- [How synchronization works](how-sync-works.md)
- [Everyday recipes](everyday-recipes.md)
- [Authentication](authentication.md)
