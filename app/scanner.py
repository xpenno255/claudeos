"""Host folder scanner — du-style breakdown of bind-mounted directories.

Mount host folders read-only under /scan in the container
(e.g. `- /home/xpenno255:/scan/home:ro`); each subdirectory of /scan
becomes a scannable root. CLAUDEOS_SCAN_BASE overrides the base path.
"""

import heapq
import os

SCAN_BASE = os.environ.get("CLAUDEOS_SCAN_BASE", "/scan")


def roots() -> list:
    """Scannable roots = immediate children of the scan base."""
    if not os.path.isdir(SCAN_BASE):
        return []
    out = []
    for e in sorted(os.listdir(SCAN_BASE)):
        p = os.path.join(SCAN_BASE, e)
        if os.path.isdir(p):
            out.append({"name": e, "path": p})
    return out


def _validate(path: str) -> str:
    real = os.path.realpath(path)
    base = os.path.realpath(SCAN_BASE)
    if real != base and not real.startswith(base + os.sep):
        raise ValueError("scan path must be inside the mounted /scan area")
    if not os.path.isdir(real):
        raise LookupError(f"not a directory: {path}")
    return real


def scan(path: str, list_depth: int = 3, top_dirs: int = 30, top_files: int = 20) -> dict:
    """Walk the tree once; return the biggest directories (full recursive
    sizes, like `du`) and the largest individual files."""
    root = _validate(path)
    dir_sizes = {}
    files_heap = []  # min-heap of (size, path), bounded to top_files
    skipped = 0

    def walk(p: str, depth: int) -> int:
        nonlocal skipped
        total = 0
        try:
            entries = os.scandir(p)
        except OSError:
            skipped += 1
            return 0
        with entries:
            for e in entries:
                try:
                    if e.is_symlink():
                        continue
                    if e.is_file(follow_symlinks=False):
                        sz = e.stat(follow_symlinks=False).st_size
                        total += sz
                        if len(files_heap) < top_files:
                            heapq.heappush(files_heap, (sz, e.path))
                        elif sz > files_heap[0][0]:
                            heapq.heapreplace(files_heap, (sz, e.path))
                    elif e.is_dir(follow_symlinks=False):
                        total += walk(e.path, depth + 1)
                except OSError:
                    skipped += 1
        if depth <= list_depth:
            dir_sizes[p] = total
        return total

    total = walk(root, 0)

    def rel(p: str) -> str:
        r = os.path.relpath(p, root)
        return "/" if r == "." else "/" + r

    dirs = [{"path": rel(p), "size": s}
            for p, s in sorted(dir_sizes.items(), key=lambda kv: -kv[1]) if p != root]
    files = [{"path": rel(p), "size": s}
             for s, p in sorted(files_heap, reverse=True)]
    return {"root": rel(root), "total": total, "skipped": skipped,
            "dirs": dirs[:top_dirs], "files": files}
