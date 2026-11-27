# System Steward
by Rajesh Budhathoki

A terminal app for understanding disk usage, reviewing cleanup targets, and managing system maintenance. Built with Python, Textual, and Rich, with keyboard and mouse navigation.

**Available on: Windows and macOS.** All features are natively implemented across platforms: **PowerShell 7 (`pwsh`)**, **Scoop**, and Win32 APIs via Python `ctypes` on Windows, and Darwin Mach APIs on macOS.

## Install

Install page: **https://rajoyish.github.io/system-steward/**. No clone needed.

### macOS

```zsh
# Homebrew
brew tap rajoyish/system-steward https://github.com/rajoyish/system-steward
brew install system-steward

# or the one-line installer (needs Python 3.10+)
curl -fsSL https://rajoyish.github.io/system-steward/install.sh | sh
```

### Windows

These builds bundle their own Python, so nothing else needs to be installed.

```powershell
# winget
winget install RajeshBudhathoki.SystemSteward

# Scoop
scoop bucket add system-steward https://github.com/rajoyish/system-steward
scoop install system-steward

# or the one-line installer
irm https://rajoyish.github.io/system-steward/install.ps1 | iex
```

Then run `system-steward` in a new terminal.

| Channel | Update | Uninstall |
| --- | --- | --- |
| Homebrew | `brew upgrade system-steward` | `brew uninstall system-steward` |
| winget | `winget upgrade RajeshBudhathoki.SystemSteward` | `winget uninstall RajeshBudhathoki.SystemSteward` |
| Scoop | `scoop update system-steward` | `scoop uninstall system-steward` |
| `install.sh` | run it again | `curl -fsSL https://rajoyish.github.io/system-steward/install.sh \| sh -s -- --uninstall` |
| `install.ps1` | run it again | `& ([scriptblock]::Create((irm https://rajoyish.github.io/system-steward/install.ps1))) -Uninstall` |

## Run from source

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

- **Explore storage:** browse folders by size, filter paths, inspect details, and export measurements as JSON. Windows measures in-process with `os.scandir` (the full audit takes a couple of seconds); macOS uses `du`.
- **Monitor your system:** view live CPU load, physical RAM usage, drive volumes, network rates, battery status, top processes, hardware model, and uptime. Windows readings come from Win32 APIs via `ctypes` with no helper processes; macOS uses Mach APIs and bundled tools.
- **Review cleanup:** inspect Chrome and Adobe caches, temporary files, user caches and logs, generated developer artifacts (`node_modules`, `.venv`, `target`, `dist`, `build`), and downloaded installers (`.exe`, `.msi`, `.iso` on Windows; `.dmg`, `.pkg`, `.iso` on macOS).
- **Remove applications (macOS):** review `.app` bundles and user data matched to their bundle identifiers. Not offered on Windows.
- **Run maintenance:**
  - **Windows:** flush the DNS resolver cache (`ipconfig /flushdns`), refresh the icon cache (`ie4uinit.exe -show`), repair the Microsoft Store (`wsreset.exe -i`), and restart the Windows Search indexer (`WSearch`, after a UAC prompt).
  - **macOS:** flush DNS (`dscacheutil`), reset Quick Look (`qlmanage`), refresh LaunchServices (`lsregister`), and reindex Spotlight (`mdutil`).
- **Keep Awake:** temporarily prevent idle or display sleep (and disk idle sleep on macOS) with a timer, a watched PID, or a wrapped command. Uses Win32 `SetThreadExecutionState` on Windows (`windows_caffeinate.py`) and `/usr/bin/caffeinate` on macOS. System power and sleep settings stay unchanged.
- **Touch ID for sudo (macOS):** enable or disable fingerprint authentication for `sudo`. Not offered on Windows.

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
| k | Open Keep Awake |
| d | Expand or collapse live dashboard readings and tools |
| ? | Show help |
| Esc | Dismiss a dialog or leave cleanup |
| q / Ctrl-C | Quit |

In cleanup lists, **Space** toggles a group, **Ctrl-A** selects all, **Ctrl-D** selects none, and **p** inspects exact paths where offered. Nothing is selected by default. Review the selection and its effects before confirming. **Esc/N** cancels, and **Y** confirms in the confirmation dialog. Enter activates the focused button; Cancel is focused by default.

## Cleanup behavior

### Files moved to the Recycle Bin / Trash

Clean System, Purge Dev Artifacts, Clean Installers, and (on macOS) Uninstall App move selected targets out of the way instead of deleting them:

- **Windows:** to the **Recycle Bin**, restorable from Explorer. Only local fixed drives are accepted; targets on network or removable drives (which have no Recycle Bin) are refused rather than permanently deleted.
- **macOS:** to `~/.Trash` with unique names. Existing Trash items are preserved. A target on another volume is refused with a message to use that volume's Trash.

**Space is reclaimed only after you empty the Recycle Bin / Trash separately.**

- Clean System covers `%LOCALAPPDATA%\Temp`, `%LOCALAPPDATA%\CrashDumps` and `~/.cache` on Windows, and user caches and logs in `~/Library/Caches`, `~/Library/Logs`, and `~/.cache` on macOS. Close related apps first.
- Developer scans skip `.git`, symlinks, and already discovered artifact trees. Common dependency directories are recognized directly; `build`, `dist`, `target`, and `venv` require a recognized project manifest in their parent. Review generated-looking folders because they can still contain work you need.
- Clean Installers finds `.exe`, `.msi`, `.iso`, `.dmg`, and `.pkg` downloads in Downloads, Desktop, and Documents.
- App removal (macOS only) checks `/Applications` and `~/Applications`, and user Caches, Preferences, Application Support, Saved Application State, and Logs for exact bundle-ID matches. Shared containers and unmatched residual data remain. Removal is blocked when a selected app is running or process inspection fails.

The app refuses targets replaced or redirected after preview and skips symlinked targets and redirected scan roots. Results show each source, destination, and any partial failures. **Stop after item** leaves subsequent groups untouched and lets the current move finish.

### Chrome cleanup

Press **c**. The default **Browser caches only** scope excludes cookies and website databases. **Website data + caches** additionally includes cookies, IndexedDB, local/session storage, service workers, storage buckets, and offline website content. Groups are separated by profile and purpose; SQLite sidecars stay with their databases.

**Save your work and quit Chrome and its helpers before confirming. Website-data cleanup signs you out and permanently deletes selected unsynced website data and offline content. There is no undo.** The app checks for running Chrome processes before and during cleanup and stops if Chrome relaunches.

Cleanup covers standard Chrome profiles (`%LOCALAPPDATA%\Google\Chrome\User Data` on Windows, `~/Library/Application Support/Google/Chrome` on macOS) and Chrome's separate macOS cache tree. Bookmarks, browsing history, saved password databases, installed extensions, profile preferences, downloaded files outside site storage, and extension-specific `Storage/ext` data are excluded. Other Chrome channels, custom profile roots, and other browsers are outside this action's scope. Selected contents are permanently cleared rather than moved to Trash.

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

Choose **Idle system sleep** (`-i`, selected by default), **Display sleep** (`-d`), **Disk idle sleep** (`-m`, macOS only; Windows has no equivalent request, so the option is hidden there), or a combination. Then choose how the session ends:

> **Windows laptops with Modern Standby:** many recent PCs (check with `powercfg /a`: "Standby (S0 Low Power Idle)") have no classic S3 sleep. On those, Windows enters standby as soon as the screen turns off from inactivity, and an idle-only request cannot stop that. System Steward detects this and selects **Display sleep** by default, so the screen stays on and the PC stays fully awake. If you clear it, the tool and the command line warn that the PC may still enter standby. To opt out from the command line, use `-AllowDisplaySleep` / `--allow-display-sleep`.

- **When stopped / timer ends:** leave the timer blank to run until stopped, or enter seconds such as `3600` for an hour.
- **When an existing PID exits:** enter a running process ID. An optional timer ends the session earlier. Stopping Keep Awake leaves the watched process running.
- **When a command finishes:** enter a command and its working folder. The command runs in the background with no interactive input; recent output and its exit status appear in the tool. Quoted arguments are supported. Shell operators and expansions are not interpreted. This mode follows the command instead of a timer.

### Background execution

By default, Keep Awake runs in the background as a detached process:
- **Persists after closing terminal:** You can safely close Warp, PowerShell, or the terminal window; Keep Awake continues running in the background.
- **Small footprint:** the background helper is one stdlib-only Python process (about 10 MB private memory, no polling loop); you do not need to keep the TUI open. `keep-awake.ps1 -Status` shows its current memory.
- **Turns off with computer:** When your computer shuts down, the background session ends and Windows power states reset naturally. No registry or startup entries are created.
- **Safe stop:** the session record in `~/.system-steward/awake_session.json` stores the helper's PID together with its start time and executable, so Stop never terminates an unrelated process that later reuses the same PID.

### Command-line and PowerShell shortcuts

You can also start, check, and stop Keep Awake directly without launching the full TUI:

```powershell
# Quick launcher
pwsh -File .\keep-awake.ps1                   # Start Keep Awake in background
pwsh -File .\keep-awake.ps1 -Display          # Prevent display sleep as well (default on Modern Standby PCs)
pwsh -File .\keep-awake.ps1 -AllowDisplaySleep  # Let the display sleep even on Modern Standby (PC may enter standby)
pwsh -File .\keep-awake.ps1 -Duration 3600    # Run for 1 hour
pwsh -File .\keep-awake.ps1 -Status           # Check status and memory usage
pwsh -File .\keep-awake.ps1 -Stop             # Stop session

# Via system-steward launcher
pwsh -File .\system-steward.ps1 -Awake
pwsh -File .\system-steward.ps1 -Awake -Stop
pwsh -File .\system-steward.ps1 -Awake -Status
```
```zsh
# macOS (and the same flags on Windows via system_steward.py)
./system-steward --keep-awake              # start in background (alias: -k, --awake)
./system-steward --keep-awake --display -t 3600
./system-steward --status-awake
./system-steward --stop-awake
```

Background sessions started from the TUI or the command line are the same session: either can show or stop it. Set `SYSTEM_STEWARD_STATE_DIR` to keep the session record somewhere other than `~/.system-steward`.

Choose **Review & start** and inspect the exact command before confirming. Only one app-managed session runs at a time. You can return to storage browsing while it runs; the sidebar shows when Keep Awake is active. Reopen the tool to see its PID, remaining time, and output, or choose **Stop session**.

Stopping a wrapped-command session or quitting the app during command execution also terminates that command and its process group, with confirmation. The operating system still controls explicit Sleep (Start › Power › Sleep), lid-close behavior, and low-battery sleep: Keep Awake cannot block those. If Windows rejects the sleep request, the session fails to start instead of reporting itself active. Preventing disk idle sleep is a request, not continuous disk activity.

## System maintenance and readings

Maintenance tasks show the exact commands and effects before confirmation. Only selected tasks run. A failure stops subsequent tasks. **Stop after item** waits for the current task to finish. These actions are troubleshooting tools; they do not guarantee faster performance.

- **Windows:** Runs the built-in tools shown in the review (`ipconfig /flushdns`, `ie4uinit.exe -show`, `wsreset.exe -i`, and a `WSearch` service restart). Tasks that need administrator rights run through a Windows User Account Control prompt; cancelling the prompt stops the remaining tasks.
- **macOS:** Administrator actions use the standard macOS authorization dialog after the app's own confirmation. Touch ID changes use `/etc/pam.d/sudo_local` on macOS Sonoma and later.

System readings use native platform APIs with no third-party background daemon required:
- **Windows:** Win32 API calls via `ctypes`: `GetSystemTimes` (CPU), `GlobalMemoryStatusEx` (RAM), drive metrics, `GetSystemPowerStatus` (battery), `GetTickCount64` (uptime), a Toolhelp process snapshot with per-process CPU time (top processes, as a share of total CPU since the previous sample), and `GetIfTable2` (per-adapter network rates). Processes owned by other users or elevated processes may be absent from the top list.
- **macOS:** Public Mach APIs and bundled tools.

The resource score is an app-defined estimate, not a hardware diagnostic. It starts at 100 and subtracts points for CPU, RAM, and system-disk usage thresholds; details show the formula. A missing required reading makes the estimate unavailable.

## Measurement limits

Scanning is read-only. Deletion is available only through a confirmed cleanup action. Directory sizes are allocated disk space, displayed in binary units: on Windows an in-process `os.scandir` walk rounds each file to whole clusters, uses the real on-disk size of compressed and sparse files, counts OneDrive online-only placeholders as zero, and does not follow junctions or symlinks (so Scoop's `current` links and pnpm's `node_modules` are not double-counted); on macOS sizes come from `du`. Scans use worker pools with cancellation and timeout protection. Navigation and quitting cancel the previous scan.

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

Lint with [ruff](https://docs.astral.sh/ruff/) (configured in `pyproject.toml`; a dev-only dependency, not needed to run the app):

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m ruff check .
```

Non-UI modules carry type hints on every function, and ruff's `ANN` rules keep it that way; Textual screens and tests are exempt because their handlers are framework callbacks.

Tests cover scanning, cancellation, timeouts, launchers, keyboard and mouse interaction, resizing, cleanup selection and confirmation, running-app blocking, maintenance, Keep Awake sessions, and system readings. Cleanup tests use temporary fake profiles and folders instead of real Chrome or Adobe data, and Keep Awake tests use a throwaway state folder, so a test run never touches your real session.

### Releasing

Releases are built and published by `.github/workflows/release.yml`:

1. Set `VERSION` in `app_identity.py`, commit, and push.
2. Tag and push: `git tag v3.0.1 && git push origin v3.0.1`.

The workflow then:
- builds the source archive (Homebrew, `install.sh`), the self-contained Windows zip (Scoop, `install.ps1`; embedded CPython plus the pinned wheels) and the per-user Windows installer (winget);
- publishes them to a GitHub release with `checksums.txt`;
- commits the rendered `Formula/system-steward.rb`, `bucket/system-steward.json` and winget manifests to `master` (templates live in `packaging/templates/`; `packaging/release.py` renders them);
- opens the winget-pkgs pull request when the `WINGET_TOKEN` repository secret holds a GitHub token with `public_repo` scope.

The install page and scripts live in `docs/`, served by GitHub Pages from `master` → `/docs`.

### Project layout

| Path | Role |
| --- | --- |
| `system_steward.py` | CLI entry point, audit locations, scanner |
| `tui/` | Textual app (`app.py`), cleanup screen, dialogs; `system_steward_tui.py` re-exports them |
| `maintenance_tui.py`, `keep_awake_tui.py`, `dashboard.py` | Tool screens and dashboard cards |
| `native_maintenance.py`, `system_actions.py` | Cleanup discovery, Recycle Bin / Trash moves, reviewed maintenance actions |
| `chrome_cleanup.py`, `adobe_cleanup.py`, `cleanup_selection.py` | Browser and Adobe cache cleanup |
| `system_status.py` | Live readings and the resource estimate |
| `keep_awake.py`, `awake_state.py`, `windows_caffeinate.py` | Keep Awake sessions, the verified session record, the Windows helper |
| `platforms/` | Every OS call: `windows.py` (Win32 via `ctypes`) and `darwin.py` |
| `strings.py` | Wording that differs between Windows and macOS |

## License

[MIT](LICENSE) © Rajesh Budhathoki
