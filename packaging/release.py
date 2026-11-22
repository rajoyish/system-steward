"""Release tooling: build the source archive and render package-manager manifests.

    python packaging/release.py source 3.0.0 dist/
        Writes dist/system-steward-3.0.0.tar.gz with only the files the app needs at runtime.

    python packaging/release.py render 3.0.0 dist/checksums.txt
        Renders the Homebrew formula, Scoop manifest and winget manifests for that release from
        templates, using the release checksums and the pinned wheels' PyPI checksums.

Every channel installs the same layout: the app's source files, an isolated Python environment
with the pinned requirements, and a small `system-steward` launcher.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = Path(__file__).resolve().parent / "templates"
REPO = "rajoyish/system-steward"
HOMEPAGE = "https://rajoyish.github.io/system-steward/"
DESCRIPTION = "Terminal app for storage analysis, cleanup and system maintenance on macOS and Windows"
LICENSE = "MIT"  # SPDX identifier used by every manifest
RUNTIME_DIRS = ("tui", "platforms")
RUNTIME_EXTRA = ("system_steward.tcss", "requirements.txt", "README.md", "LICENSE")


def runtime_files() -> list[Path]:
    """Application files shipped to users (no tests, launchers for a source checkout, or tooling)."""
    files = [path for path in ROOT.glob("*.py") if not path.name.startswith("test_") and path.name != "steward_test_env.py"]
    for directory in RUNTIME_DIRS:
        files += [path for path in (ROOT / directory).rglob("*.py") if "__pycache__" not in path.parts]
    files += [ROOT / name for name in RUNTIME_EXTRA]
    return sorted(files)


def app_version() -> str:
    match = re.search(r'^VERSION = "([^"]+)"', (ROOT / "app_identity.py").read_text(), re.M)
    if not match:
        raise SystemExit("VERSION not found in app_identity.py")
    return match[1]


def check_version(version: str) -> None:
    if version != app_version():
        raise SystemExit(f"Release version {version} does not match app_identity.VERSION {app_version()}")


def build_source(version: str, outdir: Path) -> Path:
    check_version(version)
    outdir.mkdir(parents=True, exist_ok=True)
    target = outdir / f"system-steward-{version}.tar.gz"
    prefix = f"system-steward-{version}"

    def normalized(info: tarfile.TarInfo) -> tarfile.TarInfo:
        # Reproducible archive: fixed owner and timestamp, sane permissions.
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        info.mtime = 0
        info.mode = 0o755 if info.isdir() else 0o644
        return info

    with tarfile.open(target, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path in runtime_files():
            archive.add(path, arcname=f"{prefix}/{path.relative_to(ROOT).as_posix()}", filter=normalized)
    print(target)
    return target


def requirements() -> list[tuple[str, str]]:
    pins = []
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            name, _, version = line.partition("==")
            if not version:
                raise SystemExit(f"requirements.txt must pin exact versions: {line}")
            pins.append((name, version))
    return pins


def wheel(name: str, version: str) -> tuple[str, str]:
    """URL and sha256 of the pure-Python wheel for a pinned requirement."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as response:
        data = json.load(response)
    for item in data["urls"]:
        if item["packagetype"] == "bdist_wheel" and item["filename"].endswith("-none-any.whl"):
            return item["url"], item["digests"]["sha256"]
    raise SystemExit(f"{name} {version} has no pure-Python wheel; the Homebrew formula needs one")


def read_checksums(path: Path) -> dict[str, str]:
    sums = {}
    for line in path.read_text().splitlines():
        if line.strip():
            digest, name = line.split(maxsplit=1)
            sums[name.lstrip("*")] = digest
    return sums


def render(version: str, checksums: Path) -> None:
    check_version(version)
    sums = read_checksums(checksums)
    source = f"system-steward-{version}.tar.gz"
    bundle = f"system-steward-{version}-windows-x64.zip"
    setup = f"system-steward-{version}-windows-x64-setup.exe"
    base = f"https://github.com/{REPO}/releases/download/v{version}"
    values = {
        "VERSION": version, "REPO": REPO, "HOMEPAGE": HOMEPAGE, "DESCRIPTION": DESCRIPTION, "LICENSE": LICENSE,
        "SOURCE_URL": f"{base}/{source}", "SOURCE_SHA256": sums[source],
        "BUNDLE_URL": f"{base}/{bundle}", "BUNDLE_SHA256": sums[bundle],
        "SETUP_URL": f"{base}/{setup}", "SETUP_SHA256": sums[setup].upper(),
    }
    blocks = []
    for name, pinned in requirements():
        url, digest = wheel(name, pinned)
        blocks.append(f'  resource "{name}" do\n    url "{url}"\n    sha256 "{digest}"\n  end\n')
    values["RESOURCES"] = "\n".join(blocks).rstrip("\n")

    outputs = {
        "system-steward.rb": [ROOT / "Formula/system-steward.rb"],
        "scoop.json": [ROOT / "bucket/system-steward.json", ROOT / "docs/system-steward.json"],
    }
    winget = ROOT / "packaging/winget/manifests/r/RajeshBudhathoki/SystemSteward" / version
    for template in ("RajeshBudhathoki.SystemSteward.yaml", "RajeshBudhathoki.SystemSteward.installer.yaml",
                     "RajeshBudhathoki.SystemSteward.locale.en-US.yaml"):
        outputs[template] = [winget / template]
    for template, targets in outputs.items():
        text = (TEMPLATES / template).read_text()
        for key, value in values.items():
            text = text.replace("{{" + key + "}}", value)
        leftover = re.findall(r"\{\{[A-Z_0-9]+\}\}", text)
        if leftover:
            raise SystemExit(f"{template}: unfilled placeholders {leftover}")
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            print(target.relative_to(ROOT))


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "source":
        build_source(argv[1], Path(argv[2]))
    elif len(argv) == 3 and argv[0] == "render":
        render(argv[1], Path(argv[2]))
    elif argv == ["version"]:
        print(app_version())
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
