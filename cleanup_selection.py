"""Group approved cleanup locations into explicit user-selectable units."""
from dataclasses import dataclass, replace
from pathlib import Path

from chrome_cleanup import CACHE_DIRS


@dataclass(frozen=True)
class CleanupGroup:
    label: str
    path: Path
    targets: tuple
    effect: str


def cleanup_groups(kind, plan, scope="cache"):
    buckets = {}
    if kind == "Adobe":
        for root in plan.cache_dirs:
            targets = tuple(path for path in plan.targets if path.parent == root)
            if targets:
                buckets[str(root)] = CleanupGroup(root.name, root, targets,
                    "Clears cache contents. Adobe rebuilds these files when needed. The cache folder itself stays.")
    else:
        for path in plan.targets:
            if path == plan.cache_root:
                key, label, root, cache = "mac-cache", "Browser cache · all profiles", path, True
            elif path.parent == plan.data_root:
                key, label, root, cache = "shared", "Shared rendering caches", plan.data_root, True
            else:
                profile = path.relative_to(plan.data_root).parts[0]
                cache = path.name in CACHE_DIRS
                key = profile + ("/cache" if cache else "/sites")
                label = profile + (" · caches" if cache else " · website data")
                root = plan.data_root / profile
            if not cache and scope == "cache":
                continue
            old = buckets.get(key)
            buckets[key] = CleanupGroup(label, root, (old.targets if old else ()) + (path,),
                "Clears downloaded browser caches. Websites may load more slowly the first time."
                if cache else "Signs you out of websites and removes cookies, databases, offline content and unsynced website data.")
    return sorted(buckets.values(), key=lambda group: (group.label.casefold(), str(group.path)))


def selection_plan(plan, groups, selected):
    """Restrict a plan to reviewed groups; never add paths outside the allowlist."""
    selected = set(selected)
    if not selected or any(not isinstance(index, int) or not 0 <= index < len(groups) for index in selected):
        raise ValueError("Select at least one valid cleanup group")
    targets = tuple(dict.fromkeys(path for index, group in enumerate(groups) if index in selected for path in group.targets))
    if not targets or not set(targets).issubset(set(plan.targets)):
        raise ValueError("The selection contains an unapproved cleanup path")
    return replace(plan, targets=targets)
