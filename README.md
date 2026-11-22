# System Steward
by Rajesh Budhathoki

A terminal app for understanding disk usage, reviewing cleanup targets, and managing system maintenance. Built with Python, Textual, and Rich, with keyboard and mouse navigation.

**Available now: macOS. Windows support is planned for a future release.** The current launchers, storage locations, system readings, and maintenance actions use macOS facilities; this release does not support Windows or Linux.

## Get started

Requirements: macOS, Python 3.10 or newer, zsh, and a terminal. Installation requires an internet connection. Touch ID configuration requires macOS Sonoma or later and compatible hardware.

```zsh
git clone https://github.com/rajoyish/system-steward.git
cd system-steward
./setup
./system-steward
```

Setup installs the pinned dependencies from `requirements.txt` into a local `.venv`. To choose your Python interpreter:

```zsh
SYSTEM_STEWARD_PYTHON=python3.12 ./setup
```

You can also double-click `SystemSteward.command` in Finder. The app runs in your terminal. The old `mac-steward`, `MacSteward.command`, and `storage` launchers remain available for compatibility, as do the `MAC_STEWARD_PYTHON` and `STORAGE_PYTHON` setup variables.

Explore a specific folder or generate a report without opening the interface:

```zsh
./system-steward ~/Downloads
./system-steward --json ~/sites > report.json
./system-steward --timeout 30 ~/Downloads
./system-steward --version
```

Without a folder argument, the app audits common Chrome, Adobe, developer, project, and application storage locations, including projects in `~/sites`. The application code lives at the repository root.

## What you can do

- **Explore storage:** browse folders by size, filter paths, inspect details, and export measurements as JSON.
- **Monitor your Mac:** view CPU, RAM, disk, network, battery, process, hardware, and uptime readings.
- **Review cleanup:** inspect Chrome and Adobe caches, user caches and logs, generated developer artifacts, and downloaded installers.
- **Remove applications:** review application bundles and user data matched to their exact bundle IDs.
- **Run maintenance:** preview and select DNS refresh, Quick Look reset, application registration refresh, and Spotlight reindexing.
- **Configure Touch ID for sudo:** review the proposed local PAM configuration before applying it.
- **Keep Awake (macOS):** temporarily prevent idle, display, or disk sleep with a timer, a watched PID, or a wrapped command.

Scans run in the background. The dashboard includes disk usage cards and an expandable system section with live readings and tool previews. Opening a tool or preview does not apply changes.

## Navigation

Use a terminal at least 64 columns × 24 rows; 140 × 40 gives the full layout. The sidebar and details pane collapse on smaller terminals. The interface supports mouse input, TrueColor, `NO_COLOR`, and Unicode symbols without a custom icon font.

The **Explore** category tabs sit above the storage table. Click a category or focus the tabs and use Left/Right to switch. The bar scrolls through categories on narrower terminals and remains available when the sidebar is hidden.

| Key | Action |
| --- | --- |
| ↑ / ↓ | Move through a table or list |
| Enter | Explore a folder or activate the focused control |
| Backspace | Return to the previous folder |
| Tab / Shift-Tab | Move focus |
| / | Filter by name or path; Enter finishes typing |
| s | Switch between size and name sorting |
| r | Rescan |
| e | Export measurements to `system-steward-report-*.json` in the working directory |
| c | Preview Chrome cleanup |
| a | Preview Adobe cleanup; g opens its purge guide |
| m | Open system tools |
| k | Open Keep Awake (macOS caffeinate) |
| d | Expand or collapse live dashboard readings and tools |
| ? | Show help |
| Esc | Dismiss a dialog or leave cleanup |
| q / Ctrl-C | Quit |

In cleanup lists, **Space** toggles a group, **Ctrl-A** selects all, **Ctrl-D** selects none, and **p** inspects exact paths where offered. Nothing is selected by default. Review the selection and its effects before confirming. **Esc/N** cancels, and **Y** confirms in the confirmation dialog. Enter activates the focused button; Cancel is focused by default.

## Cleanup behavior

### Files moved to Trash

Clean System, Purge Dev Artifacts, Clean Installers, and Uninstall App move selected targets to `~/.Trash` with unique names. Existing Trash items are preserved. **Space is reclaimed only after you empty Trash separately.** Restore moved items from Trash if needed.

- Clean System covers user caches and logs in `~/Library/Caches`, `~/Library/Logs`, and `~/.cache`. Close related apps first.
- Developer scans skip `.git`, symlinks, and already discovered artifact trees. Common dependency directories are recognized directly; `build`, `dist`, `target`, and `venv` require a recognized project manifest in their parent. Review generated-looking folders because they can still contain work you need.
- Clean Installers finds `.dmg`, `.pkg`, and `.iso` downloads in Downloads, Desktop, and Documents.
- App removal checks `/Applications` and `~/Applications`, and user Caches, Preferences, Application Support, Saved Application State, and Logs for exact bundle-ID matches. Shared containers and unmatched residual data remain. Removal is blocked when a selected app is running or process inspection fails.

The app refuses targets replaced or redirected after preview and skips symlinked targets and redirected scan roots. Results show each source, destination, and any partial failures. **Stop after item** leaves subsequent groups untouched and lets the current move finish.

### Chrome cleanup

Press **c**. The default **Browser caches only** scope excludes cookies and website databases. **Website data + caches** additionally includes cookies, IndexedDB, local/session storage, service workers, storage buckets, and offline website content. Groups are separated by profile and purpose; SQLite sidecars stay with their databases.

**Save your work and quit Chrome and its helpers before confirming. Website-data cleanup signs you out and permanently deletes selected unsynced website data and offline content. There is no undo.** The app checks for running Chrome processes before and during cleanup and stops if Chrome relaunches.

Cleanup covers standard macOS Chrome profiles and Chrome's separate macOS cache tree. Bookmarks, browsing history, saved password databases, installed extensions, profile preferences, downloaded files outside site storage, and extension-specific `Storage/ext` data are excluded. Other Chrome channels, custom profile roots, and other browsers are outside this action's scope. Selected contents are permanently cleared rather than moved to Trash.

### Adobe cleanup

Press **a** to discover and measure eligible cache groups. Inspect the highlighted group's path and effect, view skipped paths, or press **g** for the built-in purge guide. Custom and external cache locations are excluded.

Eligible locations under `~/Library` include:

| Location | Eligible contents |
| --- | --- |
| `Application Support/Adobe/Common/Media Cache` | Shared media database cache |
| `Application Support/Adobe/Common/Media Cache Files` | Conformed media cache |
| `Application Support/Adobe/Common/Peak Files` | Audio peak cache |
| `Caches/Adobe/…` | Named cache and temporary subfolders, including versioned folders |
| `Caches/Adobe/Lightroom/Video` | Lightroom video cache |
| `Caches/Adobe InDesign` | InDesign cache |
| `Caches/Adobe Camera Raw` or `Adobe Camera Raw N` | Camera Raw cache |
| `Caches/com.adobe.*` | Adobe application cache |

**Save your work and quit Adobe apps and Creative Cloud before confirming.** The app blocks cleanup when Adobe apps or helpers are detected, checks again during deletion, and never force-quits them. Selected cache contents are permanently deleted; cache parent folders remain. Adobe rebuilds caches on subsequent launches, which can make the first project reopen slower.

Preferences, presets, plugins, scripts, workspaces, Lightroom catalogs, catalog sidecars, and `.lrcat-data` are protected. Candidates containing protected folders, catalogs, or symlinks are skipped and rechecked before deletion. The Adobe Application Support root and Library Preferences root are never cleanup targets.

Prefer Adobe's built-in purge controls for media databases where available. Save and close projects first, purge from the app's cache settings, verify completion, then quit and relaunch before editing. Menu labels vary by version. See Adobe's [manual media cache instructions](https://helpx.adobe.com/premiere/desktop/troubleshooting/media-issues/delete-media-cache-files-manually.html), [After Effects storage guidance](https://helpx.adobe.com/after-effects/desktop/memory-storage-performance/memory-and-storage/memory-storage1.html), and [Lightroom Classic performance guidance](https://helpx.adobe.com/lightroom-classic/desktop/technical-support/performance-guidelines/optimize-performance-lightroom.html).

## Keep Awake (macOS)

Press **k**, click **Keep awake** in the sidebar, or choose **Keep Awake (macOS)** from the System tools menu. It uses the built-in `/usr/bin/caffeinate`; no extra background app or System Settings change is required. This feature is macOS-only.

Choose **Idle system sleep** (`-i`, selected by default), **Display sleep** (`-d`), **Disk idle sleep** (`-m`), or a combination. Then choose how the session ends:

- **When stopped / timer ends:** leave the timer blank to run until stopped, or enter seconds such as `3600` for an hour.
- **When an existing PID exits:** enter a running process ID. An optional timer ends the session earlier. Stopping Keep Awake leaves the watched process running.
- **When a command finishes:** enter a command and its working folder. The command runs in the background with no interactive input; recent output and its exit status appear in the tool. Quoted arguments are supported. Shell operators and expansions are not interpreted. This mode follows the command instead of a timer.

Choose **Review & start** and inspect the exact command before confirming. Only one app-managed session runs at a time. You can return to storage browsing while it runs; the sidebar shows when Keep Awake is active. Reopen the tool to see its PID, remaining time, and output, or choose **Stop session**.

App-managed sessions stop when you quit System Steward. **Stopping a wrapped-command session or quitting the app also terminates that command and its process group**, with confirmation. macOS still controls explicit Sleep, lid-close behavior, and low-battery sleep. Preventing disk idle sleep is a request, not continuous disk activity.

For a session independent of System Steward, macOS also supports these Terminal commands:

```zsh
caffeinate                 # Until Ctrl-C; prevents idle system sleep
caffeinate -t 3600         # One hour
caffeinate -di             # Keep the display and system from idle sleeping
caffeinate -m              # Prevent disk idle sleep
caffeinate -i your_command # Until the command exits
caffeinate -i -w 12345     # Until PID 12345 exits
caffeinate -di &           # Run in the shell background
awake_pid=$!               # Save this session's PID
kill "$awake_pid"          # Stop that background session
```

See `man caffeinate` on your Mac or [Apple's caffeinate source](https://github.com/apple-oss-distributions/PowerManagement/blob/main/caffeinate/caffeinate.c) for the native behavior.

## System maintenance and readings

Maintenance tasks show the exact commands and effects before confirmation. Only selected tasks run. A failure stops subsequent tasks. **Stop after item** waits for the current task to finish. Spotlight completion means reindexing was requested, not that indexing has finished. These actions are troubleshooting tools; they do not guarantee faster performance.

Administrator actions use the standard macOS authorization dialog after the app's own confirmation. System Steward does not collect or store administrator passwords. Commands have timeouts, bounded output, and subprocess cleanup.

Touch ID changes use `/etc/pam.d/sudo_local` on macOS Sonoma and later. The app verifies that the main sudo configuration includes that file, refuses automatic edits when a legacy Touch ID entry exists in the main file, previews proposed contents, checks for changes since preview, backs up an existing local file, writes atomically, and verifies the result. It never edits the main sudo configuration. A compatible sensor and enrolled fingerprint are required; enabling the configuration does not verify hardware availability. See Apple's [macOS Sonoma enterprise notes](https://support.apple.com/en-us/109030).

System readings use public macOS Mach APIs and bundled tools, with no third-party maintenance CLI required. CPU comes from sampled tick deltas; RAM includes active, wired, and physical compressor pages; network rates use interface counter deltas. These measurements can differ from Activity Monitor. GPU utilization and temperature are marked unavailable. Permission failures remain partial readings rather than healthy zero values.

The resource score is an app-defined estimate, not a hardware diagnostic. It starts at 100 and subtracts points for CPU, RAM, and startup-disk usage thresholds; details show the formula. A missing required reading makes the estimate unavailable.

## Measurement limits

Scanning is read-only. Deletion is available only through a confirmed cleanup action. Directory sizes use allocated disk blocks from `du`, displayed in binary units. Scans use six workers and a default timeout of 120 seconds per location. Navigation and quitting cancel the previous scan.

The measured total covers the selected locations, not the entire disk. The disk meter uses filesystem usage. APFS snapshots, shared blocks, hard links, and permissions can make directory totals differ from volume usage. Folder listings skip symlinks, and scans do not follow nested symlinks. Missing, unreadable, partial, and timed-out locations are labeled; partial sizes have an asterisk and can undercount. macOS privacy permissions can limit coverage. Cleanup estimates can differ from space actually reclaimed.

## Development

```zsh
./setup
.venv/bin/python -m unittest -v
```

Tests cover scanning, cancellation, timeouts, launchers, keyboard and mouse interaction, resizing, cleanup selection and confirmation, running-app blocking, maintenance, and system readings. Cleanup tests use temporary fake profiles and Library folders instead of real Chrome or Adobe data.

Windows support is a future development goal. Platform-specific discovery, measurements, cleanup targets, maintenance actions, and launchers will need Windows implementations before it is supported.
