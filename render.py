# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Check a how-it-works map against the code it describes, then draw it as one HTML page.

    uv run render.py docs/how-it-works.json

Writes docs/how-it-works.html and a screenshot of each flow and each group. When the map breaks a rule, the
problems are listed, nothing is written, and the exit code is 1.
"""
from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

# Limits that keep the page a picture a person can hold in mind, not an inventory.
MAX_BOXES = 10          # boxes in one picture: the overview, or one group opened. More stop being readable
MAX_FLOWS = 6           # more questions than this belong on a second, narrower page
MAX_STEPS = 16          # a longer route means the parts are too fine or it is two questions
MAX_CITED_LINES = 40    # a step cites the call site, not the file around it
MAX_FILE_BYTES = 10 * 1024 * 1024   # bound cited source reads against huge or special files
MAX_MAP_BYTES = 2 * 1024 * 1024     # bound map JSON reads
MIN_SCALE = 0.6         # refuse a picture whose layout shrinks below readable size
GIT_TIMEOUT = 15        # seconds before giving up on a git subprocess
LIMITS = {
    "summary": 400, "question": 120, "label": 30, "does": 220, "verb": 28, "note": 280,
    "title": 80, "unknown": 400,
}
KINDS = ("code", "store", "actor")
# Headless Chrome takes the screenshots and checks the labels in a real browser. EXPLAIN_CODEBASE_CHROME names it
# (or "none" to skip); otherwise the usual install places are tried, and without it both steps are skipped with a note.
_ENV_CHROME = os.environ.get("EXPLAIN_CODEBASE_CHROME")
CHROME = (
    _ENV_CHROME if _ENV_CHROME and _ENV_CHROME.strip().lower() != "none" else
    next((c for c in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                      *filter(None, map(shutil.which, ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"))))
          if Path(c).exists()), "chrome-not-found")
)


class BrowserError(Exception):
    """Raised when a configured or discovered browser fails to complete the label check."""


class Cite(NamedTuple):
    path: str
    a: int | None
    b: int | None

    def __str__(self) -> str:
        if self.a is None:
            return self.path
        return f"{self.path}:{self.a}" + (f"-{self.b}" if self.b != self.a else "")


def _git_dir_for(root: Path) -> Path | None:
    """Resolve the git directory for repo root (.git folder or .git file's gitdir: line). Never follows a .git symlink."""
    dot_git = root / ".git"
    try:
        if dot_git.is_symlink():
            return None
        if dot_git.is_dir():
            return dot_git
        if dot_git.is_file():
            first = dot_git.read_bytes()[:4096].splitlines()[0].decode("utf-8").strip()
            if first.startswith("gitdir:"):
                target = first[7:].strip()
                if target and "\x00" not in target:
                    p = Path(target)
                    gd = (p if p.is_absolute() else (root.resolve() / p)).resolve()
                    return gd if gd.is_dir() else None
    except (OSError, ValueError, UnicodeDecodeError, IndexError):
        return None
    return None


def git(root: Path, *args: str, raw: bool = False) -> str | None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}
    cmd = [
        "git", "--no-pager", "--literal-pathspecs",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", "core.quotePath=false",
        "-c", "diff.external=",
        "-c", "gc.auto=0",
        "-c", "log.showSignature=false",
        "-c", "gpg.program=",
        "-c", "gpg.ssh.program=",
        "-c", "gpg.x509.program=",
        "-c", "protocol.allow=never",
        "-c", "protocol.file.allow=never",
        "-c", "protocol.ext.allow=never",
        "-c", "protocol.ssh.allow=never",
        "-c", "protocol.git.allow=never",
        "-c", "protocol.http.allow=never",
        "-c", "protocol.https.allow=never",
        "-C", str(root),
    ]
    try:
        if git_dir := _git_dir_for(root):
            cmd += [f"--git-dir={git_dir}", f"--work-tree={root}"]
        cmd.extend(args)
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT, env=env)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout if raw else r.stdout.rstrip("\n")


def find_root(map_path: Path) -> tuple[Path, bool]:
    """First ancestor of map_path holding .git (folder or file), else the map's project folder."""
    cur = map_path.parent
    while True:
        if _git_dir_for(cur) is not None:
            return cur, True
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    d = map_path.parent
    return (d.parent if d.name == "docs" else d), False


def _blob_status(f: Path, head_sha: str, alg: str, normalize: str, symlink: bool = False) -> str:
    """Compare f against head_sha without filters; return 'clean', 'dirty', or 'unverified' (if > MAX_FILE_BYTES)."""
    try:
        if symlink:
            data = os.fsencode(os.readlink(f))
            sha = hashlib.new(alg, f"blob {len(data)}\0".encode("ascii") + data).hexdigest()
            return "clean" if sha == head_sha else "dirty"
        st = f.lstat()
        if not stat.S_ISREG(st.st_mode):
            return "dirty"
        if st.st_size > MAX_FILE_BYTES:
            return "unverified"
        with f.open("rb") as fp:
            raw_data = fp.read(MAX_FILE_BYTES + 1)
        if len(raw_data) > MAX_FILE_BYTES:
            return "unverified"
    except OSError:
        return "dirty"
    if hashlib.new(alg, f"blob {len(raw_data)}\0".encode("ascii") + raw_data).hexdigest() == head_sha:
        return "clean"
    if normalize == "always" or (normalize == "auto" and b"\x00" not in raw_data[:8000]):
        norm = raw_data.replace(b"\r\n", b"\n")
        if hashlib.new(alg, f"blob {len(norm)}\0".encode("ascii") + norm).hexdigest() == head_sha:
            return "clean"
    return "dirty"


def _check_attrs(root: Path, files: list[str]) -> dict[str, dict[str, str]]:
    """Read text/eol/filter/binary gitattributes via git check-attr (runs no filters)."""
    out: dict[str, dict[str, str]] = {}
    if not files:
        return out
    res = git(root, "check-attr", "-z", "text", "eol", "filter", "binary", "--", *files, raw=True)
    if not res:
        return out
    toks = res.split("\0")
    for i in range(0, len(toks) - 2, 3):
        out.setdefault(toks[i], {})[toks[i + 1]] = toks[i + 2]
    return out


def _in_submodule(root: Path, rel: str, submodules: set[str]) -> bool:
    if rel.rstrip("/") in submodules:
        return True
    cur = Path(rel)
    for anc in cur.parents:
        if str(anc) == ".":
            break
        ap = anc.as_posix()
        if ap in submodules or _git_dir_for(root / anc) is not None:
            return True
    return False


def _dirty_cited(root: Path, paths: list[str], alg: str = "sha1") -> tuple[set[str], set[str], str | None]:
    """Return (dirty, unverified, git_error) for cited paths without running git status or filters."""
    if not paths:
        return set(), set(), None
    tree_raw = git(root, "ls-tree", "-r", "-z", "HEAD", "--", *paths, raw=True)
    idx_raw = git(root, "ls-files", "-s", "-z", "--", *paths, raw=True)
    others_raw = git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths, raw=True)
    if tree_raw is None or idx_raw is None or others_raw is None:
        return set(), set(), "git ls-tree/ls-files failed; could not verify cited files against HEAD"

    head_blobs: dict[str, tuple[str, str]] = {}
    for rec in filter(None, tree_raw.split("\0")):
        meta, _, rel = rec.partition("\t")
        parts = meta.split(" ")
        if len(parts) == 3 and rel:
            head_blobs[rel] = (parts[0], parts[2])
    idx_blobs: dict[str, set[str]] = defaultdict(set)
    for rec in filter(None, idx_raw.split("\0")):
        meta, _, rel = rec.partition("\t")
        parts = meta.split(" ")
        if len(parts) >= 2 and rel:
            idx_blobs[rel].add(parts[1])

    submodules = {rel for rel, (mode, _) in head_blobs.items() if mode == "160000"}
    dirty: set[str] = set()
    unverified: set[str] = set()
    for rel in filter(None, others_raw.split("\0")):
        (unverified if _in_submodule(root, rel, submodules) else dirty).add(rel)
    for p in paths:
        if p.endswith("/"):
            if _git_dir_for(root / p.rstrip("/")) is not None:
                unverified.add(p.rstrip("/"))
        elif p not in head_blobs:
            (unverified if _in_submodule(root, p, submodules) else dirty).add(p)

    tracked = sorted(set(head_blobs) | set(idx_blobs))
    attrs = _check_attrs(root, [r for r in tracked if r in head_blobs and head_blobs[r][0] not in ("120000", "160000")])
    autocrlf = (git(root, "config", "--get", "core.autocrlf") or "").strip().lower()
    for rel in tracked:
        if rel not in head_blobs:
            dirty.add(rel)
            continue
        mode, head_sha = head_blobs[rel]
        if idx_blobs.get(rel) != {head_sha}:
            dirty.add(rel)
            continue
        if mode == "160000":
            unverified.add(rel)
            continue
        a = attrs.get(rel, {})
        filtered = a.get("filter", "unspecified") not in ("unspecified", "unset")
        if a.get("binary") == "set" or a.get("text") == "unset":
            norm = "never"
        elif a.get("text") == "set" or a.get("eol", "unspecified") not in ("unspecified", "unset"):
            norm = "always"
        elif a.get("text") == "auto" or autocrlf in ("true", "input"):
            norm = "auto"
        else:
            norm = "never"
        status = _blob_status(root / rel, head_sha, alg, norm, symlink=(mode == "120000"))
        if status == "unverified" or (status == "dirty" and filtered):
            unverified.add(rel)
        elif status == "dirty":
            dirty.add(rel)
    return dirty, unverified, None


def _count_lines(f: Path, limit: int = MAX_FILE_BYTES) -> int | None:
    """Count lines in regular file f in bounded chunks; return None if non-regular or over limit bytes."""
    try:
        st = f.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
        return None
    total = lines = 0
    last = b""
    try:
        with f.open("rb") as fp:
            while chunk := fp.read(65536):
                total += len(chunk)
                if total > limit:
                    return None
                lines += chunk.count(b"\n")
                last = chunk[-1:]
    except OSError:
        return None
    return lines + (1 if total and last != b"\n" else 0)


_ALLOWED_CF = frozenset("\u00ad\u061c\u200c\u200d\u200e\u200f")
_BIDI_BAD = frozenset(range(0x202A, 0x202F)) | frozenset(range(0x2066, 0x206A))


def _is_generic_mark(cp: int) -> bool:
    return (0x0300 <= cp <= 0x036F or 0x1AB0 <= cp <= 0x1AFF or 0x1DC0 <= cp <= 0x1DFF
            or 0x20D0 <= cp <= 0x20FF or 0xFE20 <= cp <= 0xFE2F)


def _is_unbounded_script(cp: int) -> bool:
    return (0x12000 <= cp <= 0x1254F or 0x13000 <= cp <= 0x1345F
            or 0x14400 <= cp <= 0x1467F or 0x1D800 <= cp <= 0x1DAAF)


def _emoji_tag_spans(s: str) -> set[int]:
    """Indices of tag characters (U+E0020-E007F) inside valid UTS #51 emoji tag sequences (U+1F3F4 ... U+E007F)."""
    ok: set[int] = set()
    i, n = 0, len(s)
    while i < n:
        if ord(s[i]) == 0x1F3F4:
            j = i + 1
            while j < n and 0xE0020 <= ord(s[j]) <= 0xE007E:
                j += 1
            if j > i + 1 and j < n and ord(s[j]) == 0xE007F:
                ok.update(range(i + 1, j + 1))
                i = j + 1
                continue
        i += 1
    return ok


def _bad_text(s: str, check_limits: bool = True) -> str | None:
    """Return a reason if s contains disallowed control/bidi/surrogate/private-use chars or unbounded marks/scripts."""
    tag_ok = _emoji_tag_spans(s)
    marks = gen_marks = 0
    for i, ch in enumerate(s):
        cp = ord(ch)
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cs", "Co", "Zl", "Zp") or cp in _BIDI_BAD:
            return f"contains disallowed control, bidi, surrogate or private-use character U+{cp:04X}"
        if cat == "Cf":
            if ch in _ALLOWED_CF or i in tag_ok:
                continue
            return f"contains disallowed format character U+{cp:04X}"
        if not check_limits:
            continue
        if cat[0] == "M":
            marks += 1
            if _is_generic_mark(cp):
                gen_marks += 1
            if gen_marks > 2:
                return "has more than two combining marks on one character"
            if marks > 4:
                return "has more than four combining marks on one character"
            continue
        marks = gen_marks = 0
        if _is_unbounded_script(cp):
            return f"contains character U+{cp:04X} whose width cannot be bounded"
    return None


# ---------------------------------------------------------------- checking

def check(m: dict, root: Path, in_git: bool) -> tuple[list[str], dict]:
    errs: list[str] = []
    bad = errs.append
    cited_by: dict[str, list[str]] = defaultdict(list)
    line_counts: dict[str, int] = {}
    dir_entries: dict[Path, tuple[set[str], set[str]]] = {}
    root_real = root.resolve()

    def exact_disk_case(parent: Path, name: str) -> bool:
        cached = dir_entries.get(parent)
        if cached is None:
            try:
                raw_names = set(os.listdir(parent))
            except OSError:
                return False
            cached = (raw_names, {unicodedata.normalize("NFC", n) for n in raw_names})
            dir_entries[parent] = cached
        return name in cached[0] or unicodedata.normalize("NFC", name) in cached[1]

    def words(obj: dict, key: str, where: str, required: bool = True) -> str:
        v = obj.get(key)
        if v is None and not required:
            return ""
        if not isinstance(v, str) or not v.strip():
            bad(f'{where}: add "{key}" (text)')
            return ""
        if len(v) > LIMITS[key]:
            bad(f'{where}: "{key}" is {len(v)} characters; say it in {LIMITS[key]} or fewer')
        if reason := _bad_text(v):
            bad(f'{where}: "{key}" {reason}')
        return v

    def cite(s: object, where: str, step: bool) -> Cite | None:
        if not isinstance(s, str):
            bad(f"{where}: {s!r} is not a citation; write path:start-end")
            return None
        if reason := _bad_text(s, check_limits=False):
            bad(f"{where}: {s!r} {reason}")
            return None
        mt = re.fullmatch(r"([^:]+?)(?::(\d{1,9})(?:-(\d{1,9}))?)?", s)
        if not mt:
            bad(f"{where}: {s!r} is not a citation; write path:start-end")
            return None
        path, a = mt[1], int(mt[2]) if mt[2] else None
        b = int(mt[3]) if mt[3] else a
        p_obj = Path(path)
        if path.startswith(("/", "~")) or p_obj.is_absolute() or ".." in p_obj.parts or not p_obj.parts:
            bad(f"{where}: {path} must be a path relative to the repository root")
            return None
        cur = root
        for part in p_obj.parts:
            nxt = cur / part
            try:
                if nxt.is_symlink():
                    bad(f"{where}: {path} traverses or names a symlink, which cannot be cited")
                    return None
                if nxt.exists() and not exact_disk_case(cur, part):
                    bad(f"{where}: {path} does not match the exact case of a path in {root.name}")
                    return None
            except (OSError, ValueError):
                bad(f"{where}: {path} cannot be resolved")
                return None
            cur = nxt
        f = root / path
        try:
            real = f.resolve()
        except (OSError, ValueError):
            bad(f"{where}: {path} cannot be resolved")
            return None
        if not real.is_relative_to(root_real):
            bad(f"{where}: {path} resolves outside {root.name}")
            return None
        norm_path = "/".join(p_obj.parts)
        if real.is_dir() and not step and a is None:
            c = Cite(norm_path + "/", None, None)
        elif not real.is_file():
            bad(f"{where}: {path} is not a file in {root.name}")
            return None
        elif a is None:
            if step:
                bad(f"{where}: cite the lines that show the step ({norm_path}:start-end)")
                return None
            c = Cite(norm_path, None, None)
        else:
            if norm_path not in line_counts:
                counted = _count_lines(real, MAX_FILE_BYTES)
                if counted is None:
                    bad(f"{where}: {path} is too large or not a regular file (limit {MAX_FILE_BYTES} bytes)")
                    return None
                line_counts[norm_path] = counted
            n = line_counts[norm_path]
            if not 1 <= a <= b <= n:
                bad(f"{where}: {path} has {n} lines; {a}-{b} is outside it")
                return None
            if step and b - a + 1 > MAX_CITED_LINES:
                bad(f"{where}: {s} spans {b - a + 1} lines; cite the call site, at most {MAX_CITED_LINES} lines")
                return None
            c = Cite(norm_path, a, b)
        cited_by[c.path].append(where)
        return c

    def owns(part: dict, path: str) -> bool:
        return any(path == c.path or (c.path.endswith("/") and path.startswith(c.path)) for c in part["_src"])

    if not isinstance(m, dict):
        return ["the map must be a JSON object"], {}
    words(m, "summary", "the map")
    words(m, "title", "the map", required=False)
    raw_parts = m.get("parts")
    if not isinstance(raw_parts, list):
        bad('"parts" must be a list of part objects')
        parts: list = []
    else:
        parts = raw_parts
    raw_flows = m.get("flows")
    if not isinstance(raw_flows, list):
        bad('"flows" must be a list of flow objects')
        flows: list = []
    else:
        flows = raw_flows
    if not 1 <= len(flows) <= MAX_FLOWS:
        bad(f"the map has {len(flows)} flows; it needs 1 to {MAX_FLOWS}")

    by: dict[str, dict] = {}
    for i, p in enumerate(parts):
        if not isinstance(p, dict):
            bad(f"part {i + 1}: must be an object")
            continue
        pid = p.get("id")
        if not isinstance(pid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", pid) or pid in by:
            bad(f"part {i + 1}: \"id\" must be unique, lowercase letters, digits, - or _ (got {pid!r})")
            continue
        where = f'part "{pid}"'
        by[pid] = p
        words(p, "label", where)
        p_kind = p.get("kind")
        if p_kind is None:
            p["kind"] = "code"
        elif not isinstance(p_kind, str) or p_kind not in KINDS:
            bad(f'{where}: "kind" is one of {", ".join(KINDS)}')
            p["kind"] = "code"
        raw_src = p.get("src")
        if raw_src is None:
            src_list: list = []
        elif isinstance(raw_src, str):
            src_list = [raw_src]
        elif isinstance(raw_src, list):
            src_list = raw_src
        else:
            bad(f'{where}: "src" must be a citation string or list of citations')
            src_list = []
        p["_src"] = [c for s in src_list if (c := cite(s, where, step=False))]
        if p["kind"] == "actor" and p["_src"]:
            bad(f"{where}: an actor is outside the repository and cites nothing; code that starts things has kind code")
        words(p, "does", where, required=p["kind"] != "actor")

    groups: dict[str, dict] = {}
    raw_groups = m.get("groups")
    if raw_groups is not None and not isinstance(raw_groups, list):
        bad('"groups" must be a list of group objects')
        raw_groups = []
    for i, g in enumerate(raw_groups or []):
        if not isinstance(g, dict):
            bad(f"group {i + 1}: must be an object")
            continue
        gid = g.get("id")
        if not isinstance(gid, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", gid) or gid in groups or gid in by:
            bad(f"group {i + 1}: \"id\" must differ from every part and group, start with a letter, and use lowercase "
                f"letters, digits, - or _ (got {gid!r})")
            continue
        groups[gid] = g
        words(g, "label", f'group "{gid}"')
        words(g, "does", f'group "{gid}"')
        g["_parts"] = [pid for pid, p in by.items() if isinstance(p.get("group"), str) and p.get("group") == gid]
        if not 2 <= len(g["_parts"]) <= MAX_BOXES:
            bad(f'group "{gid}" has {len(g["_parts"])} parts; a group holds 2 to {MAX_BOXES}')
    for pid, p in by.items():
        g_ref = p.setdefault("group", None)
        if g_ref is not None and (not isinstance(g_ref, str) or g_ref not in groups):
            bad(f'part "{pid}": "group" names no group (got {g_ref!r})')
            p["group"] = None
    top = len(groups) + sum(1 for p in by.values() if not p["group"])
    if top > MAX_BOXES:
        bad(f"the overview would show {top} boxes and stays readable up to {MAX_BOXES}: put parts that work together "
            f'into "groups" (the reader opens a group to see its parts), or merge helpers into the part that uses them')

    used: set[str] = set()
    crossing = False                        # some step joins two boxes of the overview
    for fi, f in enumerate(flows):
        fw = f"flow {fi + 1}"
        if not isinstance(f, dict):
            bad(f"{fw}: must be an object with a question and steps")
            continue
        words(f, "question", fw)
        raw_steps = f.get("steps")
        if not isinstance(raw_steps, list):
            bad(f'{fw}: "steps" must be a list of step objects')
            steps: list = []
        else:
            steps = raw_steps
        f["steps"] = steps
        if not 1 <= len(steps) <= MAX_STEPS:
            bad(f"{fw} has {len(steps)} steps; it needs 1 to {MAX_STEPS} (coarser parts, or split the question)")
        reached: set[str] = set()
        valid_steps: list[dict] = []
        for si, s in enumerate(steps):
            sw = f"{fw}, step {si + 1}"
            if not isinstance(s, dict):
                bad(f"{sw}: must be an object")
                continue
            a, b = s.get("from"), s.get("to")
            if not isinstance(a, str) or not isinstance(b, str) or a not in by or b not in by or a == b:
                bad(f'{sw}: "from" and "to" must name two different parts (got {a!r} to {b!r})')
                continue
            if si and not {a, b} & reached:     # a consumer may pick up from a store the flow has reached
                bad(f'{sw} joins "{a}" and "{b}", neither of which this flow has reached yet; a step is missing before it')
            reached |= {a, b}
            used |= {a, b}
            crossing = crossing or (by[a]["group"] or a) != (by[b]["group"] or b)
            words(s, "verb", sw)
            words(s, "note", sw, required=False)
            c = cite(s["src"], sw, step=True) if s.get("src") is not None else None
            if c:
                owner = by[a] if by[a]["_src"] else by[b]
                if not owner["_src"]:
                    bad(f"{sw}: both parts are outside the repository, so no code here can show this step; drop its src")
                elif not owns(owner, c.path):
                    bad(f'{sw}: {c.path} is not among the code cited by "{owner["id"]}". The part that acts owns the '
                        f"step: cite the call site in its code (the receiving code when the actor is outside the "
                        f'repository), or add the file to that part\'s "src" if it belongs there.')
            s["_src"] = c
            valid_steps.append(s)
        f["steps"] = valid_steps
    for pid in by:
        if pid not in used:
            bad(f'part "{pid}" is in no flow; remove it or add the step that uses it')
    if groups and used and not crossing:
        bad("every step is inside one group, so the overview would show no arrows; drop the group or split it")
    # Every opened group picture (its parts plus the boxes beside it) must also fit within MAX_BOXES.
    for gid, g in groups.items():
        mine = set(g["_parts"])
        near = {e for f in flows if isinstance(f, dict) for s in f.get("steps", [])
                if {s["from"], s["to"]} & mine for e in (s["from"], s["to"])}
        beside = {by[e]["group"] or e for e in near if e not in mine and e in by}
        opened = len(mine) + len(beside)
        if opened > MAX_BOXES:
            bad(f'group "{gid}" would show {opened} boxes ({len(mine)} inside, {len(beside)} beside) and stays '
                f"readable up to {MAX_BOXES}: split the group or merge parts")
    raw_unknowns = m.get("unknowns")
    if raw_unknowns is not None:
        if not isinstance(raw_unknowns, list):
            bad('"unknowns" is a list of sentences')
        else:
            for ui, u in enumerate(raw_unknowns):
                if not isinstance(u, str) or not u.strip():
                    bad(f'unknown {ui + 1}: must be non-empty text')
                else:
                    if len(u) > LIMITS["unknown"]:
                        bad(f'unknown {ui + 1}: is {len(u)} characters; say it in {LIMITS["unknown"]} or fewer')
                    if reason := _bad_text(u):
                        bad(f'unknown {ui + 1}: {reason}')

    ctx = {"groups": groups, "commit": None, "when": date.today().isoformat(),
           "dirty": set(), "unverified": set(), "base": None}
    if in_git:
        head = git(root, "rev-parse", "HEAD")
        commit = m.get("commit")
        is_hex = isinstance(commit, str) and bool(re.fullmatch(r"[0-9a-fA-F]{7,64}", commit))
        full = git(root, "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}") if is_hex else None
        if not full or not full.lower().startswith(commit.lower()):
            bad(f'set "commit" to a 7-64 character hex commit SHA the code was read at (HEAD is {head})')
        else:
            paths = sorted(cited_by)
            if paths:
                tracked_raw = git(root, "ls-files", "-z", raw=True)
                if tracked_raw is None:
                    bad("git ls-files failed; could not verify cited path casing")
                else:
                    tracked = {p for p in tracked_raw.split("\0") if p}
                    tracked_fold = {p.casefold(): p for p in tracked}
                    for p in paths:
                        if not p.endswith("/") and p not in tracked and (match := tracked_fold.get(p.casefold())):
                            bad(f'{p}: does not match the exact case of tracked file "{match}"')
                moved = git(root, "diff", "--no-ext-diff", "--no-textconv", "--name-only", "-z", full, "HEAD", "--", *paths, raw=True)
                if moved is None:
                    bad("git diff failed; could not check whether cited files changed since the recorded commit")
                else:
                    for path in filter(None, moved.split("\0")):
                        who = sorted({w for p, ws in cited_by.items() if path == p or (p.endswith("/") and path.startswith(p)) for w in ws})
                        bad(f'{path} changed since {full[:7]}: re-read it (cited by {"; ".join(who)}), correct the map, '
                            f'then set "commit" to {head}')
            alg = "sha256" if len(full) == 64 else "sha1"
            dirty, unverified, dirty_err = _dirty_cited(root, paths, alg)
            if dirty_err:
                bad(dirty_err)
            when = git(root, "show", "-s", "--no-ext-diff", "--no-textconv", "--format=%cs", "--end-of-options", full)
            ctx.update(commit=full, when=when or ctx["when"],
                       dirty=dirty, unverified=unverified, base=github_base(root, full))
    return errs, ctx


def github_base(root: Path, commit: str) -> str | None:
    """Web address of the repository, only when the commit can be opened there."""
    url = git(root, "remote", "get-url", "--", "origin") or ""
    mt = re.fullmatch(
        r"(?:git@github\.com:|(?:https|ssh)://(?:[^@/]+@)?github\.com/)([A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*?)(?:\.git)?/?",
        url,
    )
    if not mt or ".." in mt[1].split("/") or not git(root, "branch", "-r", f"--contains={commit}"):
        return None
    return f"https://github.com/{mt[1]}"


# ---------------------------------------------------------------- layout

_CHAR_W = {
    **dict.fromkeys("ijl'., ", 0.27),
    **dict.fromkeys("ftr()[]-!IJ:;|/\\", 0.36),
    **dict.fromkeys("csz", 0.47),
    **dict.fromkeys("kvxy", 0.51),
    **dict.fromkeys("0123456789", 0.59),
    **dict.fromkeys("DGHNOQU", 0.78),
    **dict.fromkeys("#+<=>^~&→←↑↓↔", 0.76),
    **dict.fromkeys("@%…—", 0.95),
    "m": 0.89, "w": 0.76, "M": 0.90, "W": 0.99,
    "\u2001": 1.05, "\u2003": 1.05, "\u2030": 1.6, "\u2031": 2.1,
    "\u2a0c": 1.6, "\u2b33": 1.7,
    "\u2e3a": 2.4, "\u2e3b": 3.5,
    "\ufdfa": 9.0, "\ufdfb": 5.0, "\ufdfc": 1.5, "\ufdfd": 7.5,
}


def _glyph_w(ch: str, expand: bool = True) -> float:
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Cf"):
        return 0.0
    if ch in _CHAR_W:
        return _CHAR_W[ch]
    if ord(ch) >= 0x1F000 or cat == "So":
        return 1.35
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 1.08
    if cat == "Me":
        return 1.0
    if expand and ord(ch) > 127:
        decomp = unicodedata.normalize("NFKC", ch)
        if len(decomp) > 1:
            return max(0.68, sum(_glyph_w(d, expand=False) for d in decomp))
    if ch.isupper():
        return 0.65
    return 0.68 if ord(ch) > 127 else 0.59


def text_w(s: str, size: float, bold: bool = False) -> float:
    """Estimated width of s in the page's primary fonts (Avenir Next, Segoe UI), keeping wide CJK/emoji/symbols and bold."""
    return sum(_glyph_w(ch) for ch in s) * size * (1.11 if bold else 1.0)


def step_w(s: str, size: float = 12.0) -> float:
    """Width of a step label line ('N verb'), accounting for the bold step-number prefix."""
    mt = re.match(r"^(\d+ )(.*)", s)
    if mt:
        return text_w(mt[1], size, bold=True) + text_w(mt[2], size)
    return text_w(s, size)


def wrap(label: str) -> list[str]:
    ws = label.split()
    if len(label) <= 14 or len(ws) < 2:
        return [label]
    i = min(range(1, len(ws)), key=lambda k: abs(len(" ".join(ws[:k])) - len(" ".join(ws[k:]))))
    return [" ".join(ws[:i]), " ".join(ws[i:])]


GAP_X, GAP_Y, PAD = 30, 78, 14
ROW_W = 900     # the widest row of parts that stays readable beside the text; a wider row is staggered
PORT = 28       # room an arrow needs where it meets a part
HUB = 4         # a part that this many arrows meet on one side becomes a bar across the parts they join
FS, FA = 15, 12 # type sizes: a part's name, an arrow's label
LINE = 15       # height of one line of an arrow's label
TALL = 590      # the height a picture can have and still fit a laptop browser window at full size
WIDE = 820      # the width the picture has beside the text in that window
SUB = 17        # height of the small second line a group carries ("4 parts")
MARGIN = 3.0    # clear space every visible arrow label must keep from boxes, arrows and other labels
MARK_HALF = 4.5 # half the open question's 9 px yellow highlighter (.arrow .mark in page.html)
ARROW_MARGIN = MARGIN + MARK_HALF   # clearance from an open-flow arrow centerline to any visible label
LABEL_GAP = 9.0 # horizontal offset from arrow centerline to label edge (4.5 px beyond highlighter)


class Breach(NamedTuple):
    picture: str
    flow: int
    a: tuple[str, object]
    b: tuple[str, object]


class LayoutError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("\n".join(errors))
        self.errors = errors


def bez(x1: float, x2: float, t: float) -> float:
    """Where an arrow's curve between two rows is across, a fraction t of the way down."""
    u = 1 - t
    return (u ** 3 + 3 * u * u * t) * x1 + (3 * u * t * t + t ** 3) * x2


def bez_y(y1: float, y2: float, t: float) -> float:
    """True y coordinate of SVG cubic Bezier C x1,my x2,my x2,y2 with my = (y1 + y2) / 2."""
    return y1 + (y2 - y1) * (1.5 * t * (1 - t) + t ** 3)


def label_box(lx: float, ly: float, side: int, size: tuple[float, float]) -> tuple[float, float, float, float]:
    """Left, right, top, bottom of a label beside its arrow (or on it, if side == 0)."""
    x0 = lx - size[0] / 2 if side == 0 else lx + LABEL_GAP if side > 0 else lx - LABEL_GAP - size[0]
    return x0, x0 + size[0], ly - size[1] / 2, ly + size[1] / 2


def rects_near(a: tuple[float, float, float, float], b: tuple[float, float, float, float], m: float = MARGIN) -> bool:
    """Whether two (x0, x1, y0, y1) rectangles come closer than m px."""
    return a[0] < b[1] + m and b[0] < a[1] + m and a[2] < b[3] + m and b[2] < a[3] + m


def _inv_bez_y(u: float) -> float:
    """Solve 1.5*t*(1-t) + t^3 = u for t in [0, 1]; 4 Newton steps reach float precision."""
    t = u
    for _ in range(4):
        t -= (1.5 * t * (1.0 - t) + t * t * t - u) / (1.5 - 3.0 * t + 3.0 * t * t)
    return 0.0 if t < 0.0 else 1.0 if t > 1.0 else t


def curve_x_at_y(x1: float, y1: float, x2: float, y2: float, target_y: float) -> float:
    """Horizontal position of the cubic Bezier C x1,my x2,my x2,y2 at vertical coordinate target_y."""
    if x1 == x2 or target_y <= y1:
        return x1
    if target_y >= y2:
        return x2
    return bez(x1, x2, _inv_bez_y((target_y - y1) / (y2 - y1)))


def piece_hits_rect(x1: float, y1: float, x2: float, y2: float, curve: bool,
                    rx0: float, rx1: float, ry0: float, ry1: float) -> bool:
    """Whether one monotonic arrow piece (y1 <= y2) enters the open rectangle (rx0, rx1) x (ry0, ry1)."""
    if y2 <= ry0 or y1 >= ry1:
        return False
    lo_x, hi_x = (x1, x2) if x1 <= x2 else (x2, x1)
    if hi_x <= rx0 or lo_x >= rx1:
        return False
    if not curve or x1 == x2:
        return True
    ya = y1 if y1 > ry0 else ry0
    yb = y2 if y2 < ry1 else ry1
    xa = curve_x_at_y(x1, y1, x2, y2, ya)
    xb = curve_x_at_y(x1, y1, x2, y2, yb)
    return (xa if xa >= xb else xb) > rx0 and (xa if xa <= xb else xb) < rx1


def _overlap_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float], m: float = MARGIN) -> float:
    ox = min(a[1], b[1] + m) - max(a[0], b[0] - m)
    oy = min(a[3], b[3] + m) - max(a[2], b[2] - m)
    return ox * oy if ox > 0 and oy > 0 else 0.0


def _bbox_of_pieces(pieces: list[tuple[float, float, float, float, bool]],
                    hb: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    xs = [hb[0], hb[1]]
    ys = [hb[2], hb[3]]
    for x1, y1, x2, y2, _ in pieces:
        xs.extend((x1, x2))
        ys.extend((y1, y2))
    return min(xs), max(xs), min(ys), max(ys)


def arrow_near(g_arrow: dict, rect: tuple[float, float, float, float], m: float = MARGIN) -> bool:
    """Whether an arrow's highlighter path (margin + MARK_HALF) or arrowhead (margin) comes within m px of rect."""
    am = m + MARK_HALF
    if not rects_near(rect, g_arrow["bbox"], am):
        return False
    if rects_near(rect, g_arrow["head_box"], m):
        return True
    rx0, rx1, ry0, ry1 = rect[0] - am, rect[1] + am, rect[2] - am, rect[3] + am
    return any(piece_hits_rect(x1, y1, x2, y2, curve, rx0, rx1, ry0, ry1)
               for x1, y1, x2, y2, curve in g_arrow["pieces"])


def _spot_for(g_arrow: dict, fi: int) -> tuple[float, float, int]:
    """Label anchor (lx, ly, side) for flow fi, falling back to the arrow's shared anchor."""
    ls = g_arrow.get("labels")
    if ls is not None and fi < len(ls) and ls[fi] is not None:
        return ls[fi]
    return g_arrow["label"]


def _static_hits(lb: tuple[float, float, float, float], side: int,
                 boxes: dict[str, tuple[float, float, float, float]],
                 flow_arrows: list[tuple[object, dict]], own_key: object,
                 bounds: tuple[float, float, float, float] | None = None,
                 top_min: float | None = None) -> list[tuple[str, object, float]]:
    """Single source of truth for label clearance against picture bounds, part boxes, own arrow and flow arrows."""
    hits: list[tuple[str, object, float]] = []
    if bounds is not None:
        vx0, vx1, vy0, vy1 = bounds
        if lb[0] < vx0 or lb[1] > vx1 or lb[2] < vy0 or lb[3] > vy1:
            hits.append(("edge", "viewBox", 100.0))
    elif top_min is not None and lb[2] < top_min:
        hits.append(("edge", "top", (top_min - lb[2]) * (lb[1] - lb[0])))
    for pid, pb in boxes.items():
        if rects_near(lb, pb, MARGIN):
            hits.append(("part", pid, _overlap_area(lb, pb, MARGIN)))
    for key, g_arr in flow_arrows:
        if (key == own_key and side == 0) or arrow_near(g_arr, lb, MARGIN):
            hits.append(("arrow", key, 100.0))
    return hits


def breaches(geo: dict, arrows: dict[tuple[str, str], dict], picture: str = "") -> list[Breach]:
    """Every breach of the label-clearance rule in one laid-out picture.

    In each flow, every visible arrow label must stay inside the viewBox and keep at least MARGIN px clear of:
    1. every other visible label in that flow,
    2. every part box in the picture (including dimmed ones),
    3. every arrow drawn in that flow (including half its highlighter stroke), and
    4. its own arrow (side == 0 is forbidden).
    """
    out: list[Breach] = []
    n_flows = max((len(arr["label_sizes"]) for arr in arrows.values()), default=0)
    boxes, g_arrows = geo["boxes"], geo["arrows"]
    vx0, vy0 = geo.get("x0", 0.0), geo.get("y0", 0.0)
    bounds = (vx0, vx0 + geo["w"], vy0, vy0 + geo["h"])
    for fi in range(n_flows):
        shown = [(pair, arr["label_sizes"][fi]) for pair, arr in arrows.items()
                 if fi < len(arr["label_sizes"]) and arr["label_sizes"][fi]]
        flow_arrows = [(pair, g_arrows[pair]) for pair, _ in shown]
        lboxes = {pair: label_box(*_spot_for(g_arrows[pair], fi), sz) for pair, sz in shown}
        for i, (p1, _) in enumerate(shown):
            lb = lboxes[p1]
            for kind, target, _ in _static_hits(lb, _spot_for(g_arrows[p1], fi)[2], boxes, flow_arrows, p1, bounds=bounds):
                out.append(Breach(picture, fi, ("label", p1), (kind, target)))
            for p2, _ in shown[i + 1:]:
                if rects_near(lb, lboxes[p2], MARGIN):
                    out.append(Breach(picture, fi, ("label", p1), ("label", p2)))
    return out


def _format_clash(picture: str, flow: int, kind: str, a_arr: str, a_txt: str,
                  b_val: str, b_txt: str = "", mode: str = "", suffix: str = "") -> str:
    """Format one label or part-text clearance breach line shared by layout and browser checks."""
    where = f'group "{picture}"' if picture else "overview"
    head = f"{where}, question {flow + 1}" + (f" ({mode})" if mode else "")
    if kind == "part_text":
        return f'{head}: part "{a_arr}" text ({a_txt}) overflows its box'
    a_desc = f"{a_arr} ({a_txt})" if a_txt else a_arr
    if kind == "min_font":
        if b_val == a_arr:
            return f'{head}: part "{a_arr}" text ({a_txt}) would shrink below 9 px{suffix}'
        return f"{head}: label on {a_desc} would shrink below 9 px{suffix}"
    if kind == "label":
        hit = f"label on {b_val}" + (f" ({b_txt})" if b_txt else "")
    elif kind == "part":
        hit = f'part "{b_val}"'
    elif kind == "edge":
        hit = "the edge of the picture"
    elif b_val == a_arr:
        hit = f"its own arrow ({a_arr})"
    else:
        hit = f"arrow {b_val}" + (f" ({b_txt})" if b_txt else "")
    return f"{head}: label on {a_desc} touches {hit}{suffix}"


def format_breach(b: Breach, arrows: dict[tuple[str, str], dict]) -> str:
    """Error line naming picture, question number, steps involved, and how to fix the map."""
    def shown_text(pair: tuple[str, str]) -> str:
        shown = arrows.get(pair, {}).get("shown", [])
        return ", ".join(shown[b.flow]) if b.flow < len(shown) and shown[b.flow] else ""

    _, p1 = b.a
    kind_b, val_b = b.b
    is_pair = isinstance(val_b, tuple)
    return _format_clash(
        b.picture, b.flow, kind_b,
        f"{p1[0]}->{p1[1]}", shown_text(p1),
        f"{val_b[0]}->{val_b[1]}" if is_pair else str(val_b),
        shown_text(val_b) if is_pair else "",
        suffix=" — use fewer steps on that arrow, shorter verbs, or a group",
    )


def choose_layout(parts: list[dict], arrows: dict[tuple[str, str], dict], picture: str = "") -> tuple[dict, list[Breach]]:
    """Pick the larger clean layout between hub-isolated and non-isolated, else return the fewest-breach fallback."""
    tries = []
    for iso in (True, False):
        geo = layout(parts, arrows, iso)
        tries.append((geo, breaches(geo, arrows, picture)))
    clean = [(g, bs) for g, bs in tries if not bs]
    if clean:
        return max(clean, key=lambda pair: scale(pair[0]))
    return min(tries, key=lambda pair: (len(pair[1]), -scale(pair[0])))


def _scale_wh(w: float, h: float) -> float:
    return round(min(1.2, WIDE / w, TALL / h), 2)


def scale(geo: dict) -> float:
    """How large a picture is drawn in the window: a layout that keeps it larger keeps its words larger."""
    return _scale_wh(geo["w"], geo["h"])


def _isotonic_pool(targets: list[tuple[float, float]], off: list[float]) -> list[float]:
    """Pool adjacent items toward (position, weight) while keeping pos[i] - pos[i - 1] >= off[i] - off[i - 1]."""
    blocks: list[list[float]] = []
    for (v, w), o in zip(targets, off):
        blocks.append([(v - o) * w, w, 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, w2, c = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += w2
            blocks[-1][2] += c
    out: list[float] = []
    i = 0
    for s, w2, c in blocks:
        for _ in range(int(c)):
            out.append(s / w2 + off[i])
            i += 1
    return out


def _spread_bar_ports(want_x: list[float], lo: float, hi: float, step: float) -> list[float]:
    """Keep bar ports in order and at least step px apart inside [lo, hi], moving only colliding ports."""
    n = len(want_x)
    if n <= 1:
        return list(want_x)
    step = min(step, (hi - lo) / (n - 1))
    off = [i * step for i in range(n)]
    hi_base = hi - off[-1]
    return [min(max(p - o, lo), hi_base) + o for p, o in zip(_isotonic_pool([(v, 1.0) for v in want_x], off), off)]


def _part_w(words: list[str], has_sub: bool, ports: int, port_gap: float = PORT) -> float:
    """Minimum width of a part box for its label text, optional subtitle and port spacing."""
    return max(132.0 if has_sub else 96.0, max(text_w(ln, FS) for ln in words) * 1.06 + 38,
               32 + port_gap * (ports - 1))


def _topology(parts: list[dict], arrows: dict[tuple[str, str], dict], isolate: bool) -> dict:
    """Compute cycle flipping, ranks, hub isolation, crossing-minimised layers and chains once per picture."""
    ids = [p["id"] for p in parts]
    pairs = list(arrows)
    real = lambda n: isinstance(n, str)     # the other nodes are waypoints of long arrows

    out = defaultdict(list)
    for a, b in pairs:
        out[a].append(b)
    flipped, state = set(), {}

    def visit(u: str) -> None:              # an arrow that closes a loop is laid out upwards
        state[u] = 1
        for v in out[u]:
            if state.get(v) == 1:
                flipped.add((u, v))
            elif v not in state:
                visit(v)
        state[u] = 2

    for i in ids:
        if i not in state:
            visit(i)
    down = [((b, a) if (a, b) in flipped else (a, b)) for a, b in pairs]
    n_top, n_bot = defaultdict(int), defaultdict(int)
    for a, b in down:
        n_bot[a] += 1
        n_top[b] += 1

    kind = {p["id"]: p["kind"] for p in parts}
    base_words = {p["id"]: wrap(p["label"]) for p in parts}
    sub = {p["id"]: p.get("sub") for p in parts}
    opens = {p["id"]: bool(p.get("opens")) for p in parts}
    base_w = {i: _part_w(base_words[i], bool(sub[i]), max(n_top[i], n_bot[i])) for i in ids}

    def settle(floor: dict) -> dict:        # each part below everything that acts on it, never above its floor
        rank = dict(floor)
        for _ in ids:
            for a, b in down:
                rank[b] = max(rank[b], rank[a] + 1)
        return rank

    rank = settle({i: 0 for i in ids})
    for i in ids:                           # a part nothing points at sits just above what it acts on
        below = [rank[b] for a, b in down if a == i]
        if below and not n_top[i]:
            rank[i] = min(below) - 1
    dense = {r: k for k, r in enumerate(sorted(set(rank.values())))}
    rank = {i: dense[r] for i, r in rank.items()}

    hubs = {i: "b" if n_bot[i] >= n_top[i] else "t" for i in ids if max(n_top[i], n_bot[i]) >= HUB}
    for _ in range(3 * len(hubs) * isolate):    # a hub takes a row of its own: it drops, or its row-mates do
        h = next((h for h in hubs if any(rank[i] == rank[h] for i in ids if i != h)), None)
        if h is None:
            break
        drop = [h] if hubs[h] == "t" else [i for i in ids if rank[i] == rank[h] and i != h]
        rank = settle({**rank, **{i: rank[h] + 1 for i in drop}})

    def arrange(rank: dict) -> tuple:
        """Rows of parts and waypoints, ordered so that few arrows cross."""
        rk = dict(rank)
        layers: list[list] = [[] for _ in range(max(rk.values()) + 1)]
        for i in ids:
            layers[rk[i]].append(i)
        chains = []                         # per arrow: the nodes it passes, top to bottom
        for k, (a, b) in enumerate(down):
            chain = [a]
            for r in range(rk[a] + 1, rk[b]):
                layers[r].append((k, r))
                rk[(k, r)] = r
                chain.append((k, r))
            chains.append(chain + [b])
        segs = [(c[j], c[j + 1]) for c in chains for j in range(len(c) - 1)]
        up, dn = defaultdict(list), defaultdict(list)
        for a, b in segs:
            dn[a].append(b)
            up[b].append(a)

        def crossings() -> int:
            pos = {n: i for L in layers for i, n in enumerate(L)}
            return sum(1 for i, (a, b) in enumerate(segs) for c, d in segs[i + 1:]
                       if rk[a] == rk[c] and (pos[a] - pos[c]) * (pos[b] - pos[d]) < 0)

        best = (crossings(), [L[:] for L in layers])
        for sweep in range(24):
            fwd = sweep % 2 == 0
            nb = up if fwd else dn
            for r in (range(1, len(layers)) if fwd else range(len(layers) - 2, -1, -1)):
                ref = {n: i for i, n in enumerate(layers[r - 1 if fwd else r + 1])}
                cur = {n: i for i, n in enumerate(layers[r])}
                layers[r].sort(key=lambda n: sum(ref[q] for q in nb[n]) / len(nb[n]) if nb[n] else cur[n])
            c = crossings()
            if c < best[0]:
                best = (c, [L[:] for L in layers])
        layers[:] = best[1]
        improved = True
        while improved:
            improved = False
            for L in layers:
                for i in range(len(L) - 1):
                    before = crossings()
                    L[i], L[i + 1] = L[i + 1], L[i]
                    if crossings() < before:
                        improved = True
                    else:
                        L[i], L[i + 1] = L[i + 1], L[i]
        given = {i: k for k, i in enumerate(ids)}
        settled = False
        while not settled:                  # where it costs no crossings, keep the order the parts were given in
            settled = True
            for L in layers:
                for i in range(len(L) - 1):
                    if real(L[i]) and real(L[i + 1]) and given[L[i]] > given[L[i + 1]]:
                        before = crossings()
                        L[i], L[i + 1] = L[i + 1], L[i]
                        if crossings() > before:
                            L[i], L[i + 1] = L[i + 1], L[i]
                        else:
                            settled = False
        return layers, chains, rk, up, dn

    for _ in range(4):                      # stagger a row too wide to read: every second part drops one row
        layers, chains, rk, up, dn = arrange(rank)
        w, r = max((sum(base_w[n] + GAP_X for n in L if real(n)), r) for r, L in enumerate(layers))
        row = [n for n in ids if rk[n] == r]
        if w <= ROW_W or len(row) < 4:
            break
        lower = set(row[1::2])              # in the order given, so the two rows read as a zigzag through it
        rank = {i: k + (1 if k > r or i in lower else 0) for i, k in rank.items()}

    return {"ids": ids, "pairs": pairs, "flipped": flipped, "n_top": n_top, "n_bot": n_bot,
            "kind": kind, "base_words": base_words, "sub": sub, "opens": opens, "hubs": hubs,
            "layers": layers, "chains": chains, "rk": rk, "up": up, "dn": dn}


def _layout_once(topo: dict, arrows: dict[tuple[str, str], dict],
                 gap_add: list[float] | None = None, gap_x: float = GAP_X,
                 wp_gap: float = 10.0, port_gap: float = PORT) -> tuple[dict, dict[tuple[str, str], list[int]]]:
    """One layered top-down layout pass with the given spacing parameters."""
    ids, pairs, flipped = topo["ids"], topo["pairs"], topo["flipped"]
    n_top, n_bot = topo["n_top"], topo["n_bot"]
    kind, sub, opens, hubs = topo["kind"], topo["sub"], topo["opens"], topo["hubs"]
    layers, chains, rk, up, dn = topo["layers"], topo["chains"], topo["rk"], topo["up"], topo["dn"]
    words = dict(topo["base_words"])
    real = lambda n: isinstance(n, str)
    tall = lambda i: (38 if len(words[i]) == 1 else 56) + (8 if kind[i] == "store" else 0) + (SUB if sub[i] else 0)
    size = {i: (_part_w(words[i], bool(sub[i]), max(n_top[i], n_bot[i]), port_gap), tall(i)) for i in ids}
    W = lambda n: size[n][0] if real(n) else 8.0
    sep = lambda a, b: (W(a) + W(b)) / 2 + (gap_x if real(a) or real(b) else wp_gap)

    x: dict = {}
    for L in layers:
        cx = 0.0
        for i, n in enumerate(L):
            cx += sep(L[i - 1], n) if i else 0
            x[n] = cx
        mid = (x[L[0]] + x[L[-1]]) / 2
        for n in L:
            x[n] -= mid

    def place(L: list, target) -> None:
        """Move each node toward target(n) = (position, weight), keeping order and spacing."""
        off = [0.0]
        for i in range(1, len(L)):
            off.append(off[-1] + sep(L[i - 1], L[i]))
        for n, pos in zip(L, _isotonic_pool([target(n) for n in L], off)):
            x[n] = pos

    def toward(*nbs):                       # the mean position of a node's neighbours in the rows given
        def target(n):
            near = [x[q] for nb in nbs for q in nb[n]]
            return (sum(near) / len(near), 1.0 if real(n) else 4.0) if near else (x[n], 0.2)
        return target

    for it in range(12):
        fwd = it % 2 == 0
        for r in (range(1, len(layers)) if fwd else range(len(layers) - 2, -1, -1)):
            place(layers[r], toward(up if fwd else dn))
    for L in layers:
        place(L, toward(up, dn))

    bars = {h for h in hubs if sum(map(real, layers[rk[h]])) == 1}
    straight = {}
    for c in chains:                        # a long arrow runs straight; from a bar, straight down to its part
        top_bar, low_bar = c[0] in bars and hubs[c[0]] == "b", c[-1] in bars and hubs[c[-1]] == "t"
        a = x[c[-1]] if top_bar and not low_bar else x[c[0]]
        b = x[c[0]] if low_bar and not top_bar else x[c[-1]]
        for j, n in enumerate(c[1:-1], 1):
            straight[n] = a + (b - a) * j / (len(c) - 1)
    for L in layers:
        place(L, lambda n: (x[n], 8.0) if real(n) else (straight[n], 1.0))
    pad_bar = 12.0 + (gap_x - GAP_X)        # keep a stretched bar clear of passing waypoints when widening
    for h in bars:                          # stretch the bar over the parts its arrows join, as far as its row is free
        far = [x[c[1]] for c in chains if c[0] == h] if hubs[h] == "b" else [x[c[-2]] for c in chains if c[-1] == h]
        L = layers[rk[h]]
        i = L.index(h)
        lo = x[L[i - 1]] + W(L[i - 1]) / 2 + pad_bar if i else -math.inf
        hi = x[L[i + 1]] - W(L[i + 1]) / 2 - pad_bar - (5.0 if opens[h] else 0.0) if i + 1 < len(L) else math.inf
        left = max(lo, min(min(far) - 30, x[h] - W(h) / 2))
        right = min(hi, max(max(far) + 30, x[h] + W(h) / 2))
        if right - left > text_w(" ".join(words[h]), FS) * 1.06 + 38:
            words[h] = [" ".join(words[h])]
        size[h] = (right - left, tall(h))
        x[h] = (left + right) / 2

    left = min(x[n] - W(n) / 2 for n in x)
    y, top, band = {}, float(PAD), []
    busy = defaultdict(int)                 # arrows through the gap below each row: their labels need the room
    for c in chains:
        for n in c[:-1]:
            busy[rk[n]] += 1
    rows = [max(size[n][1] for n in L if real(n)) for L in layers]
    stack = defaultdict(lambda: defaultdict(list))      # per gap and flow: the label heights of the arrows that start there
    for pair, c in zip(pairs, chains):
        for f, s in enumerate(arrows[pair]["label_sizes"]):
            if s:
                stack[rk[c[0]]][f].append(s[1])
    floor = [max([44.0] + [max(hs) + LINE * min(2, len(hs) - 1) + 24 for hs in stack[r].values()])
             for r in range(len(layers) - 1)]           # the least a gap needs to hold its tallest label
    want = [max(f, min(160.0, max(GAP_Y, 40 + 12 * busy[r]))) for r, f in enumerate(floor)]
    spare = TALL - sum(rows) - sum(floor)               # a tall picture gives up the room beyond that first
    ease = min(1.0, max(0.0, spare) / (sum(want) - sum(floor) or 1.0))
    gaps_y = [floor[r] + (want[r] - floor[r]) * ease + (gap_add[r] if gap_add and r < len(gap_add) else 0.0)
              for r in range(len(want))]
    gap = 0.0
    for r, (L, h) in enumerate(zip(layers, rows)):
        band.append((top, top + h))
        for n in L:
            x[n] += PAD - left
            y[n] = top + h / 2
        top += h + (gap := gaps_y[r] if r < len(gaps_y) else 0.0)
    width = max(x[n] + W(n) / 2 + (5.0 if real(n) and opens[n] else 0.0) for n in x) + PAD
    height = top - gap + PAD

    def edge_y(n: str, px: float, bottom: bool) -> float:
        w, h = size[n]
        dip = 0.0
        if kind[n] == "store":              # a cylinder's rim curves away from the box edge
            dip = 5 * (1 - math.sqrt(max(0.0, 1 - ((px - x[n]) / (w / 2)) ** 2)))
        return y[n] + h / 2 - dip if bottom else y[n] - h / 2 + dip

    sides = {"b": defaultdict(list), "t": defaultdict(list)}
    for k, c in enumerate(chains):
        sides["b"][c[0]].append((x[c[1]], k))
        sides["t"][c[-1]].append((x[c[-2]], k))
    port = {}
    for side, d in sides.items():
        for n, lst in d.items():
            lst.sort()
            if n in bars:                   # on a bar an arrow leaves in line with where it is going
                reach = W(n) / 2 - 16
                want_x = [min(max(far_x, x[n] - reach), x[n] + reach) for far_x, _ in lst]
                for (_, k), px in zip(lst, _spread_bar_ports(want_x, x[n] - reach, x[n] + reach, float(port_gap))):
                    port[side, k] = px
                continue
            p_gap = min(float(port_gap), (W(n) - 32) / max(1, len(lst) - 1))
            for i, (_, k) in enumerate(lst):    # on a box they are spread around the middle, in order
                port[side, k] = x[n] + (i - (len(lst) - 1) / 2) * p_gap

    pboxes = {i: (x[i] - size[i][0] / 2, x[i] + size[i][0] / 2 + (5.0 if opens[i] else 0.0),
                  y[i] - size[i][1] / 2, y[i] + size[i][1] / 2 + (5.0 if opens[i] else 0.0))
              for i in ids}

    paths = []                              # each arrow's outline and exact segments for clearance checks
    arrow_gaps: dict[tuple[str, str], list[int]] = {}
    for k, (pair, chain) in enumerate(zip(pairs, chains)):
        tip_low = pair not in flipped       # the arrowhead is at the lower end unless laid out upwards
        p0 = (port["b", k], edge_y(chain[0], port["b", k], True))
        p1 = (port["t", k], edge_y(chain[-1], port["t", k], False))
        pts = [(p0[0], p0[1] + (0 if tip_low else 7))]
        for n in chain[1:-1]:
            pts += [(x[n], band[rk[n]][0]), (x[n], band[rk[n]][1])]
        pts.append((p1[0], p1[1] - (7 if tip_low else 0)))
        d = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
        gaps_s, runs_s, pieces = [], [], [] # where a label can sit: curves between rows, then straight runs past rows
        for j, ((x1, y1), (x2, y2)) in enumerate(zip(pts, pts[1:])):
            if j % 2:                       # straight through a row, past its parts
                d += f"L{x2:.1f},{y2:.1f}"
                pieces.append((x1, y1, x2, y2, False))
                runs_s.append((x1, y1, x2, y2, False))
                continue
            my = (y1 + y2) / 2
            d += f"C{x1:.1f},{my:.1f} {x2:.1f},{my:.1f} {x2:.1f},{y2:.1f}"
            gaps_s.append((x1, y1, x2, y2, True))
            pieces.append((x1, y1, x2, y2, True))
        tx, ty = p1 if tip_low else p0
        s = 1 if tip_low else -1
        head = f"M{tx:.1f},{ty:.1f}l-4.2,{-8 * s}h8.4z"
        hb = (tx - 4.2, tx + 4.2, min(ty, ty - 8 * s), max(ty, ty - 8 * s))
        arrow_geom = {"d": d, "head": head, "head_box": hb, "pieces": pieces, "bbox": _bbox_of_pieces(pieces, hb)}
        paths.append((arrow_geom, gaps_s, runs_s))
        arrow_gaps[pair] = list(range(rk[chain[0]], rk[chain[-1]]))

    placed = _place_labels(pairs, chains, paths, arrows, n_top, n_bot, pboxes, width, height)

    def _pack_geo(per_flow: dict[tuple[str, str], list[tuple[float, float, int] | None]] | None = None) -> dict:
        geo_arrows, reach_x, reach_y = {}, [0.0, width], [0.0, height]
        for k, pair in enumerate(pairs):
            entry = {**paths[k][0], "label": placed[k]}
            if per_flow is not None:
                entry["labels"] = per_flow[pair]
            for fi, sz in enumerate(arrows[pair]["label_sizes"]):
                if sz:
                    lb = label_box(*_spot_for(entry, fi), sz)
                    reach_x = [min(reach_x[0], lb[0] - 4.0), max(reach_x[1], lb[1] + 4.0)]
                    reach_y = [min(reach_y[0], lb[2] - 4.0), max(reach_y[1], lb[3] + 4.0)]
            geo_arrows[pair] = entry
        return {"x0": reach_x[0], "y0": reach_y[0], "w": reach_x[1] - reach_x[0], "h": reach_y[1] - reach_y[0],
                "arrows": geo_arrows, "boxes": pboxes, "parts": {i: (x[i], y[i], *size[i], words[i]) for i in ids}}

    geo = _pack_geo()
    n_flows = max((len(arrows[p]["label_sizes"]) for p in pairs), default=0)
    if n_flows > 1 and (bs := breaches(geo, arrows)):
        # A flow only shares the page with itself: re-place breaching flows independently before widening the picture.
        per_flow = {
            pair: [placed[k] if (fi < len(arrows[pair]["label_sizes"]) and arrows[pair]["label_sizes"][fi]) else None
                   for fi in range(n_flows)]
            for k, pair in enumerate(pairs)
        }
        for fi in sorted({b.flow for b in bs}):
            active_ks = [k for k, pair in enumerate(pairs)
                         if fi < len(arrows[pair]["label_sizes"]) and arrows[pair]["label_sizes"][fi]]
            sub_pairs = [pairs[k] for k in active_ks]
            sub_arrows = {
                pairs[k]: {**arrows[pairs[k]], "label_sizes": [arrows[pairs[k]]["label_sizes"][fi]],
                           "label_w": arrows[pairs[k]]["label_sizes"][fi][0]}
                for k in active_ks
            }
            sub_placed = _place_labels(sub_pairs, [chains[k] for k in active_ks], [paths[k] for k in active_ks],
                                       sub_arrows, n_top, n_bot, pboxes, width, height)
            for pair, spot in zip(sub_pairs, sub_placed):
                per_flow[pair][fi] = spot
        geo = _pack_geo(per_flow)
    return geo, arrow_gaps


def _place_labels(pairs: list[tuple[str, str]], chains: list[list], paths: list[tuple],
                  arrows: dict[tuple[str, str], dict], n_top: dict, n_bot: dict,
                  pboxes: dict[str, tuple[float, float, float, float]],
                  width: float, height: float) -> list[tuple[float, float, int]]:
    """Place each arrow's label beside its line (never on it), scoring candidates as (hard, soft)."""
    n_flows = max((len(arrows[pair]["label_sizes"]) for pair in pairs), default=0)
    active_flows = [[(fi, sz) for fi, sz in enumerate(arrows[pair]["label_sizes"]) if sz] for pair in pairs]
    arrow_geoms = [p[0] for p in paths]
    flow_members = [[k for k, pair in enumerate(pairs)
                     if fi < len(arrows[pair]["label_sizes"]) and arrows[pair]["label_sizes"][fi]]
                    for fi in range(n_flows)]
    flow_geoms = [[(q, arrow_geoms[q]) for q in ms] for ms in flow_members]
    flow_others = [[[q for q in flow_members[fi] if q != k] for fi in range(n_flows)] for k in range(len(pairs))]

    # Candidate spots sorted by soft score ascending: the first spot with zero hard breaches is the (hard, soft) optimum.
    cands: list[list[tuple[float, float, float, int]]] = []
    for k, (pair, chain) in enumerate(zip(pairs, chains)):
        _, gaps_s, runs_s = paths[k]
        lean = n_top[chain[-1]] - n_bot[chain[0]]
        home = 0 if lean > 0 else len(gaps_s) - 1 if lean < 0 else (len(gaps_s) - 1) / 2
        prefer = 0.3 if lean > 0 else 0.7 if lean < 0 else 0.5
        sizes, widest = arrows[pair]["label_sizes"], arrows[pair]["label_w"]
        active = [sz for sz in sizes if sz]
        max_h = max((sz[1] for sz in active), default=float(LINE))
        spots = [(gaps_s[j], abs(j - home)) for j in sorted(range(len(gaps_s)), key=lambda j: abs(j - home))]
        spots += [(run, len(gaps_s) + 1 + j) for j, run in enumerate(runs_s)]
        arrow_cands: list[tuple[float, float, float, int]] = []
        for (x1, y1, x2, y2, curve), seg_rank in spots:
            span_y = abs(y2 - y1)
            steps = min(29, max(17, int(span_y / 12)))
            y_top, y_bot = min(y1, y2), max(y1, y2)
            for i in range(steps):
                t = 0.04 + 0.92 * i / (steps - 1)
                x_mid = bez(x1, x2, t) if curve else x1
                ly = bez_y(y1, y2, t) if curve else y1 + (y2 - y1) * t
                xa = xb = x_mid
                if curve and x1 != x2:
                    xa = curve_x_at_y(x1, y1, x2, y2, ly - max_h / 2 - ARROW_MARGIN)
                    xb = curve_x_at_y(x1, y1, x2, y2, ly + max_h / 2 + ARROW_MARGIN)
                near_end = t < 0.28 or t > 0.72
                for side in (1, -1):
                    x_edge = (max(xa, xb) if side > 0 else min(xa, xb)) if (curve and x1 != x2) else x_mid
                    xs = [(x_mid, False)]
                    if abs(x_edge - x_mid) > 0.5:
                        xs.append((x_edge, False))
                    if near_end:            # tiny 4 px nudge near arrowheads clears wings without detaching
                        xs.append((x_edge + side * 4.0, True))
                    for lx, nudged in xs:
                        reach = label_box(lx, ly, side, (widest, 0.0))
                        soft = 40.0 * (max(0.0, 2.0 - reach[0]) + max(0.0, reach[1] - width + 2.0))
                        for sz in active:
                            lb = label_box(lx, ly, side, sz)
                            soft += 40.0 * (max(0.0, 2.0 - lb[2]) + max(0.0, lb[3] - height + 2.0))
                            if curve:
                                soft += (max(0.0, y_top + 6.0 - lb[2]) + max(0.0, lb[3] - y_bot + 2.0)) * sz[0]
                        soft += 12.0 * seg_rank + 8.0 * abs(t - prefer) + 0.8 * abs(lx - x_mid)
                        soft += (0.2 if side < 0 else 0.0) + (15.0 if nudged else 0.0)
                        arrow_cands.append((soft, lx, ly, side))
        arrow_cands.sort()
        cands.append(arrow_cands)

    # Cache static breach (count, area) per candidate via _static_hits so all placement passes share one rule.
    static_cost: list[list[tuple[int, float] | None]] = [[None] * len(cs) for cs in cands]

    def cand_static(k: int, idx: int) -> tuple[int, float]:
        cached = static_cost[k][idx]
        if cached is not None:
            return cached
        _, lx, ly, side = cands[k][idx]
        hc, ha = 0, 0.0
        for fi, sz in active_flows[k]:
            hits = _static_hits(label_box(lx, ly, side, sz), side, pboxes, flow_geoms[fi], k, top_min=2.0)
            hc += len(hits)
            ha += sum(pen for _, _, pen in hits)
        static_cost[k][idx] = (hc, ha)
        return hc, ha

    placed: list[tuple[float, float, int] | None] = [None] * len(pairs)
    placed_idx: list[int] = [0] * len(pairs)
    placed_boxes: list[dict[int, tuple[float, float, float, float]]] = [{} for _ in pairs]

    def set_placed(k: int, idx: int | None) -> None:
        if idx is None:
            placed[k] = None
            placed_boxes[k] = {}
        else:
            spot = cands[k][idx][1:]
            placed[k], placed_idx[k] = spot, idx
            placed_boxes[k] = {fi: label_box(*spot, sz) for fi, sz in active_flows[k]}

    def peer_clashes(k: int, spot: tuple[float, float, int]) -> tuple[int, float, set[int]]:
        """Label-vs-label clashes against already-placed arrows sharing a flow with arrow k."""
        hc, ha, peers = 0, 0.0, set()
        for fi, sz in active_flows[k]:
            lb = label_box(*spot, sz)
            for q in flow_others[k][fi]:
                if placed[q] is not None and rects_near(lb, placed_boxes[q][fi], MARGIN):
                    hc += 1
                    ha += _overlap_area(lb, placed_boxes[q][fi], MARGIN)
                    peers.add(q)
        return hc, ha, peers

    def pick_best(k: int) -> tuple[bool, int]:
        for idx, (_, lx, ly, side) in enumerate(cands[k]):
            if cand_static(k, idx)[0] == 0 and peer_clashes(k, (lx, ly, side))[0] == 0:
                return True, idx
        best_key: tuple[tuple[int, float], float] | None = None
        best_idx = 0
        for idx, (soft, lx, ly, side) in enumerate(cands[k]):
            shc, sha = cand_static(k, idx)
            if best_key is not None and shc > best_key[0][0]:
                continue
            phc, pha, _ = peer_clashes(k, (lx, ly, side))
            key = ((shc + phc, sha + pha), soft)
            if best_key is None or key < best_key:
                best_key, best_idx = key, idx
        return False, best_idx

    order = sorted(
        range(len(pairs)),
        key=lambda k: (
            len(paths[k][1]) + len(paths[k][2]),
            -max((s[0] * s[1] for _, s in active_flows[k]), default=0.0),
            -sum(len(qs) for qs in flow_others[k]),
            k,
        ),
    )
    for k in order:
        set_placed(k, pick_best(k)[1])

    def bad_arrows() -> set[int]:
        bad: set[int] = set()
        for k in range(len(pairs)):
            if cand_static(k, placed_idx[k])[0]:
                bad.add(k)
            phc, _, peers = peer_clashes(k, placed[k])
            if phc:
                bad.add(k)
                bad.update(peers)
        return bad

    for _ in range(4):
        bad = bad_arrows()
        if not bad:
            break
        for k in sorted(bad):
            set_placed(k, None)
            set_placed(k, pick_best(k)[1])
        bad = bad_arrows()
        if not bad:
            break
        for k in sorted(bad):               # try moving one blocking label out of the way
            moved = False
            for idx, (_, lx, ly, side) in enumerate(cands[k]):
                if cand_static(k, idx)[0]:
                    continue
                _, _, blockers = peer_clashes(k, (lx, ly, side))
                if len(blockers) == 1:
                    q = next(iter(blockers))
                    old_k, old_q = placed_idx[k], placed_idx[q]
                    set_placed(q, None)
                    set_placed(k, idx)
                    q_ok, q_idx = pick_best(q)
                    if q_ok:
                        set_placed(q, q_idx)
                        moved = True
                        break
                    set_placed(k, old_k)
                    set_placed(q, old_q)
            if moved and not bad_arrows():
                break

    return [spot for spot in placed if spot is not None]


def _clashing_gaps(bs: list[Breach], geo: dict, arrow_gaps: dict[tuple[str, str], list[int]],
                   widen_all: bool) -> set[int]:
    """Row gaps where breaching labels sit, so vertical room is added only where needed."""
    out: set[int] = set()
    for b in bs:
        pair = b.a[1]
        gs = arrow_gaps[pair]
        if widen_all or len(gs) == 1:
            out.update(gs)
        else:
            ly = _spot_for(geo["arrows"][pair], b.flow)[1]
            curves = [p for p in geo["arrows"][pair]["pieces"] if p[4]]
            idx = min(range(len(gs)), key=lambda i: 0.0 if curves[i][1] <= ly <= curves[i][3]
                      else min(abs(ly - curves[i][1]), abs(ly - curves[i][3])))
            out.add(gs[idx])
        if b.b[0] == "label" and len(arrow_gaps[b.b[1]]) == 1:
            out.add(arrow_gaps[b.b[1]][0])
    return out


def _trim_gaps(topo: dict, arrows: dict[tuple[str, str], dict],
               gap_x: float, wp_gap: float, port_gap: float,
               gap_add: list[float], clean_geo: dict) -> dict:
    """Undo unneeded vertical increments and shrink each remaining gap to the smallest clean height."""
    cur, best_geo = list(gap_add), clean_geo
    for r in sorted((r for r, v in enumerate(cur) if v > 0.0), key=lambda r: -cur[r]):
        trial = list(cur)
        trial[r] = 0.0
        g0, _ = _layout_once(topo, arrows, trial, gap_x, wp_gap, port_gap)
        if not breaches(g0, arrows):
            cur, best_geo = trial, g0
            continue
        lo, hi = 0.0, cur[r]
        while hi - lo > 6.0:
            mid = float(round((lo + hi) / 2.0))
            trial[r] = mid
            gm, _ = _layout_once(topo, arrows, trial, gap_x, wp_gap, port_gap)
            if not breaches(gm, arrows):
                hi, best_geo = mid, gm
            else:
                lo = mid
        cur[r] = hi
    return best_geo


def layout(parts: list[dict], arrows: dict[tuple[str, str], dict], isolate: bool = True) -> dict:
    """Layered, top-down: a part sits above the parts it acts on, so routes read downwards.

    Prefers horizontal room to vertical room (pictures are height-limited in the panel), adds only
    the minimal trimmed height a clash needs in the gap where it occurs, never alters rows or bars
    for label clearance, and picks the clean layout with the largest scale().
    """
    topo = _topology(parts, arrows, isolate)
    best_clean: dict | None = None
    best_fallback: dict | None = None
    best_count = math.inf

    def record(geo: dict, bs: list[Breach]) -> None:
        nonlocal best_clean, best_fallback, best_count
        if not bs:
            if best_clean is None or (scale(geo), -geo["h"], -geo["w"]) > (scale(best_clean), -best_clean["h"], -best_clean["w"]):
                best_clean = geo
        elif len(bs) < best_count:
            best_fallback, best_count = geo, len(bs)

    def can_beat(w: float, h: float) -> bool:
        return best_clean is None or (_scale_wh(w, h), -h) > (scale(best_clean), -best_clean["h"])

    def run_once(gap_add: list[float] | None, gx: float, wp: float, pg: float):
        geo, ag = _layout_once(topo, arrows, gap_add, gx, wp, pg)
        bs = breaches(geo, arrows)
        record(geo, bs)
        return geo, bs, ag

    # Horizontal spacing ladder (parts, long-arrow waypoints, ports), starting from defaults.
    h_levels = (
        (float(GAP_X), 10.0, float(PORT)),
        (42.0, 26.0, 30.0), (56.0, 42.0, 33.0), (64.0, 50.0, 35.0), (72.0, 58.0, 37.0),
        (92.0, 78.0, 42.0), (118.0, 104.0, 48.0), (144.0, 132.0, 55.0), (155.0, 145.0, 58.0),
        (210.0, 200.0, 74.0), (285.0, 265.0, 98.0), (360.0, 320.0, 126.0),
    )

    # Pass 1: try horizontal room with zero extra height first.
    base_runs: list[tuple[float, float, float, dict, list[Breach], dict]] = []
    for idx, (gx, wp, pg) in enumerate(h_levels):
        geo, bs, ag = run_once(None, gx, wp, pg)
        if not bs:
            if idx == 0:
                return geo
            break
        base_runs.append((gx, wp, pg, geo, bs, ag))

    if best_clean is not None and scale(best_clean) >= _scale_wh(1.0, base_runs[0][3]["h"]):
        return best_clean

    def try_vertical(gx: float, wp: float, pg: float, base_geo: dict, base_bs: list[Breach],
                     base_ag: dict, y_steps: tuple[float, ...]) -> bool:
        n_gaps = max((max(gs) + 1 for gs in base_ag.values() if gs), default=0)
        gap_add = [0.0] * n_gaps
        geo, bs, ag = base_geo, base_bs, base_ag
        base_h, base_w = base_geo["h"], base_geo["w"]
        for step_idx, step_y in enumerate(y_steps):
            if not n_gaps or not can_beat(base_w, base_h + sum(gap_add) + 6.0):
                break
            for r in _clashing_gaps(bs, geo, ag, widen_all=step_idx >= 2):
                gap_add[r] += step_y
            geo, bs, ag = run_once(gap_add, gx, wp, pg)
            if not bs:
                record(_trim_gaps(topo, arrows, gx, wp, pg, gap_add, geo), [])
                return True
        return False

    # Pass 2: add minimal trimmed height on levels that can improve scale(), preferring wider horizontal levels first.
    for gx, wp, pg, g0, bs0, ag0 in sorted(base_runs, key=lambda it: (-_scale_wh(it[3]["w"], it[3]["h"] + 6.0), -it[0])):
        if can_beat(g0["w"], g0["h"] + 6.0):
            try_vertical(gx, wp, pg, g0, bs0, ag0, (16.0, 28.0, 44.0, 64.0))

    # Pass 3: if still no clean layout, allow deeper vertical steps on wider levels.
    if best_clean is None:
        for gx, wp, pg, g0, bs0, ag0 in reversed(base_runs):
            if try_vertical(gx, wp, pg, g0, bs0, ag0, (32.0, 56.0, 88.0, 128.0, 180.0)):
                break

    return best_clean if best_clean is not None else best_fallback


# ---------------------------------------------------------------- drawing

def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


def rich(s: str) -> str:
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", esc(s))


def bare(s: str) -> str:
    return s.replace("`", "")


def view(parts: list[dict], groups: dict, flows: list[dict], inside: str | None) -> tuple[dict, list[dict], dict]:
    """One picture: the overview (inside is None), or one group opened.

    Returns where each part is drawn (a part's own box, its group's box, or nowhere), the boxes, and the
    arrows. In the overview a group is one box and a step inside it draws nothing. In an opened group its
    parts are drawn in full, and whatever they exchange steps with stands beside them as one box each.
    """
    home = {p["id"]: p["group"] or p["id"] for p in parts}
    if inside is None:
        at = dict(home)
    else:
        mine = {p["id"] for p in parts if p["group"] == inside}
        near = {e for f in flows for s in f["steps"] if {s["from"], s["to"]} & mine for e in (s["from"], s["to"])}
        near_groups = {home[e] for e in near} & set(groups) - {inside}
        at = {i: i if i in mine else home[i] for i in home if i in mine | near or home[i] in near_groups}
    nodes: dict[str, dict] = {}
    for p in parts:
        n = at.get(p["id"])
        if n is None or n in nodes:
            continue
        if n in groups:
            count = len(groups[n]["_parts"])
            nodes[n] = {"id": n, "label": groups[n]["label"], "kind": "code", "sub": f"{count} parts", "opens": True,
                        "outside": False, "beside": inside is not None}
        else:
            nodes[n] = {"id": n, "label": p["label"], "kind": p["kind"], "sub": None, "opens": False,
                        "outside": not p["_src"], "beside": inside is not None and p["group"] != inside}
    arrows: dict[tuple[str, str], dict] = {}
    for fi, f in enumerate(flows):
        for si, s in enumerate(f["steps"]):
            a, b = at.get(s["from"]), at.get(s["to"])
            if not a or not b or a == b or (inside is not None and not {s["from"], s["to"]} & mine):
                continue
            arr = arrows.setdefault((a, b), {"verbs": [], "seen": False, "shown": [[] for _ in flows]})
            verb = bare(s["verb"])
            if verb not in arr["verbs"]:
                arr["verbs"].append(verb)
            arr["seen"] = arr["seen"] or s["_src"] is not None
            arr["shown"][fi].append(f"{si + 1} {verb}")
    for arr in arrows.values():             # an arrow's checked labels: one per flow that uses it
        arr["label_sizes"] = [(max(step_w(t, FA) for t in lines) + 6, LINE * len(lines) + 5) if lines else None
                              for lines in arr["shown"]]
        arr["label_w"] = max((sz[0] for sz in arr["label_sizes"] if sz), default=0.0)
    return at, list(nodes.values()), arrows


def draw(name: str, nodes: list[dict], arrows: dict, geo: dict) -> str:
    lines, boxes, labels = [], [], []
    for (a, b), arr in arrows.items():
        g = geo["arrows"][a, b]
        cls = "arrow" + ("" if arr["seen"] else " unseen")
        ea, eb = esc(a), esc(b)
        lines.append(f'<g class="{cls}" data-arrow="{ea}&gt;{eb}" data-verbs="{esc(", ".join(arr["verbs"]))}">'
                     f'<path class="hit" d="{g["d"]}"/><path class="mark" d="{g["d"]}"/>'
                     f'<path class="line" d="{g["d"]}"/><path class="head" d="{g["head"]}"/></g>')
        labels.append(f'<text class="{cls}" data-arrow="{ea}&gt;{eb}"></text>')
    for p in nodes:
        x, y, w, h, ls = geo["parts"][p["id"]]
        x0, y0 = x - w / 2, y - h / 2
        if p["kind"] == "store":
            r = f"{w / 2:.1f},5"
            shape = (f'<path class="shape" d="M{x0:.1f},{y0 + 5:.1f}v{h - 10:.1f}a{r} 0 0 0 {w:.1f},0v{-(h - 10):.1f}'
                     f'a{r} 0 0 0 {-w:.1f},0a{r} 0 0 0 {w:.1f},0"/>')
        else:
            rx = h / 2 if p["kind"] == "actor" else 5
            rect = f'<rect class="shape" x="{{:.1f}}" y="{{:.1f}}" width="{w:.1f}" height="{h:.1f}" rx="{rx:.1f}"/>'
            shape = (rect.format(x0 + 5, y0 + 5) if p["opens"] else "") + rect.format(x0, y0)   # a group is a stack of two
        block = 18 * len(ls) + (SUB if p["sub"] else 0)
        ty = y - block / 2 + 13.5 + (3 if p["kind"] == "store" else 0)
        text = "".join(f'<tspan x="{x:.1f}" dy="{18 if i else 0}">{esc(ln)}</tspan>' for i, ln in enumerate(ls))
        small = f'<text class="sub" x="{x:.1f}" y="{ty + 18 * len(ls) - 1:.1f}" data-sub="{esc(p["sub"])}">{esc(p["sub"])}</text>' if p["sub"] else ""
        cls = f'part {p["kind"]}' + " outside" * p["outside"] + " beside" * p["beside"] + " opens" * p["opens"]
        epid = esc(p["id"])
        opens = f' data-go="{epid}" tabindex="0" role="link" aria-label="Open {esc(p["label"])}"' if p["opens"] else ""
        boxes.append(f'<g class="{cls}" data-part="{epid}"{opens}>{shape}<text x="{x:.1f}" y="{ty:.1f}">{text}</text>{small}</g>')
    vx0 = math.floor(geo["x0"])
    vy0 = math.floor(geo.get("y0", 0.0))
    vw = math.ceil(geo["x0"] + geo["w"] + 5 - vx0)
    vh = math.ceil(geo.get("y0", 0.0) + geo["h"] + 5 - vy0)
    return (f'<svg data-view="{esc(name)}" viewBox="{vx0} {vy0} {vw} {vh}" style="max-width:{geo["w"] * 1.2:.0f}px" '
            f'role="img" aria-label="Map of the parts and the arrows between them" hidden>'
            f'{"".join(lines)}{"".join(boxes)}{"".join(labels)}</svg>')


KEY = {
    "code": '<rect x="1" y="3" width="20" height="10" rx="2"/>',
    "data": '<path d="M1,5v6a10,2.5 0 0 0 20,0v-6a10,2.5 0 0 0 -20,0a10,2.5 0 0 0 20,0"/>',
    "person or event": '<rect x="1" y="3" width="20" height="10" rx="5"/>',
    "group, click to open": '<rect x="4" y="5" width="17" height="9" rx="2"/><rect x="1" y="2" width="17" height="9" rx="2"/>',
    "outside the repository, or not seen": '<rect x="1" y="3" width="20" height="10" rx="2" stroke-dasharray="3 2"/>',
}


def build(m: dict, root: Path, ctx: dict) -> tuple[str, dict]:
    parts, flows, groups = m["parts"], m["flows"], ctx["groups"]
    by = {p["id"]: p for p in parts}
    title = m.get("title") or root.name

    views, maps, order, layout_errs = {}, [], {}, []

    def _label_spec(max_w: float, spot: tuple[float, float, int]) -> list:
        lx, ly, side = spot
        return [round(max(1.0, max_w - 2.0), 1), round(lx + LABEL_GAP * side, 1), round(ly + 4.0, 1), side]

    for name in [None, *groups]:
        at, nodes, arrows = view(parts, groups, flows, name)
        geo, bs = choose_layout(nodes, arrows, name or "")
        if bs:
            layout_errs.extend(format_breach(b, arrows) for b in bs)
            continue
        if scale(geo) < MIN_SCALE:
            where = f'group "{name}"' if name else "overview"
            layout_errs.append(
                f"{where}: picture shrinks to scale {scale(geo):.2f} ({geo['w']:.0f}x{geo['h']:.0f} px), "
                f"below the readable minimum {MIN_SCALE:.1f} — use fewer steps on busy arrows, shorter verbs, or split into groups"
            )
            continue
        v_arrows = {
            f"{a}>{b}": {
                "f": [_label_spec(sz[0], _spot_for(geo["arrows"][a, b], fi)) if sz else None
                      for fi, sz in enumerate(arr["label_sizes"])],
            }
            for (a, b), arr in arrows.items()
        }
        v_parts = {n["id"]: round(max(1.0, geo["parts"][n["id"]][2] - 24.0), 1) for n in nodes}
        views[name or ""] = {"label": groups[name]["label"] if name else title, "at": at, "arrows": v_arrows, "parts": v_parts}
        maps.append(draw(name or "", nodes, arrows, geo))
        order[name] = {n["id"]: (geo["parts"][n["id"]][1], geo["parts"][n["id"]][0]) for n in nodes}
    if layout_errs:
        raise LayoutError(layout_errs)
    names = {**{i: p["label"] for i, p in by.items()}, **{i: g["label"] for i, g in groups.items()}}

    def cite_html(c: Cite) -> str:
        if ctx["base"] and c.path not in ctx["dirty"]:
            kind = "tree" if c.path.endswith("/") else "blob"
            lines = f"#L{c.a}-L{c.b}" if c.a else ""
            href = f'{ctx["base"]}/{kind}/{ctx["commit"]}/{quote(c.path)}{lines}'
            return f'<a class="cite" href="{esc(href)}">{esc(c)}</a>'
        return f'<span class="cite">{esc(c)}</span>'

    flow_html, unseen = [], []
    for i, f in enumerate(flows):
        rows = []
        for s in f["steps"]:
            pair = f'{esc(by[s["from"]]["label"])} → {esc(by[s["to"]]["label"])}'
            src = cite_html(s["_src"]) if s["_src"] else '<span class="cite">not seen in the code</span>'
            note = f'<p>{rich(s["note"])}</p>' if s.get("note") else ""
            rows.append(f'<li tabindex="0" data-from="{esc(s["from"])}" data-to="{esc(s["to"])}" data-verb="{esc(bare(s["verb"]))}">'
                        f'<div><b>{pair}</b><span>{rich(s["verb"])} {src}</span>{note}<small></small></div></li>')
            if not s["_src"]:
                unseen.append(f'{pair}: {rich(s["verb"])}. Believed, but not seen in the code.')
        flow_html.append(f'<details name="flow"{" open" if i == 0 else ""}><summary>{rich(f["question"])}</summary>'
                         f'<ol>{"".join(rows)}</ol></details>')

    def part_html(p: dict) -> str:
        cites = " ".join(cite_html(c) for c in p["_src"]) or '<span class="cite">outside this repository</span>'
        return (f'<div tabindex="0" data-part="{esc(p["id"])}"><dt>{esc(p["label"])}</dt>'
                f'<dd>{rich(p.get("does") or "Starts a flow.")} {cites}</dd></div>')

    listing = []                            # in reading order of the pictures: top to bottom, left to right
    for nid in sorted(order[None], key=order[None].get):
        if nid in groups:
            g = groups[nid]
            enid = esc(nid)
            inner = "".join(part_html(by[i]) for i in sorted(g["_parts"], key=order[nid].get))
            listing.append(f'<div class="group"><div tabindex="0" role="link" data-part="{enid}" data-go="{enid}"><dt>{esc(g["label"])} '
                           f'<small>{len(g["_parts"])} parts</small></dt><dd>{rich(g["does"])}</dd></div>{inner}</div>')
        else:
            listing.append(part_html(by[nid]))
    unknowns = [rich(u) for u in m.get("unknowns") or []] + unseen
    unknown_html = (f'<section><h2>Not established</h2><ul>{"".join(f"<li>{u}</li>" for u in unknowns)}</ul></section>'
                    if unknowns else "")

    if ctx["commit"]:
        c7 = esc(ctx["commit"][:7])
        href = f'{ctx["base"]}/commit/{ctx["commit"]}'
        ref = f'<a href="{esc(href)}">{c7}</a>' if ctx["base"] else f"<code>{c7}</code>"
        stamp = f'Describes commit {ref} of {esc(ctx["when"])}'
        if ctx["dirty"]:
            stamp += f', plus uncommitted changes in {len(ctx["dirty"])} cited file{"s" if len(ctx["dirty"]) > 1 else ""}'
        if ctx.get("unverified"):
            n = len(ctx["unverified"])
            stamp += f', with {n} cited file{"s" if n > 1 else ""} not verified'
    else:
        stamp = f'Describes the files as they were on {esc(ctx["when"])}'
    if ctx.get("unchecked_browser"):
        stamp += " (not checked in a browser)"
    kinds = {p["kind"] for p in parts}
    shown = [("code", "code" in kinds), ("data", "store" in kinds), ("person or event", "actor" in kinds),
             ("group, click to open", bool(groups)), ("outside the repository, or not seen", True)]
    key = "".join(f'<span><svg viewBox="0 0 22 16" aria-hidden="true">{KEY[k]}</svg>{k}</span>' for k, on in shown if on)
    data = json.dumps({"views": views, "names": names}).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    fill = {"title": esc(title), "summary": rich(m["summary"]), "stamp": stamp + ".", "maps": "".join(maps), "key": key,
            "flows": "".join(flow_html), "parts": "".join(listing), "unknowns": unknown_html, "data": data}
    template = Path(__file__).with_name("page.html").read_text(encoding="utf-8")
    page = re.sub(r"\{\{(\w+)\}\}", lambda mt: fill[mt[1]], template)
    counts = {"parts": len(parts), "flows": len(flows), "steps": sum(len(f["steps"]) for f in flows),
              "not established": len(unknowns)}
    if groups:
        counts = {"groups": len(groups), **counts}
    return page, counts


def pictures(m: dict) -> list[tuple[str, str]]:
    """What to photograph, as (file suffix, page address): each flow on the overview, then each group opened."""
    flows = m["flows"]
    group = {p["id"]: p["group"] for p in m["parts"]}
    out = [(str(n), f"#{n}") for n in range(1, len(flows) + 1)]
    for g in m.get("groups") or []:         # a group is shown with the first flow that passes through it
        n = next((i for i, f in enumerate(flows, 1) for s in f["steps"] if g["id"] in (group[s["from"]], group[s["to"]])), 1)
        out.append((g["id"], f'#{g["id"]}/{n}'))
    return out


def _resolve_chrome() -> tuple[str | None, bool]:
    """Return (chrome_binary, fail_if_missing). Respects EXPLAIN_CODEBASE_CHROME (including 'none') and EXPLAIN_CODEBASE_REQUIRE_CHROME."""
    env = os.environ.get("EXPLAIN_CODEBASE_CHROME")
    if env is not None:
        if env.strip().lower() == "none":
            return None, bool(os.environ.get("EXPLAIN_CODEBASE_REQUIRE_CHROME"))
        if env:
            return env, True
    if CHROME and Path(CHROME).exists():
        return CHROME, True
    if os.environ.get("EXPLAIN_CODEBASE_REQUIRE_CHROME"):
        return None, True
    return None, False


def screenshots(page: Path, name: str, wanted: list[tuple[str, str]], stem: str | None = None) -> list[Path]:
    """One picture per flow and per group: the page reads which to show from its address."""
    chrome, _ = _resolve_chrome()
    if not chrome or not Path(chrome).exists():
        return []
    folder = Path(tempfile.mkdtemp(prefix="explain-codebase-"))
    shots = []
    label_stem = stem or page.stem
    with tempfile.TemporaryDirectory() as profile:
        for suffix, address in wanted:
            shot = folder / f"{name}-{label_stem}-{suffix}.png"
            try:
                subprocess.run([chrome, "--headless=new", "--disable-gpu", "--disable-dev-shm-usage",
                                "--hide-scrollbars", "--window-size=1288,720", "--virtual-time-budget=1500",
                                f"--user-data-dir={profile}", f"--screenshot={shot}", page.as_uri() + address],
                               capture_output=True, timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                break
            if shot.exists():
                shots.append(shot)
    if not shots:
        try:
            folder.rmdir()
        except OSError:
            pass
    return shots


BROWSER_PROBE_HEAD = "<script>window.__probeErrors=[];addEventListener('error',e=>window.__probeErrors.push(String(e.message||e.error)));</script>"
BROWSER_WIDE_FONT_CSS = '<style>body, svg, svg text, svg tspan { font-family: "Verdana", "DejaVu Sans", sans-serif !important; }</style>'

BROWSER_PROBE = r"""
(() => {
  // Layout places labels with MARGIN and ARROW_MARGIN; the browser samples curves and hints glyphs differently, so it
  // checks one pixel less. A label that passes still clears the edge of the highlighter by two pixels.
  const TOLERANCE = 1, MARGIN = 3 - TOLERANCE, ARROW_MARGIN = 7.5 - TOLERANCE;
  const near = (a, b, m = MARGIN) => a.x < b.x + b.width + m && b.x < a.x + a.width + m
    && a.y < b.y + b.height + m && b.y < a.y + a.height + m;
  const segHitsRect = (p1, p2, r, m) => {
    const rx0 = r.x - m, rx1 = r.x + r.width + m, ry0 = r.y - m, ry1 = r.y + r.height + m;
    if (Math.max(p1.x, p2.x) <= rx0 || Math.min(p1.x, p2.x) >= rx1
        || Math.max(p1.y, p2.y) <= ry0 || Math.min(p1.y, p2.y) >= ry1) return false;
    const dx = p2.x - p1.x, dy = p2.y - p1.y;
    let t0 = 0, t1 = 1;
    for (const [p, q] of [[-dx, p1.x - rx0], [dx, rx1 - p1.x], [-dy, p1.y - ry0], [dy, ry1 - p1.y]]) {
      if (p === 0) { if (q <= 0) return false; }
      else {
        const t = q / p;
        if (p < 0) { if (t > t1) return false; if (t > t0) t0 = t; }
        else { if (t < t0) return false; if (t < t1) t1 = t; }
      }
    }
    return t0 < t1 || (t0 === t1 && rx0 < p1.x + t0 * dx && p1.x + t0 * dx < rx1
                       && ry0 < p1.y + t0 * dy && p1.y + t0 * dy < ry1);
  };
  const writeResult = payload => {
    const el = document.createElement('script');
    el.id = 'label-check-result';
    el.type = 'application/json';
    el.textContent = JSON.stringify(payload).replace(/</g, '\\u003c');
    document.body.appendChild(el);
  };
  const run = () => {
    try {
      if (window.__probeErrors && window.__probeErrors.length) {
        writeResult({ checked: false, error: window.__probeErrors.join('; '), breaches: [] });
        return;
      }
      const out = [];
      for (const v of Object.keys(views)) {
        for (let fi = 0; fi < flows.length; fi++) {
          location.hash = '#' + (v ? encodeURIComponent(v) + '/' : '') + (fi + 1);
          read();
          svg.getBoundingClientRect();
          const vb = svg.viewBox.baseVal;
          const outside = r => r.x < vb.x || r.y < vb.y || r.x + r.width > vb.x + vb.width || r.y + r.height > vb.y + vb.height;
          const labels = all('text.arrow.on', svg).map(el => ({
            arrow: el.dataset.arrow,
            text: [...el.querySelectorAll(':scope > tspan')].map(t => t.textContent).join(', '),
            tooSmall: el.dataset.tooSmall === '1'
              || all(':scope > tspan', el).some(t => parseFloat(getComputedStyle(t).fontSize) < 8.99),
            r: el.getBBox(),
          }));
          const boxes = all('g.part', svg).map(el => {
            const shapes = el.querySelectorAll('.shape');
            const sr = shapes.length ? shapes[shapes.length - 1].getBBox() : el.getBBox();
            all('text', el).forEach(t => {
              const tr = t.getBBox();
              const small = t.dataset.tooSmall === '1' || parseFloat(getComputedStyle(t).fontSize) < 8.99;
              if (small || (tr.width > 0 && (tr.x < sr.x || tr.x + tr.width > sr.x + sr.width))) {
                out.push({ picture: v, flow: fi, kind: small ? 'min_font' : 'part_text',
                           a: el.dataset.part, a_text: t.textContent, b: el.dataset.part });
              }
            });
            return { part: el.dataset.part, r: el.getBBox() };
          });
          const lines = all('g.arrow.on', svg).map(el => {
            const p = el.querySelector('path.line');
            const head = el.querySelector('path.head');
            const len = p.getTotalLength();
            const n = Math.max(24, Math.ceil(len / 2));
            const pts = [];
            for (let i = 0; i <= n; i++) pts.push(p.getPointAtLength(len * i / n));
            return { arrow: el.dataset.arrow, pts, head: head.getBBox() };
          });
          for (let i = 0; i < labels.length; i++) {
            const a = labels[i];
            if (a.tooSmall) {
              out.push({ picture: v, flow: fi, kind: 'min_font', a: a.arrow, a_text: a.text, b: '9px' });
            }
            if (outside(a.r)) {
              out.push({ picture: v, flow: fi, kind: 'edge', a: a.arrow, a_text: a.text, b: 'viewBox' });
            }
            for (let j = i + 1; j < labels.length; j++) {
              if (near(a.r, labels[j].r, MARGIN)) {
                out.push({ picture: v, flow: fi, kind: 'label', a: a.arrow, a_text: a.text,
                           b: labels[j].arrow, b_text: labels[j].text });
              }
            }
            for (const bx of boxes) {
              if (near(a.r, bx.r, MARGIN)) {
                out.push({ picture: v, flow: fi, kind: 'part', a: a.arrow, a_text: a.text, b: bx.part });
              }
            }
            for (const ln of lines) {
              let hit = near(a.r, ln.head, MARGIN);
              for (let s = 0; !hit && s + 1 < ln.pts.length; s++) {
                if (segHitsRect(ln.pts[s], ln.pts[s + 1], a.r, ARROW_MARGIN)) hit = true;
              }
              if (hit) {
                out.push({ picture: v, flow: fi, kind: 'arrow', a: a.arrow, a_text: a.text, b: ln.arrow });
              }
            }
          }
        }
      }
      if (window.__probeErrors && window.__probeErrors.length) {
        writeResult({ checked: false, error: window.__probeErrors.join('; '), breaches: [] });
        return;
      }
      writeResult({ checked: true, error: null, breaches: out });
    } catch (err) {
      writeResult({ checked: false, error: String(err && err.stack || err), breaches: [] });
    }
  };
  if (document.readyState === 'complete') run();
  else window.addEventListener('load', run, { once: true });
})();
"""


def browser_probe_html(page: str, wide_font: bool = False) -> str:
    """Return a temporary copy of the rendered page with the browser label-check probe (and optional wide-font override) added."""
    out = page.replace("</style>", "</style>\n" + BROWSER_WIDE_FONT_CSS, 1) if wide_font else page
    out = out.replace("<script", BROWSER_PROBE_HEAD + "\n<script", 1) if "<script" in out else BROWSER_PROBE_HEAD + "\n" + out
    return out.replace("</html>", f"<script>{BROWSER_PROBE}</script>\n</html>", 1)


def parse_browser_result(dom: str) -> tuple[list[dict] | None, str | None]:
    """Extract (breaches, error) written by BROWSER_PROBE from Chrome's --dump-dom output."""
    mt = re.search(r'<script[^>]*\bid="label-check-result"[^>]*>(.*?)</script>', dom, re.S)
    if not mt:
        return None, "browser probe produced no result element"
    try:
        data = json.loads(mt[1])
    except json.JSONDecodeError as e:
        return None, f"browser probe wrote invalid JSON: {e}"
    if not isinstance(data, dict):
        return None, "browser probe result was not an object"
    if data.get("error"):
        return None, f"page or probe error: {data['error']}"
    if data.get("checked") is not True or not isinstance(data.get("breaches"), list):
        return None, "browser probe did not complete"
    return data["breaches"], None


def parse_browser_check(dom: str) -> list[dict] | None:
    """Extract breaches from Chrome's --dump-dom output, or None if missing or invalid."""
    breaches_list, err = parse_browser_result(dom)
    return None if err else breaches_list


def _run_browser_probe(chrome: str, page_html: str, wide_font: bool, tmp: Path) -> list[dict]:
    """Run one headless Chrome probe pass (default or wide-font CSS) and return its breaches."""
    tag = "wide" if wide_font else "default"
    probe = tmp / f"probe-{tag}.html"
    profile = tmp / f"profile-{tag}"
    probe.write_text(browser_probe_html(page_html, wide_font=wide_font), encoding="utf-8")
    try:
        r = subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--disable-dev-shm-usage",
             "--window-size=1288,720", "--virtual-time-budget=3000",
             f"--user-data-dir={profile}", "--dump-dom", probe.as_uri()],
            capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as e:
        raise BrowserError(f"Chrome ({tag}) timed out after {e.timeout}s") from e
    except OSError as e:
        raise BrowserError(f"failed to launch {chrome}: {e}") from e
    if r.returncode != 0:
        detail = (r.stderr or "").strip().splitlines()[-1:] or ["no stderr"]
        raise BrowserError(f"Chrome ({tag}) exited with code {r.returncode}: {detail[0]}")
    breaches_list, err = parse_browser_result(r.stdout)
    if err is not None:
        raise BrowserError(f"{tag}: {err}")
    if wide_font:
        return [{**item, "font": "wide"} for item in breaches_list]
    return breaches_list


def browser_check(page_path: Path) -> list[dict] | None:
    """Check the page twice in real headless Chrome: as written, and with a wide fallback font (Verdana / DejaVu Sans).

    Returns None only when no browser was configured or discovered. Raises BrowserError on any failure
    when a browser is configured, required, or discovered.
    """
    chrome, must_run = _resolve_chrome()
    if not chrome:
        if must_run:
            raise BrowserError("EXPLAIN_CODEBASE_REQUIRE_CHROME is set but no Chrome/Chromium binary was found")
        return None
    if not Path(chrome).exists():
        raise BrowserError(f"configured browser does not exist: {chrome}")
    page_html = page_path.read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        default_breaches = _run_browser_probe(chrome, page_html, wide_font=False, tmp=tmp_path)
        wide_breaches = _run_browser_probe(chrome, page_html, wide_font=True, tmp=tmp_path)
    return default_breaches + wide_breaches


def format_browser_breach(item: dict) -> str:
    """Error line for a breach found by the real browser after client-side fitting."""
    kind = str(item.get("kind") or "")
    a_val = str(item.get("a", ""))
    b_val = str(item.get("b", ""))
    is_part = kind == "part_text" or (kind == "min_font" and b_val == a_val)
    return _format_clash(
        item.get("picture") or "", int(item.get("flow", 0)), kind,
        a_val if is_part else a_val.replace(">", "->"),
        item.get("a_text") or "",
        b_val.replace(">", "->") if kind in ("label", "arrow") else b_val,
        item.get("b_text") or "",
        mode="browser, wide font" if item.get("font") == "wide" else "browser",
        suffix=" (text_w() underestimated a label width)",
    )


def _write_atomic(out: Path, text: str) -> None:
    """Write text to out via an exclusive temp file in out.parent, respecting umask and never following symlinks."""
    if out.is_symlink():
        raise OSError(f"{out} is a symlink")
    umask = os.umask(0o077)
    os.umask(umask)
    mode = 0o666 & ~umask
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out.stem}-", suffix=".tmp", dir=out.parent)
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            fp.write(text)
        if out.is_symlink():
            raise OSError(f"{out} is a symlink")
        os.replace(tmp_path, out)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _has_symlink_component(start: Path, root_real: Path) -> bool:
    """True if start or any directory strictly below root_real is a symlink, or if root_real is never reached."""
    if start.is_symlink():
        return True
    cur = start
    while True:
        if cur.resolve() == root_real:
            return False
        if cur.is_symlink():
            return True
        parent = cur.parent
        if parent == cur:
            return True
        cur = parent


def _symlink_inside_enclosing_repo(raw_src: Path) -> Path | None:
    """If a lexical ancestor of raw_src holds .git and a symlink lies strictly below it, return that symlink."""
    sym: Path | None = None
    cur = raw_src
    while True:
        if cur != raw_src:
            dot_git = cur / ".git"
            if dot_git.is_symlink():
                return dot_git
            if dot_git.exists():
                return sym
        if cur.is_symlink():
            sym = sym or cur
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    arg = sys.argv[1]
    if "\x00" in arg or _bad_text(arg, check_limits=False):
        print(f"error: invalid map path {arg!r}; nothing written.")
        return 1
    try:
        raw_src = Path(os.path.abspath(arg))
        if sym := _symlink_inside_enclosing_repo(raw_src):
            print(f"error: {raw_src} traverses symlink {sym} inside the repository; nothing written.")
            return 1
        root, in_git = find_root(raw_src)
        root_real = root.resolve()
        src_real = raw_src.resolve()
        out_dir_real = raw_src.parent.resolve()
        raw_out = raw_src.with_suffix(".html")
        out = src_real.with_suffix(".html")
        if _has_symlink_component(raw_src, root_real) or raw_out.is_symlink() or out.is_symlink():
            print(f"error: {raw_src} and every directory between {root_real} and {out} must not be symlinks; nothing written.")
            return 1
        if out == src_real:
            print(f"error: {raw_src} must not already end with .html; nothing written.")
            return 1
        if (root_real / ".git").is_symlink():
            print(f"error: {root_real / '.git'} must not be a symlink; nothing written.")
            return 1
    except (OSError, ValueError) as e:
        print(f"error: cannot resolve {arg!r}: {e}; nothing written.")
        return 1
    docs_real = root_real / "docs"
    if not src_real.is_relative_to(docs_real) or not out_dir_real.is_relative_to(docs_real) or src_real == docs_real:
        print(f"error: {raw_src} must be inside {docs_real}; nothing written.")
        return 1
    try:
        st = src_real.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_MAP_BYTES:
            print(f"error: {raw_src} is too large or not a regular file (limit {MAX_MAP_BYTES} bytes); nothing written.")
            return 1
        with src_real.open("rb") as fp:
            raw_bytes = fp.read(MAX_MAP_BYTES + 1)
        if len(raw_bytes) > MAX_MAP_BYTES:
            print(f"error: {raw_src} exceeds {MAX_MAP_BYTES} bytes; nothing written.")
            return 1
        m = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as e:
        print(f"error: cannot read {raw_src}: {e}")
        return 1
    chrome_opt_out = (os.environ.get("EXPLAIN_CODEBASE_CHROME") or "").strip().lower() == "none"
    errs, ctx = check(m, root, in_git and git(root, "rev-parse", "HEAD") is not None)
    if errs:
        print(*(f"error: {e}" for e in errs), sep="\n")
        print(f"{len(errs)} to fix; nothing written.")
        return 1
    ctx["unchecked_browser"] = chrome_opt_out
    try:
        page, counts = build(m, root, ctx)
    except LayoutError as e:
        print(*(f"error: {msg}" for msg in e.errors), sep="\n")
        print(f"{len(e.errors)} to fix; nothing written.")
        return 1

    # Check in headless Chrome (and take screenshots) on a private temp copy BEFORE placing the output page.
    shots: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="explain-codebase-check-") as tmp_dir:
        staged = Path(tmp_dir) / out.name
        staged.write_text(page, encoding="utf-8")
        try:
            b_errs = browser_check(staged)
        except BrowserError as e:
            print(f"error: browser check failed: {e}")
            print("nothing written.")
            return 1
        if b_errs:
            print(*(f"error: {format_browser_breach(b)}" for b in b_errs), sep="\n")
            print(f"{len(b_errs)} browser label breach(es); nothing written.")
            return 1
        if b_errs is not None:
            shots = screenshots(staged, root.name, pictures(m), stem=out.stem)

    try:
        _write_atomic(out, page)
    except OSError as e:
        print(f"error: cannot write {out}: {e}; nothing written.")
        return 1
    print(f"{out}  ({', '.join(f'{n} {k}' for k, n in counts.items())})")
    if shots:
        print("screenshots, one per flow on the overview, then one per group:", *shots, sep="\n  ")
    elif chrome_opt_out:
        print("no screenshots (EXPLAIN_CODEBASE_CHROME=none): open the page to check it")
    else:
        print("no screenshots (Chrome missing or too slow): open the page to check it")
    if b_errs is None:
        reason = "EXPLAIN_CODEBASE_CHROME=none" if chrome_opt_out else "Chrome missing"
        print(f"not checked in a browser ({reason})")
    else:
        print("browser check: all labels clear")
    return 0


if __name__ == "__main__":
    sys.exit(main())
