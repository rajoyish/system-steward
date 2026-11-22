# System Steward
by Rajesh Budhathoki

A terminal app for understanding disk usage, reviewing cleanup targets, and managing system maintenance. Built with Python, Textual, and Rich, with keyboard and mouse navigation.

**Available on: Windows and macOS.** All features are natively implemented across platforms: **PowerShell 7 (`pwsh`)**, **Scoop**, and Win32 APIs via Python `ctypes` on Windows, and Darwin Mach APIs on macOS.

## Get started

### Windows (PowerShell 7 & Scoop)

Requirements: Windows 10/11, [PowerShell 7 (`pwsh`)](https://github.com/PowerShell/PowerShell), [Scoop package manager](https://scoop.sh), and a terminal.

1. Install Python via Scoop:
   ```powershell
   scoop install python
   ```
2. Clone and run setup:
   ```powershell
   git clone https://github.com/rajoyish/system-steward.git
   cd system-steward
   pwsh -File .\setup.ps1
   pwsh -File .\system-steward.ps1
   ```
3. You can also run `SystemSteward.cmd` or `SystemSteward.ps1` from File Explorer or any terminal.

### macOS (zsh)

Requirements: macOS, Python 3.10 or newer, zsh, and a terminal. Touch ID configuration requires macOS Sonoma or later and compatible hardware.

```zsh
git clone https://github.com/rajoyish/system-steward.git
cd system-steward
./setup
./system-steward
```

Setup installs the pinned dependencies from `requirements.txt` into a local `.venv`. To choose your Python interpreter:

```powershell
# Windows
$env:SYSTEM_STEWARD_PYTHON = "python.exe"; pwsh -File .\setup.ps1
```
```zsh
# macOS
SYSTEM_STEWARD_PYTHON=python3.12 ./setup
```

You can double-click `SystemSteward.cmd` on Windows or `SystemSteward.command` on macOS.

### CLI Usage

Explore a specific folder or generate a report without opening the full TUI:

```powershell
# Windows PowerShell 7
pwsh -File .\system-steward.ps1 ~
pwsh -File .\system-steward.ps1 --json ~ > report.json
pwsh -File .\system-steward.ps1 --timeout 30 ~
pwsh -File .\system-steward.ps1 --version
```
```zsh
# macOS zsh
./system-steward ~/Downloads
./system-steward --json ~/sites > report.json
./system-steward --timeout 30 ~/Downloads
./system-steward --version
```

Without a folder argument, the app audits common Chrome, Adobe, developer, project, and application storage locations across user AppData and Program Files (Windows) or `~/Library` (macOS).

## What you can do

- **Explore storage:** browse folders by size, filter paths, inspect details, and export measurements as JSON. Uses fast native `os.scandir` recursive sizing on Windows (scans multi-gigabyte folders in ~1s) and `du` on macOS.
- **Monitor your system:** view live CPU load, physical RAM usage, drive volumes, network rates, battery status, top processes, hardware model, and uptime. Uses native Win32 APIs (`GetSystemTimes`, `GlobalMemoryStatusEx`, `GetSystemPowerStatus`, `GetTickCount64`) via `ctypes` on Windows, and Mach APIs on macOS.
- **Review cleanup:** inspect Chrome and Adobe caches, user caches and logs, generated developer artifacts (`node_modules`, `.venv`, `target`, `dist`, `build`), and downloaded installers (`.exe`, `.msi`, `.iso`, `.dmg`, `.pkg`).
- **Remove applications:** review installed programs and user data matched to their application identities.
- **Run maintenance:**
  - **Windows:** flush DNS cache (`Clear-DnsClientCache`), reset thumbnail and icon cache database (`thumbcache`), reset Windows Store registration (`wsreset.exe -i`), and restart/rebuild Windows Search indexer (`WSearch`).
  - **macOS:** flush DNS (`dscacheutil`), reset Quick Look (`qlmanage`), refresh LaunchServices (`lsregister`), and reindex Spotlight (`mdutil`).
- **Keep Awake:** temporarily prevent idle, display, or disk sleep with a timer, a watched PID, or a wrapped command. Uses Win32 `SetThreadExecutionState` on Windows (`windows_caffeinate.py`) and `/usr/bin/caffeinate` on macOS. System power and sleep settings stay unchanged.

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

Eligible locations include:

| Platform | Location | Eligible contents |
| --- | --- | --- |
| Windows | `%APPDATA%\Adobe\Common\Media Cache` | Shared media database cache |
| Windows | `%APPDATA%\Adobe\Common\Media Cache Files` | Conformed media cache |
| Windows | `%APPDATA%\Adobe\Common\Peak Files` | Audio peak cache |
| Windows | `%LOCALAPPDATA%\Adobe\…` | Named cache and temporary subfolders, including versioned folders |
| macOS | `Library/Application Support/Adobe/Common/Media Cache` | Shared media database cache |
| macOS | `Library/Application Support/Adobe/Common/Media Cache Files` | Conformed media cache |
| macOS | `Library/Application Support/Adobe/Common/Peak Files` | Audio peak cache |
| macOS | `Library/Caches/Adobe/…` | Named cache and temporary subfolders, including versioned folders |
| macOS | `Library/Caches/Adobe/Lightroom/Video` | Lightroom video cache |
| macOS | `Library/Caches/Adobe InDesign` | InDesign cache |
| macOS | `Library/Caches/Adobe Camera Raw` or `Adobe Camera Raw N` | Camera Raw cache |
| macOS | `Library/Caches/com.adobe.*` | Adobe application cache |

**Save your work and quit Adobe apps and Creative Cloud before confirming.** The app blocks cleanup when Adobe apps or helpers are detected, checks again during deletion, and never force-quits them. Selected cache contents are permanently deleted; cache parent folders remain. Adobe rebuilds caches on subsequent launches, which can make the first project reopen slower.

Preferences, presets, plugins, scripts, workspaces, Lightroom catalogs, catalog sidecars, and `.lrcat-data` are protected. Candidates containing protected folders, catalogs, or symlinks are skipped and rechecked before deletion. The Adobe Application Support / AppData root and Library Preferences root are never cleanup targets.

Prefer Adobe's built-in purge controls for media databases where available. Save and close projects first, purge from the app's cache settings, verify completion, then quit and relaunch before editing. Menu labels vary by version. See Adobe's [manual media cache instructions](https://helpx.adobe.com/premiere/desktop/troubleshooting/media-issues/delete-media-cache-files-manually.html), [After Effects storage guidance](https://helpx.adobe.com/after-effects/desktop/memory-storage-performance/memory-and-storage/memory-storage1.html), and [Lightroom Classic performance guidance](https://helpx.adobe.com/lightroom-classic/desktop/technical-support/performance-guidelines/optimize-performance-lightroom.html).

## Keep Awake (Windows & macOS)

Press **k**, click **Keep awake** in the sidebar, or choose **Keep Awake** from the System tools menu. It prevents idle system sleep without changing permanent power settings:
- **Windows:** Uses the Win32 `SetThreadExecutionState` API (`windows_caffeinate.py`).
- **macOS:** Uses the built-in `/usr/bin/caffeinate`.

Choose **Idle system sleep** (`-i`, selected by default), **Display sleep** (`-d`), **Disk idle sleep** (`-m`), or a combination. Then choose how the session ends:

- **When stopped / timer ends:** leave the timer blank to run until stopped, or enter seconds such as `3600` for an hour.
- **When an existing PID exits:** enter a running process ID. An optional timer ends the session earlier. Stopping Keep Awake leaves the watched process running.
- **When a command finishes:** enter a command and its working folder. The command runs in the background with no interactive input; recent output and its exit status appear in the tool. Quoted arguments are supported. Shell operators and expansions are not interpreted. This mode follows the command instead of a timer.

### Background execution

By default, Keep Awake runs in the background as a detached process:
- **Persists after closing terminal:** You can safely close Warp, PowerShell, or the terminal window; Keep Awake continues running in the background.
- **Small footprint:** the background helper is a single small Python process; you do not need to keep the TUI open.
- **Turns off with computer:** When your computer shuts down, the background session ends and Windows power states reset naturally. No registry or startup entries are created.
- **Safe stop:** the session record in `~/.system-steward/awake_session.json` stores the helper's PID together with its start time and executable, so Stop never terminates an unrelated process that later reuses the same PID.

### Command-line and PowerShell shortcuts

You can also start, check, and stop Keep Awake directly without launching the full TUI:

```powershell
# Quick launcher
pwsh -File .\keep-awake.ps1                   # Start Keep Awake in background
pwsh -File .\keep-awake.ps1 -Display          # Prevent display sleep as well
pwsh -File .\keep-awake.ps1 -Duration 3600    # Run for 1 hour
pwsh -File .\keep-awake.ps1 -Status           # Check status and memory usage
pwsh -File .\keep-awake.ps1 -Stop             # Stop session

# Via system-steward launcher
pwsh -File .\system-steward.ps1 -Awake
pwsh -File .\system-steward.ps1 -Awake -Stop
pwsh -File .\system-steward.ps1 -Awake -Status
```

Choose **Review & start** and inspect the exact command before confirming. Only one app-managed session runs at a time. You can return to storage browsing while it runs; the sidebar shows when Keep Awake is active. Reopen the tool to see its PID, remaining time, and output, or choose **Stop session**.

Stopping a wrapped-command session or quitting the app during command execution also terminates that command and its process group, with confirmation. The operating system still controls explicit Sleep, lid-close behavior, and low-battery sleep. Preventing disk idle sleep is a request, not continuous disk activity.

## System maintenance and readings

Maintenance tasks show the exact commands and effects before confirmation. Only selected tasks run. A failure stops subsequent tasks. **Stop after item** waits for the current task to finish. These actions are troubleshooting tools; they do not guarantee faster performance.

- **Windows:** Uses PowerShell 7 and standard Windows management tools (`Clear-DnsClientCache`, thumbnail database rebuilds, `wsreset.exe -i`, and `WSearch` service restart).
- **macOS:** Administrator actions use the standard macOS authorization dialog after the app's own confirmation. Touch ID changes use `/etc/pam.d/sudo_local` on macOS Sonoma and later.

System readings use native platform APIs with no third-party background daemon required:
- **Windows:** Win32 API calls via `ctypes` (`GetSystemTimes` for CPU load, `GlobalMemoryStatusEx` for RAM, volume metrics for drives, `GetSystemPowerStatus` for battery, and `GetTickCount64` for uptime).
- **macOS:** Public Mach APIs and bundled tools.

The resource score is an app-defined estimate, not a hardware diagnostic. It starts at 100 and subtracts points for CPU, RAM, and system-disk usage thresholds; details show the formula. A missing required reading makes the estimate unavailable.

## Measurement limits

Scanning is read-only. Deletion is available only through a confirmed cleanup action. Directory sizes use fast non-blocking native recursive traversal (`os.scandir`) on Windows, and allocated disk blocks from `du` on macOS, displayed in binary units. Scans use worker pools with cancellation and timeout protection. Navigation and quitting cancel the previous scan.

The measured total covers the selected locations, not the entire disk. The disk meter uses filesystem usage. Missing, unreadable, partial, and timed-out locations are labeled; partial sizes have an asterisk and can undercount. Cleanup estimates can differ from space actually reclaimed.

## Development

Run tests on Windows:

```powershell
pwsh -File .\setup.ps1
pwsh -Command ".venv\Scripts\python -m unittest -v"
```

Run tests on macOS:

```zsh
./setup
.venv/bin/python -m unittest -v
```

Tests cover scanning, cancellation, timeouts, launchers, keyboard and mouse interaction, resizing, cleanup selection and confirmation, running-app blocking, maintenance, Keep Awake sessions, and system readings. Cleanup tests use temporary fake profiles and folders instead of real Chrome or Adobe data.
