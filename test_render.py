# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Tests for render.py: each rule that keeps a map true must refuse the map that breaks it.

    uv run test_render.py
"""
from __future__ import annotations

import copy
import importlib.util
import io
import itertools
import json
import os
import random
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location("render", Path(__file__).with_name("render.py"))
render = importlib.util.module_from_spec(spec)
sys.modules["render"] = render
spec.loader.exec_module(render)

GOOD = {
    "summary": "Takes an order and books its shipment.",
    "parts": [
        {"id": "clerk", "label": "Clerk", "kind": "actor"},
        {"id": "api", "label": "Orders API", "does": "Accepts an order.", "src": ["src/api.py:1-12"]},
        {"id": "db", "label": "Orders database", "kind": "store", "does": "One row per order.", "src": ["src/schema.sql"]},
        {"id": "carrier", "label": "Carrier service", "does": "Books the shipment."},
    ],
    "flows": [{"question": "What happens when a clerk submits an order?", "steps": [
        {"from": "clerk", "to": "api", "verb": "posts the order", "src": "src/api.py:1-3"},
        {"from": "api", "to": "db", "verb": "inserts the order", "src": "src/api.py:5-6"},
        {"from": "api", "to": "carrier", "verb": "books a shipment", "src": "src/api.py:8-9", "note": "Only when in stock."},
    ]}],
}


def _make_repo(with_git: bool = True) -> tuple[tempfile.TemporaryDirectory, Path, dict]:
    """Create a temporary minimal repository (optionally git-initialised) and matching map copy."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name).resolve()
    (root / "src").mkdir()
    (root / "docs").mkdir()
    (root / "src/api.py").write_text("".join(f"line {i}\n" for i in range(1, 13)), encoding="utf-8")
    (root / "src/schema.sql").write_text("create table orders (id);\n", encoding="utf-8")
    m = copy.deepcopy(GOOD)
    if with_git:
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], check=True)
        m["commit"] = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    return tmp, root, m


def _run_main(map_path: Path, chrome: str | Path | None = None, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Invoke render.main() on map_path with isolated Chrome discovery and captured stdout."""
    buf = io.StringIO()
    with mock.patch.dict(os.environ, env or {}, clear=False), \
         mock.patch.object(sys, "argv", ["render.py", str(map_path)]), \
         redirect_stdout(buf):
        if env is None or "EXPLAIN_CODEBASE_CHROME" not in env:
            os.environ.pop("EXPLAIN_CODEBASE_CHROME", None)
        if env is None or "EXPLAIN_CODEBASE_REQUIRE_CHROME" not in env:
            os.environ.pop("EXPLAIN_CODEBASE_REQUIRE_CHROME", None)
        if chrome is not None:
            with mock.patch.object(render, "CHROME", str(chrome)):
                rc = render.main()
        else:
            rc = render.main()
    return rc, buf.getvalue()


def _probe_dom(payload: dict) -> str:
    return f'<html><body><script id="label-check-result" type="application/json">{json.dumps(payload)}</script></body></html>'


def _two_arrow_geo(ab_label: tuple[float, float, int], cd_label: tuple[float, float, int] = (200.0, 100.0, 1),
                   x2: float = 200.0, w: float = 400.0, h: float = 250.0,
                   extra_parts: dict | None = None, extra_boxes: dict | None = None) -> dict:
    """Synthetic picture with two vertical arrows (a->b at x=100, c->d at x=x2) for unit-testing breaches()."""
    parts = {"a": (100.0, 30.0, 80.0, 40.0, ["A"]), "b": (100.0, 180.0, 80.0, 40.0, ["B"]), **(extra_parts or {})}
    boxes = {"a": (60.0, 140.0, 10.0, 50.0), "b": (60.0, 140.0, 160.0, 200.0), **(extra_boxes or {})}

    def v_arrow(x: float, label: tuple[float, float, int]) -> dict:
        return {
            "d": f"M{x:.1f},50.0C{x:.1f},101.5 {x:.1f},101.5 {x:.1f},153.0",
            "head": f"M{x:.1f},160.0l-4.2,-8h8.4z",
            "head_box": (x - 4.2, x + 4.2, 152.0, 160.0),
            "pieces": [(x, 50.0, x, 153.0, True)],
            "bbox": (x - 4.2, x + 4.2, 50.0, 160.0),
            "label": label,
        }

    return {"x0": 0.0, "y0": 0.0, "w": w, "h": h, "parts": parts, "boxes": boxes,
            "arrows": {("a", "b"): v_arrow(100.0, ab_label), ("c", "d"): v_arrow(x2, cd_label)}}


class Repo(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp, self.root, self.map = _make_repo(with_git=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit(self, message: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)

    def errors(self) -> str:
        errs, _ = render.check(self.map, self.root, True)
        return "\n".join(errs)

    def test_a_true_map_passes_and_renders_every_part_and_step(self) -> None:
        errs, ctx = render.check(self.map, self.root, True)
        self.assertEqual(errs, [])
        page, counts = render.build(self.map, self.root, ctx)
        self.assertEqual(counts, {"parts": 4, "flows": 1, "steps": 3, "not established": 0})
        for text in ("Orders API", "books a shipment", "src/api.py:8-9", "Only when in stock.", self.map["commit"][:7]):
            self.assertIn(text, page)

    def test_a_citation_must_point_at_lines_that_exist(self) -> None:
        self.map["flows"][0]["steps"][1]["src"] = "src/api.py:5-60"
        self.assertIn("has 12 lines", self.errors())
        self.map["flows"][0]["steps"][1]["src"] = "src/missing.py:1-2"
        self.assertIn("is not a file", self.errors())

    def test_a_step_cites_a_call_site_not_a_file(self) -> None:
        self.map["flows"][0]["steps"][1]["src"] = "src/api.py"
        self.assertIn("cite the lines", self.errors())
        (self.root / "src/api.py").write_text("x\n" * 100)
        self.map["flows"][0]["steps"][1]["src"] = "src/api.py:1-90"
        self.assertIn("cite the call site", self.errors())

    def test_the_part_that_acts_owns_the_step(self) -> None:
        # evidence for "API inserts the order" taken from the database's file: the classic misattributed arrow
        self.map["flows"][0]["steps"][1]["src"] = "src/schema.sql:1-1"
        self.assertIn('not among the code cited by "api"', self.errors())

    def test_a_step_from_outside_cites_the_code_that_receives_it(self) -> None:
        self.map["flows"][0]["steps"][0]["src"] = "src/schema.sql:1-1"
        self.assertIn('not among the code cited by "api"', self.errors())

    def test_a_flow_is_connected(self) -> None:
        steps = self.map["flows"][0]["steps"]
        steps[1], steps[0] = steps[0], {"from": "db", "to": "carrier", "verb": "jumps"}
        self.assertIn("neither of which this flow has reached yet", self.errors())

    def test_a_consumer_may_pick_up_from_a_store_the_flow_reached(self) -> None:
        self.map["parts"].append({"id": "worker", "label": "Worker", "does": "Ships orders.", "src": ["src/schema.sql"]})
        self.map["flows"][0]["steps"].append({"from": "worker", "to": "db", "verb": "takes new orders"})
        self.assertEqual(self.errors(), "")

    def test_every_part_is_on_a_route(self) -> None:
        self.map["parts"].append({"id": "cache", "label": "Cache", "kind": "store", "does": "Unused."})
        self.assertIn('part "cache" is in no flow', self.errors())

    def test_the_map_stays_small(self) -> None:
        for i in range(render.MAX_BOXES):
            self.map["parts"].append({"id": f"p{i}", "label": f"Part {i}", "does": "Extra."})
        self.assertIn('into "groups"', self.errors())
        self.map = copy.deepcopy(GOOD) | {"commit": self.map["commit"]}
        self.map["flows"][0]["steps"][0]["verb"] = "x" * 40
        self.assertIn('"verb" is 40 characters', self.errors())

    def grouped(self) -> None:
        self.map["groups"] = [{"id": "office", "label": "Order office", "does": "Takes orders and stores them."}]
        for p in self.map["parts"][1:3]:
            p["group"] = "office"

    def test_a_group_is_one_box_in_the_overview_and_opens_into_its_parts(self) -> None:
        # the reader starts from few boxes; a step inside a group must not draw an arrow until the group is opened
        self.grouped()
        errs, ctx = render.check(self.map, self.root, True)
        self.assertEqual(errs, [])
        at, nodes, arrows = render.view(self.map["parts"], ctx["groups"], self.map["flows"], None)
        self.assertEqual([n["id"] for n in nodes], ["clerk", "office", "carrier"])
        self.assertEqual(set(arrows), {("clerk", "office"), ("office", "carrier")})
        self.assertEqual(arrows["office", "carrier"]["shown"], [["3 books a shipment"]])     # numbered as in the full flow
        at, nodes, arrows = render.view(self.map["parts"], ctx["groups"], self.map["flows"], "office")
        self.assertEqual(set(arrows), {("clerk", "api"), ("api", "db"), ("api", "carrier")})
        self.assertEqual({n["id"] for n in nodes if n["beside"]}, {"clerk", "carrier"})
        page, counts = render.build(self.map, self.root, ctx)
        self.assertEqual(counts["groups"], 1)
        self.assertIn('data-view="office"', page)
        self.assertEqual(render.pictures(self.map), [("1", "#1"), ("office", "#office/1")])

    def test_a_group_must_earn_its_box(self) -> None:
        self.grouped()
        self.map["parts"][2]["group"] = None
        self.assertIn('group "office" has 1 parts', self.errors())
        self.map["parts"][2]["group"] = "nowhere"
        self.assertIn('"group" names no group', self.errors())
        for p in self.map["parts"]:
            p["group"] = "office"
        self.assertIn("the overview would show no arrows", self.errors())

    def test_a_cited_file_that_changed_since_the_commit_must_be_read_again(self) -> None:
        (self.root / "src/api.py").write_text("".join(f"changed {i}\n" for i in range(1, 13)))
        self.commit("second")
        text = self.errors()
        self.assertIn("src/api.py changed since", text)
        self.assertIn('part "api"', text)
        self.assertNotIn("schema.sql changed", text)
        self.map["commit"] = self.git("rev-parse", "HEAD")
        self.assertEqual(self.errors(), "")

    def test_an_unseen_step_is_drawn_dashed_and_listed(self) -> None:
        del self.map["flows"][0]["steps"][2]["src"]
        errs, ctx = render.check(self.map, self.root, True)
        self.assertEqual(errs, [])
        page, counts = render.build(self.map, self.root, ctx)
        self.assertEqual(counts["not established"], 1)
        self.assertIn('class="arrow unseen" data-arrow="api&gt;carrier"', page)
        self.assertIn("Believed, but not seen in the code.", page)

    def test_uncommitted_changes_to_cited_files_are_declared_on_the_page(self) -> None:
        (self.root / "src/api.py").write_text("".join(f"edited {i}\n" for i in range(1, 13)))
        errs, ctx = render.check(self.map, self.root, True)
        self.assertEqual(errs, [])
        self.assertEqual(ctx["dirty"], {"src/api.py"})
        page, _ = render.build(self.map, self.root, ctx)
        self.assertIn("plus uncommitted changes in 1 cited file.", page)

    def test_citations_link_to_github_only_when_the_commit_is_there(self) -> None:
        self.git("remote", "add", "origin", "git@github.com:owner/repo.git")
        self.assertIsNone(render.check(self.map, self.root, True)[1]["base"])       # not pushed
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        errs, ctx = render.check(self.map, self.root, True)
        page, _ = render.build(self.map, self.root, ctx)
        self.assertIn(f'https://github.com/owner/repo/blob/{self.map["commit"]}/src/api.py#L5-L6', page)

    def test_nothing_is_written_while_the_map_is_wrong(self) -> None:
        self.map["flows"][0]["steps"][1]["src"] = "src/api.py:5-60"
        path = self.root / "docs/how-it-works.json"
        path.write_text(json.dumps(self.map))
        script = Path(__file__).with_name("render.py")
        r = subprocess.run([sys.executable, str(script), str(path)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("nothing written", r.stdout)
        self.assertFalse(path.with_suffix(".html").exists())


class Layout(unittest.TestCase):
    """Whatever the shape of the graph, parts stay apart and inside the picture."""

    SHAPES = {
        "chain": [("a", "b"), ("b", "c"), ("c", "d")],
        "loop": [("a", "b"), ("b", "c"), ("c", "a")],
        "both ways": [("a", "b"), ("b", "a"), ("a", "c")],
        "hub": [("in", "hub")] + [("hub", f"s{i}") for i in range(9)] + [(f"s{i}", "db") for i in range(9)],
        "two hubs": [(f"s{i}", h) for i in range(5) for h in ("db", "bus")] + [("x", "s0"), ("db", "bus")],
        "alone": [("a", "b")],      # plus a box no arrow touches: a group whose steps are all inside it
    }

    def test_parts_do_not_overlap_and_fit(self) -> None:
        for name, pairs in self.SHAPES.items():
            with self.subTest(name):
                ids = list(dict.fromkeys(itertools.chain.from_iterable(pairs))) + ["lone"] * (name == "alone")
                parts = [{"id": i, "label": f"Part {i}", "kind": "store" if i == "db" else "code",
                          "sub": "3 parts" if i in ("hub", "lone") else None} for i in ids]
                arrows = {p: {"label_sizes": [(60.0, 18.0)], "label_w": 60.0} for p in pairs}
                geo = render.layout(parts, arrows)
                boxes = {i: (x - w / 2, y - h / 2, x + w / 2, y + h / 2) for i, (x, y, w, h, _) in geo["parts"].items()}
                for b in boxes.values():
                    self.assertTrue(b[0] >= geo["x0"] and b[1] >= 0 and b[2] <= geo["x0"] + geo["w"] and b[3] <= geo["h"], b)
                for (i, p), (j, q) in itertools.combinations(boxes.items(), 2):
                    apart = p[2] <= q[0] or q[2] <= p[0] or p[3] <= q[1] or q[3] <= p[1]
                    self.assertTrue(apart, f"{i} overlaps {j}")
                self.assertEqual(set(geo["arrows"]), set(pairs))

    def test_labels_of_one_flow_keep_apart(self) -> None:
        """Labels shown together must not cover each other or a part: the reader could not tell whose they are.

        The shape is the one where they once did: two parts acting on one, three steps on one arrow.
        """
        pairs = [("person", "site"), ("jobs", "site"), ("jobs", "app"), ("site", "store"), ("app", "store")]
        parts = [{"id": i, "label": f"Part {i}", "kind": "code", "sub": None} for i in ("person", "jobs", "site", "app", "store")]
        sizes = {("jobs", "site"): (150.0, 50.0), ("site", "store"): (130.0, 50.0)}
        arrows = {p: {"label_sizes": [sizes.get(p, (110.0, 20.0))], "label_w": sizes.get(p, (110.0, 20.0))[0]} for p in pairs}
        geo = render.layout(parts, arrows)

        def label(p):
            (lx, ly, side), (w, h) = geo["arrows"][p]["label"], arrows[p]["label_sizes"][0]
            x0 = lx - w / 2 if side == 0 else lx + 6 if side > 0 else lx - 6 - w
            return x0, ly - h / 2, x0 + w, ly + h / 2

        boxes = {i: (x - w / 2, y - h / 2, x + w / 2, y + h / 2) for i, (x, y, w, h, _) in geo["parts"].items()}
        shapes = [(f"label {p}", label(p)) for p in pairs] + [(f"part {i}", b) for i, b in boxes.items()]
        for (i, p), (j, q) in itertools.combinations(shapes, 2):
            apart = p[2] <= q[0] or q[2] <= p[0] or p[3] <= q[1] or q[3] <= p[1]
            self.assertTrue(apart or (i.startswith("part") and j.startswith("part")), f"{i} overlaps {j}")


def _prep_fixture(path: Path) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """Fill the derived fields check() adds so real fixture maps can be laid out without their repos."""
    m = json.loads(path.read_text(encoding="utf-8"))
    parts = m["parts"]
    groups = {g["id"]: dict(g) for g in m.get("groups") or []}
    for p in parts:
        p.setdefault("kind", "code")
        p.setdefault("group", None)
        src = p.get("src") or []
        p["_src"] = [render.Cite(s, None, None) for s in ([src] if isinstance(src, str) else src)]
    for gid, g in groups.items():
        g["_parts"] = [p["id"] for p in parts if p.get("group") == gid]
    for f in m["flows"]:
        for s in f["steps"]:
            s["_src"] = render.Cite(s["src"], 1, 1) if s.get("src") else None
    return parts, groups, m["flows"]


def _random_case(rng: random.Random, family: str) -> tuple[list[dict], dict[tuple[str, str], dict]]:
    """Build one seeded random graph with 1..4 flows and labels 1..6 lines tall, 40..220 px wide."""
    n = rng.randint(4, 10)
    ids = [f"p{i}" for i in range(n)]
    edges: set[tuple[str, str]] = set()
    if family == "chain":
        for i in range(n - 1):
            edges.add((ids[i], ids[i + 1]))
        for _ in range(rng.randint(1, 3)):
            a, b = rng.sample(ids, 2)
            edges.add((a, b))
    elif family == "hub":
        hub = ids[0]
        for i in ids[1:]:
            edges.add((hub, i) if rng.random() < 0.7 else (i, hub))
        for _ in range(rng.randint(0, 3)):
            a, b = rng.sample(ids[1:], 2)
            edges.add((a, b))
    elif family == "fan_in":
        sink = ids[-1]
        for i in ids[:-1]:
            edges.add((i, sink))
        for i in range(n - 2):
            if rng.random() < 0.4:
                edges.add((ids[i], ids[i + 1]))
    elif family == "fan_out":
        root = ids[0]
        for i in ids[1:]:
            edges.add((root, i))
        for i in range(1, n - 1):
            if rng.random() < 0.4:
                edges.add((ids[i], ids[i + 1]))
    elif family == "both_dirs":
        for i in range(n - 1):
            edges.add((ids[i], ids[i + 1]))
            if rng.random() < 0.6:
                edges.add((ids[i + 1], ids[i]))
        a, b = ids[0], ids[1]
        edges.add((a, b))
        edges.add((b, a))
        for _ in range(rng.randint(0, 2)):
            u, v = rng.sample(ids, 2)
            edges.add((u, v))
            edges.add((v, u))
    elif family == "long_arrows":
        n = rng.randint(5, 9)
        ids = [f"p{i}" for i in range(n)]
        for i in range(n - 1):
            edges.add((ids[i], ids[i + 1]))
        for _ in range(rng.randint(2, 5)):
            i = rng.randint(0, n - 3)
            j = rng.randint(i + 2, n - 1)
            edges.add((ids[i], ids[j]) if rng.random() < 0.8 else (ids[j], ids[i]))
    elif family == "double_hub":
        n = rng.randint(6, 10)
        ids = [f"p{i}" for i in range(n)]
        h1, h2 = ids[0], ids[-1]
        mid = ids[1:-1]
        for m in mid:
            edges.add((h1, m))
            edges.add((m, h2))
        if rng.random() < 0.5:
            edges.add((h1, h2))
    else:  # mixed
        for i in range(n - 1):
            edges.add((ids[i], ids[i + 1]))
        target = min(14, n + rng.randint(1, 6))
        while len(edges) < target:
            a, b = rng.sample(ids, 2)
            edges.add((a, b))

    pairs = sorted(edges)
    n_flows = rng.randint(1, 4)
    # Assign each edge to at least one flow, capping steps per flow like real maps (<= MAX_STEPS).
    per_flow: list[list[tuple[str, str]]] = [[] for _ in range(n_flows)]
    for pair in pairs:
        k = rng.randint(1, min(n_flows, 2))
        for fi in rng.sample(range(n_flows), k):
            if len(per_flow[fi]) < 12:
                per_flow[fi].append(pair)
    for fi in range(n_flows):
        if not per_flow[fi]:
            per_flow[fi].append(rng.choice(pairs))
    arrows: dict[tuple[str, str], dict] = {}
    for pair in pairs:
        sizes: list[tuple[float, float] | None] = []
        shown: list[list[str]] = []
        for fi in range(n_flows):
            if pair in per_flow[fi]:
                lines = rng.randint(1, 6)
                w = round(rng.uniform(40.0, 220.0), 1)
                h = render.LINE * lines + 5.0
                sizes.append((w, h))
                shown.append([f"{j + 1} step" for j in range(lines)])
            else:
                sizes.append(None)
                shown.append([])
        if not any(sizes):
            lines = rng.randint(1, 6)
            sizes[0] = (round(rng.uniform(40.0, 220.0), 1), render.LINE * lines + 5.0)
            shown[0] = [f"{j + 1} step" for j in range(lines)]
        arrows[pair] = {
            "label_sizes": sizes,
            "label_w": max(s[0] for s in sizes if s),
            "shown": shown,
        }
    parts = [
        {
            "id": i,
            "label": f"Part {i}",
            "kind": rng.choice(render.KINDS),
            "sub": "3 parts" if idx == 0 and rng.random() < 0.25 else None,
            "opens": idx == 0 and rng.random() < 0.25,
        }
        for idx, i in enumerate(ids)
    ]
    for p in parts:
        if p["opens"]:
            p["sub"] = "3 parts"
    return parts, arrows


class LabelRule(unittest.TestCase):
    """Shared rule checker, real fixture maps, refusal, browser check, and seeded random graphs."""

    FAMILIES = ("chain", "hub", "fan_in", "fan_out", "both_dirs", "long_arrows", "double_hub", "mixed")

    def test_breaches_enforces_all_four_clauses_and_margin(self) -> None:
        extra_parts = {"dim": (280.0, 100.0, 60.0, 40.0, ["Dim"]), "grp": (280.0, 180.0, 80.0, 40.0, ["Grp"])}
        extra_boxes = {"dim": (250.0, 310.0, 80.0, 120.0), "grp": (240.0, 325.0, 160.0, 205.0)}  # +5 group stack
        mk_geo = lambda ab, cd=(200.0, 100.0, 1): _two_arrow_geo(ab, cd, extra_parts=extra_parts, extra_boxes=extra_boxes)

        arrows_same = {
            ("a", "b"): {"label_sizes": [(80.0, 20.0)], "label_w": 80.0},
            ("c", "d"): {"label_sizes": [(80.0, 20.0)], "label_w": 80.0},
        }
        self.assertEqual(render.breaches(mk_geo((100.0, 100.0, 1), (200.0, 65.0, 1)), arrows_same, "ov"), [])

        # Clause 1: label vs label within MARGIN (3 px).
        bs = render.breaches(mk_geo((100.0, 100.0, 1), (194.0, 100.0, -1)), arrows_same, "ov")
        self.assertIn(render.Breach("ov", 0, ("label", ("a", "b")), ("label", ("c", "d"))), bs)

        # Clause 2: label vs dimmed part and group +5 stack shadow within MARGIN.
        self.assertIn(render.Breach("ov", 0, ("label", ("a", "b")), ("part", "dim")),
                      render.breaches(mk_geo((162.0, 100.0, 1), (200.0, 65.0, 1)), arrows_same, "ov"))
        self.assertIn(render.Breach("ov", 0, ("label", ("a", "b")), ("part", "grp")),
                      render.breaches(mk_geo((250.0, 217.0, 1), (200.0, 65.0, 1)), arrows_same, "ov"))

        # Clause 3: label vs other arrow in same flow breaches, but arrow only in another flow does not.
        arrows_wide_same = {("a", "b"): {"label_sizes": [(92.0, 20.0)], "label_w": 92.0},
                            ("c", "d"): {"label_sizes": [(40.0, 20.0)], "label_w": 40.0}}
        arrows_diff_flow = {("a", "b"): {"label_sizes": [(92.0, 20.0), None], "label_w": 92.0},
                            ("c", "d"): {"label_sizes": [None, (40.0, 20.0)], "label_w": 40.0}}
        g_near = mk_geo((100.0, 100.0, 1), (200.0, 65.0, 1))
        self.assertIn(render.Breach("ov", 0, ("label", ("a", "b")), ("arrow", ("c", "d"))),
                      render.breaches(g_near, arrows_wide_same, "ov"))
        self.assertEqual(render.breaches(g_near, arrows_diff_flow, "ov"), [])

        # Clause 4: side == 0 (on its own line) always breaches own arrow.
        bs = render.breaches(mk_geo((100.0, 100.0, 0), (200.0, 65.0, 1)), arrows_same, "ov")
        self.assertIn(render.Breach("ov", 0, ("label", ("a", "b")), ("arrow", ("a", "b"))), bs)

    def test_unplaceable_map_is_refused_and_writes_nothing(self) -> None:
        # 1) A genuinely impossible label (wider than bounded horizontal spacing between flanking vertical arrows) is refused.
        parts = [{"id": i, "label": f"Part {i}", "kind": "code", "sub": None} for i in ("a", "b0", "b1", "b2", "b3", "c")]
        pairs = [("a", f"b{i}") for i in range(4)] + [(f"b{i}", "c") for i in range(4)]
        arrows = {
            p: {
                "label_sizes": [(650.0, 40.0) if p == ("a", "b1") else (80.0, 20.0)],
                "label_w": 650.0 if p == ("a", "b1") else 80.0,
                "shown": [["1 impossibly wide verb"] if p == ("a", "b1") else ["2 step"]],
            }
            for p in pairs
        }
        _, bs = render.choose_layout(parts, arrows, "")
        self.assertTrue(bs, "expected refusal when a label is wider than bounded horizontal room")

        # 2) In main(), layout breaches print error: lines with picture, question, steps and fix hint, write nothing, exit 1.
        tmp, root, m = _make_repo(with_git=False)
        try:
            path = root / "docs/how-it-works.json"
            path.write_text(json.dumps(m), encoding="utf-8")
            fake_b = [render.Breach("", 0, ("label", ("api", "db")), ("label", ("api", "carrier")))]
            with mock.patch.object(render, "choose_layout", side_effect=lambda n, a, p="": ({}, fake_b)):
                rc, out = _run_main(path)
            self.assertEqual(rc, 1)
            for snippet in ("error: overview, question 1:", "2 inserts the order", "3 books a shipment", "shorter verbs", "nothing written"):
                self.assertIn(snippet, out)
            self.assertFalse(path.with_suffix(".html").exists())
        finally:
            tmp.cleanup()

    def test_browser_check_probe_parse_degrade_and_error(self) -> None:
        # Probe injection leaves delivered HTML clean and adds probe only to the temporary copy.
        sample_html = "<!doctype html><html><body>ok</body></html>"
        probed = render.browser_probe_html(sample_html)
        self.assertNotIn("label-check-result", sample_html)
        for token in ("label-check-result", "getBBox", "getPointAtLength"):
            self.assertIn(token, probed)

        # Parsing --dump-dom output: clean, breaching (with reordered attributes), and missing/bad.
        clean_dom = '<html><body><script type="application/json" id="label-check-result">{"checked":true,"breaches":[]}</script></body></html>'
        self.assertEqual(render.parse_browser_check(clean_dom), [])
        breach_item = {"picture": "", "flow": 0, "kind": "part", "a": "api>db", "a_text": "2 inserts the order", "b": "db"}
        self.assertEqual(render.parse_browser_check(_probe_dom({"checked": True, "breaches": [breach_item]})), [breach_item])
        self.assertIsNone(render.parse_browser_check("<html><body>no result</body></html>"))

        # Degrades cleanly when Chrome is missing, and exits non-zero without writing when browser finds a breach.
        tmp, root, m = _make_repo(with_git=False)
        try:
            path = root / "docs/how-it-works.json"
            path.write_text(json.dumps(m), encoding="utf-8")

            rc, out = _run_main(path, chrome=root / "no-such-chrome")
            self.assertEqual(rc, 0)
            self.assertIn("not checked in a browser", out)
            self.assertTrue(path.with_suffix(".html").exists())
            self.assertNotIn("label-check-result", path.with_suffix(".html").read_text(encoding="utf-8"))

            path.with_suffix(".html").unlink()
            with mock.patch.object(render, "screenshots", return_value=[]), \
                 mock.patch.object(render, "browser_check", return_value=[breach_item]):
                rc2, out2 = _run_main(path)
            self.assertEqual(rc2, 1)
            for snippet in ("error: overview, question 1 (browser):", "2 inserts the order", "text_w()", "nothing written"):
                self.assertIn(snippet, out2)
            self.assertFalse(path.with_suffix(".html").exists())
        finally:
            tmp.cleanup()

    def test_fixtures_have_no_label_breaches(self) -> None:
        fixtures_dir = Path(__file__).with_name("fixtures")
        for fname in ("dense-1.json", "dense-2.json", "dense-3.json", "dense-4.json"):    # four real-width synthetic maps
            parts, groups, flows = _prep_fixture(fixtures_dir / fname)
            for name in [None, *groups]:
                with self.subTest(fixture=fname, picture=name or "overview"):
                    _, nodes, arrows = render.view(parts, groups, flows, name)
                    geo, bs = render.choose_layout(nodes, arrows, name or "")
                    self.assertEqual(bs, [], f"{fname} {name or 'overview'} had {len(bs)} breach(es): {bs[:3]}")
                    self.assertEqual(render.breaches(geo, arrows, name or ""), [])
                    self.assertGreaterEqual(
                        render.scale(geo), 0.85,
                        f"{fname} {name or 'overview'} shrank to scale {render.scale(geo):.2f} ({geo['w']:.1f}x{geo['h']:.1f})",
                    )

    def test_random_graphs_either_clean_or_refused(self) -> None:
        rng = random.Random(20261123)
        total = 120
        refused = []
        for idx in range(total):
            family = self.FAMILIES[idx % len(self.FAMILIES)]
            parts, arrows = _random_case(rng, family)
            geo, bs = render.choose_layout(parts, arrows, f"{family}-{idx}")
            if bs:
                refused.append((idx, family, len(bs)))
            else:
                self.assertEqual(render.breaches(geo, arrows, f"{family}-{idx}"), [])
                boxes = {i: (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
                         for i, (x, y, w, h, _) in geo["parts"].items()}
                for (i, p), (j, q) in itertools.combinations(boxes.items(), 2):
                    apart = p[2] <= q[0] or q[2] <= p[0] or p[3] <= q[1] or q[3] <= p[1]
                    self.assertTrue(apart, f"case {idx} ({family}): {i} overlaps {j}")
        self.assertLessEqual(len(refused), 6, f"too many refused ({len(refused)}/{total}): {refused[:10]}")


class SafetyAndContract(unittest.TestCase):
    """Safety on untrusted repositories, geometry invariants, fail-closed browser checks, docs and CI rules."""

    def test_writes_stay_in_repo_never_follow_symlinks_respect_umask_and_private_shots(self) -> None:
        tmp, root, m = _make_repo()
        outside = tempfile.TemporaryDirectory()
        try:
            secret = Path(outside.name).resolve() / "secret.txt"
            secret.write_text("KEEP ME", encoding="utf-8")
            os.chmod(secret, 0o600)
            map_path = root / "docs/how-it-works.json"
            map_path.write_text(json.dumps(m), encoding="utf-8")
            no_chrome = root / "no-chrome"

            # Planted symlink at .html.tmp must not overwrite target; output respects restrictive umask.
            (root / "docs/how-it-works.html.tmp").symlink_to(secret)
            old_umask = os.umask(0o077)
            try:
                rc, out = _run_main(map_path, chrome=no_chrome)
            finally:
                os.umask(old_umask)
            self.assertEqual(rc, 0, out)
            self.assertEqual(secret.read_text(encoding="utf-8"), "KEEP ME")
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
            out_html = root / "docs/how-it-works.html"
            self.assertEqual(stat.S_IMODE(out_html.stat().st_mode), 0o600)

            # Symlinked output path is refused without touching the target.
            out_html.unlink()
            out_html.symlink_to(secret)
            self.assertEqual(_run_main(map_path, chrome=no_chrome)[0], 1)
            self.assertEqual(secret.read_text(encoding="utf-8"), "KEEP ME")
            out_html.unlink()

            # Symlinked input map or parent directory resolving outside repo is refused.
            outside_map = Path(outside.name).resolve() / "ext.json"
            outside_map.write_text(json.dumps(m), encoding="utf-8")
            sym_map = root / "docs/linked.json"
            sym_map.symlink_to(outside_map)
            sym_docs = root / "linked_docs"
            sym_docs.symlink_to(Path(outside.name).resolve())
            for bad_input in (sym_map, sym_docs / "ext.json"):
                self.assertEqual(_run_main(bad_input, chrome=no_chrome)[0], 1)
                self.assertFalse(outside_map.with_suffix(".html").exists())

            # screenshots() uses a fresh private (0o700) temp folder per run.
            fake_chrome = root / "fake-chrome"
            fake_chrome.write_text("", encoding="utf-8")
            page_file = root / "docs/tmp.html"
            page_file.write_text("<html></html>", encoding="utf-8")

            def fake_run(cmd, **kw):
                for arg in cmd:
                    if arg.startswith("--screenshot="):
                        Path(arg.split("=", 1)[1]).write_bytes(b"png")
                return subprocess.CompletedProcess(cmd, 0, "", "")

            with mock.patch.object(render, "CHROME", str(fake_chrome)), \
                 mock.patch.object(subprocess, "run", side_effect=fake_run):
                s1 = render.screenshots(page_file, "repo", [("1", "#1")])
                s2 = render.screenshots(page_file, "repo", [("1", "#1")])
            self.assertTrue(s1 and s2)
            self.assertNotEqual(s1[0].parent, Path(tempfile.gettempdir()) / "explain-codebase")
            self.assertNotEqual(s1[0].parent, s2[0].parent)
            self.assertEqual(stat.S_IMODE(s1[0].parent.stat().st_mode), 0o700)
            for p in s1 + s2:
                p.unlink(missing_ok=True)
                p.parent.rmdir()
        finally:
            tmp.cleanup()
            outside.cleanup()

    def test_reads_stay_inside_repo_and_bound_size(self) -> None:
        tmp, root, m = _make_repo()
        outside = tempfile.TemporaryDirectory()
        try:
            ext_file = Path(outside.name).resolve() / "secret.py"
            ext_file.write_text("a\nb\nc\n", encoding="utf-8")
            (root / "src/leak.py").symlink_to(ext_file)
            (root / "src/extdir").symlink_to(Path(outside.name).resolve())

            # File symlink escaping repo is refused without disclosing line count; directory symlink refused too.
            for src_entry in ("src/leak.py:99-99", "src/extdir"):
                mc = copy.deepcopy(m)
                mc["parts"][1]["src"] = ["src/api.py:1-12", src_entry]
                errs, _ = render.check(mc, root, True)
                self.assertTrue(errs)
                self.assertNotIn("has 3 lines", "\n".join(errs))

            # Oversized cited file is refused by MAX_FILE_BYTES.
            (root / "src/big.py").write_bytes(b"x\n" * 1000)
            m3 = copy.deepcopy(m)
            m3["parts"][1]["src"] = ["src/api.py:1-12", "src/big.py:1-2"]
            with mock.patch.object(render, "MAX_FILE_BYTES", 500):
                self.assertTrue(render.check(m3, root, True)[0])
        finally:
            tmp.cleanup()
            outside.cleanup()

    def test_html_escaping_github_base_and_csp(self) -> None:
        tmp, root, m = _make_repo()
        try:
            evil_url = 'https://github.com/owner/repo"><img src=x onerror=alert(1)>'
            subprocess.run(["git", "-C", str(root), "remote", "add", "origin", evil_url], check=True)
            subprocess.run(["git", "-C", str(root), "update-ref", "refs/remotes/origin/main", "HEAD"], check=True)
            self.assertIsNone(render.github_base(root, m["commit"]))

            errs, ctx = render.check(m, root, True)
            self.assertEqual(errs, [])
            page, _ = render.build(m, root, ctx)
            self.assertNotIn("onerror=alert(1)", page)
            self.assertIn('http-equiv="Content-Security-Policy"', page)
            self.assertIn("default-src 'none'", page)

            ctx["base"] = 'https://github.com/o/r" onclick="x'
            self.assertNotIn('" onclick="', render.build(m, root, ctx)[0])
        finally:
            tmp.cleanup()

    def test_git_hardened_flags_timeout_and_hex_commit(self) -> None:
        tmp, root, m = _make_repo()
        try:
            seen_cmds, seen_timeouts = [], []
            real_run = subprocess.run

            def spy(cmd, *a, **kw):
                seen_cmds.append(cmd)
                seen_timeouts.append(kw.get("timeout"))
                return real_run(cmd, *a, **kw)

            with mock.patch.object(subprocess, "run", side_effect=spy):
                render.git(root, "rev-parse", "HEAD")
            self.assertIn("core.fsmonitor=false", " ".join(seen_cmds[0]))
            self.assertGreater(seen_timeouts[0], 0)

            for bad_commit in ("HEAD", "main", "abc12", "--all", "zzzzzzz"):
                m_bad = copy.deepcopy(m)
                m_bad["commit"] = bad_commit
                self.assertTrue(render.check(m_bad, root, True)[0], f"expected commit {bad_commit!r} to be refused")
        finally:
            tmp.cleanup()

    def test_strict_text_w_viewbox_highlighter_and_hover(self) -> None:
        # CJK, emoji and wide ASCII keep realistic widths; primary-font Latin stays calibrated to Avenir Next/Segoe UI.
        self.assertGreaterEqual(render.text_w("設定を読む", 12), 5 * 12.0)
        self.assertGreaterEqual(render.text_w("🚀", 12), 12.0)
        self.assertGreaterEqual(render.text_w("@" * 10, 12), 10 * 12.0 * 0.95)
        self.assertGreaterEqual(render.text_w("m" * 10, 12), 10 * 12.0 * 0.88)
        jp_parts = [{"id": "a", "label": "設定マネージャ", "kind": "code", "sub": None},
                    {"id": "b", "label": "検証サービス", "kind": "code", "sub": None}]
        jp_w = render.step_w("1 設定を読む", 12) + 6
        jp_geo = render.layout(jp_parts, {("a", "b"): {"label_sizes": [(jp_w, 20.0)], "label_w": jp_w}})
        self.assertGreaterEqual(jp_geo["parts"]["a"][2], render.text_w("設定マネージャ", render.FS) + 38)

        # Highlighter clearance (ARROW_MARGIN = 7.5 px) and viewBox edge containment.
        geo = _two_arrow_geo((100.0, 100.0, 1), (180.0, 100.0, 1), x2=180.0, w=300.0, h=200.0)
        gap = getattr(render, "LABEL_GAP", 6.0)
        arrows = {("a", "b"): {"label_sizes": [(75.0 - gap, 20.0)], "label_w": 75.0 - gap},
                  ("c", "d"): {"label_sizes": [(40.0, 20.0)], "label_w": 40.0}}
        self.assertIn(render.Breach("ov", 0, ("label", ("a", "b")), ("arrow", ("c", "d"))),
                      render.breaches(geo, arrows, "ov"))

        arrows_edge = {("a", "b"): {"label_sizes": [(40.0, 20.0)], "label_w": 40.0},
                       ("c", "d"): {"label_sizes": [(150.0, 20.0)], "label_w": 150.0}}
        self.assertTrue(any(b.b[0] == "edge" for b in render.breaches(geo, arrows_edge, "ov")))
        self.assertIn("viewBox", render.BROWSER_PROBE)

        # Pointing at a part lights arrow lines only (g[data-arrow]), not unchecked labels.
        self.assertIn("all('g[data-arrow]', svg)", Path(__file__).with_name("page.html").read_text(encoding="utf-8"))

    def test_browser_check_fails_closed_writes_nothing_and_uses_segments(self) -> None:
        tmp, root, m = _make_repo()
        try:
            map_path = root / "docs/how-it-works.json"
            map_path.write_text(json.dumps(m), encoding="utf-8")
            out_html = map_path.with_suffix(".html")
            fake_chrome = root / "fake-chrome"
            fake_chrome.write_text("", encoding="utf-8")

            # Broken EXPLAIN_CODEBASE_CHROME and EXPLAIN_CODEBASE_REQUIRE_CHROME with no browser must fail closed.
            self.assertEqual(_run_main(map_path, env={"EXPLAIN_CODEBASE_CHROME": str(root / "missing-chrome")})[0], 1)
            self.assertFalse(out_html.exists())
            self.assertEqual(_run_main(map_path, chrome=root / "no-chrome", env={"EXPLAIN_CODEBASE_REQUIRE_CHROME": "1"})[0], 1)
            self.assertFalse(out_html.exists())

            # Non-zero Chrome exit, browser-reported breach, and probe JS error each exit 1 and write nothing.
            breach_dom = _probe_dom({"checked": True, "breaches": [{"picture": "", "flow": 0, "kind": "part", "a": "api>db", "a_text": "2 inserts", "b": "db"}]})
            err_dom = _probe_dom({"checked": False, "error": "ReferenceError: read is not defined", "breaches": []})
            for result, expected in (
                (subprocess.CompletedProcess([], 1, "", "crash"), ""),
                (subprocess.CompletedProcess([], 0, breach_dom, ""), "nothing written"),
                (subprocess.CompletedProcess([], 0, err_dom, ""), "ReferenceError"),
            ):
                with mock.patch.object(subprocess, "run", return_value=result):
                    rc, out = _run_main(map_path, chrome=fake_chrome)
                self.assertEqual(rc, 1)
                self.assertFalse(out_html.exists())
                if expected:
                    self.assertIn(expected, out)

            self.assertIn("try {", render.BROWSER_PROBE)
            self.assertNotIn("ptNear", render.BROWSER_PROBE)
        finally:
            tmp.cleanup()

    def test_opened_group_max_boxes_beside_steps_and_min_scale(self) -> None:
        tmp, root, m = _make_repo()
        try:
            # Group of 7 parts touching 4 outside boxes = 11 boxes in opened group -> refused by check().
            m_big = {
                "commit": m["commit"], "summary": "Big group test.",
                "groups": [{"id": "grp", "label": "Big group", "does": "Seven parts."}],
                "parts": (
                    [{"id": f"in{i}", "label": f"In {i}", "group": "grp", "does": "Inside.", "src": ["src/api.py"]} for i in range(7)]
                    + [{"id": f"out{i}", "label": f"Out {i}", "kind": "actor"} for i in range(4)]
                ),
                "flows": [{"question": "How does it flow?", "steps": (
                    [{"from": "out0", "to": "in0", "verb": "starts"}]
                    + [{"from": f"in{i}", "to": f"in{i + 1}", "verb": "next"} for i in range(6)]
                    + [{"from": f"in{i}", "to": f"out{i}", "verb": f"calls {i}"} for i in (1, 2, 3)]
                )}],
            }
            errs, _ = render.check(m_big, root, True)
            self.assertTrue(any("11 boxes" in e or f"up to {render.MAX_BOXES}" in e for e in errs), errs)

            # Opened group view maps every member part of closed beside-groups.
            parts, groups, flows = _prep_fixture(Path(__file__).parent / "fixtures/dense-1.json")
            at, _, _ = render.view(parts, groups, flows, "g2")
            for pid in groups["g1"]["_parts"]:
                self.assertEqual(at.get(pid), "g1", f"member {pid} of beside-group g1 missing from at")

            # build() refuses a picture whose scale() < MIN_SCALE (0.6).
            errs0, ctx0 = render.check(m, root, True)
            self.assertEqual(errs0, [])
            tiny_geo = {"x0": 0.0, "w": 2000.0, "h": 1500.0, "arrows": {}, "parts": {p["id"]: (0, 0, 10, 10, ["x"]) for p in m["parts"]}}
            with mock.patch.object(render, "choose_layout", return_value=(tiny_geo, [])):
                with self.assertRaises(render.LayoutError) as cm:
                    render.build(m, root, ctx0)
            self.assertIn("0.6", "\n".join(cm.exception.errors))
        finally:
            tmp.cleanup()

    def test_malformed_map_types_and_limits_never_crash(self) -> None:
        tmp, root, m = _make_repo()
        try:
            cases = [
                lambda x: x.__setitem__("parts", "not-a-list"),
                lambda x: x.__setitem__("flows", {"bad": 1}),
                lambda x: x.__setitem__("groups", {"g": {"id": "g", "label": "G", "does": "D"}}),
                lambda x: x.__setitem__("unknowns", 123),
                lambda x: x.__setitem__("unknowns", [""]),
                lambda x: x.__setitem__("unknowns", ["x" * 500]),
                lambda x: x.__setitem__("title", 123),
                lambda x: x.__setitem__("title", "x" * 120),
                lambda x: x["parts"][1].__setitem__("src", 123),
                lambda x: x["parts"][1].__setitem__("src", {"src/api.py": "1-2"}),
                lambda x: x["parts"][1].__setitem__("group", ["office"]),
                lambda x: x["flows"][0]["steps"][0].__setitem__("from", ["clerk"]),
                lambda x: x["flows"][0]["steps"][0].__setitem__("to", {"id": "api"}),
            ]
            for idx, mutate in enumerate(cases):
                with self.subTest(case=idx):
                    mc = copy.deepcopy(m)
                    mutate(mc)
                    self.assertTrue(render.check(mc, root, True)[0], f"malformed case {idx} should return errors")
        finally:
            tmp.cleanup()

    def test_page_script_safety_and_toolbar_wrap(self) -> None:
        tpl = Path(__file__).with_name("page.html").read_text(encoding="utf-8")
        self.assertIn("Object.hasOwn(views", tpl)
        self.assertRegex(tpl, r"try\s*\{[^}]*decodeURIComponent")
        bar_css = re.search(r"\.bar\s*\{([^}]*)\}", tpl)
        self.assertIsNotNone(bar_css)
        self.assertIn("flex-wrap: wrap", bar_css[1])
        self.assertNotIn("overflow: hidden", bar_css[1])

    def test_docs_match_enforced_rules(self) -> None:
        skill = Path(__file__).with_name("SKILL.md").read_text(encoding="utf-8")
        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        for token in ("python3", "xdg-open", "6", "16", "title", "unknowns"):
            self.assertIn(token, skill)
        self.assertNotIn("With ten parts or fewer, use no groups.", skill)
        self.assertNotIn("Every part and every step cites a file and line range", readme)

    def test_ci_workflow_pins_shas_and_runs_browser_on_all_fixtures(self) -> None:
        wf = (Path(__file__).parent / ".github/workflows/test.yml").read_text(encoding="utf-8")
        uses = re.findall(r"uses:\s*(\S+)", wf)
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"@[0-9a-f]{40}$", f"action not pinned to 40-char SHA: {u}")
        self.assertIn("EXPLAIN_CODEBASE_REQUIRE_CHROME", wf)
        for fn in ("dense-1.json", "dense-2.json", "dense-3.json", "dense-4.json"):
            self.assertIn(fn, wf)


class ReserveAndFit(unittest.TestCase):
    """Primary-font width reservation, client-side text fitting, and wide-font browser checks."""

    def test_reserved_widths_emitted_and_used_by_page_script(self) -> None:
        tmp, root, m = _make_repo(with_git=False)
        try:
            errs, ctx = render.check(m, root, False)
            self.assertEqual(errs, [])
            page, _ = render.build(m, root, ctx)
        finally:
            tmp.cleanup()

        mt = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S)
        self.assertIsNotNone(mt)
        ov = json.loads(mt[1])["views"][""]
        for key in ("clerk>api", "api>db", "api>carrier"):
            entry = ov["arrows"][key]
            self.assertNotIn("r", entry)
            self.assertEqual(len(entry["f"]), len(GOOD["flows"]))
            self.assertGreater(entry["f"][0][0], 0)
        for pid in ("clerk", "api", "db", "carrier"):
            self.assertGreater(ov["parts"][pid], 0)

        tpl = Path(__file__).with_name("page.html").read_text(encoding="utf-8")
        for token in ("getComputedTextLength", "fontSize", "fit(", "fitParts("):
            self.assertIn(token, tpl)

    def test_browser_check_runs_both_default_and_wide_font(self) -> None:
        sample = "<!doctype html><style>body{font:15px sans-serif}</style><body>ok</body></html>"
        wide = render.browser_probe_html(sample, wide_font=True)
        self.assertIn("Verdana", wide)
        self.assertIn("DejaVu Sans", wide)
        self.assertNotIn("Verdana", render.browser_probe_html(sample, wide_font=False))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fake_chrome = root / "fake-chrome"
            fake_chrome.write_text("", encoding="utf-8")
            html_file = root / "page.html"
            html_file.write_text(sample, encoding="utf-8")

            seen_html: list[str] = []
            clean_dom = _probe_dom({"checked": True, "breaches": []})
            breach_dom = _probe_dom({"checked": True, "breaches": [{"picture": "", "flow": 0, "kind": "part", "a": "a>b", "a_text": "1 step", "b": "b"}]})

            def run_both_clean(cmd, **kw):
                seen_html.append(Path(cmd[-1].removeprefix("file://")).read_text(encoding="utf-8"))
                return subprocess.CompletedProcess(cmd, 0, clean_dom, "")

            with mock.patch.dict(os.environ, {}, clear=False), \
                 mock.patch.object(render, "CHROME", str(fake_chrome)), \
                 mock.patch.object(subprocess, "run", side_effect=run_both_clean):
                os.environ.pop("EXPLAIN_CODEBASE_CHROME", None)
                res = render.browser_check(html_file)
            self.assertEqual(res, [])
            self.assertEqual(len(seen_html), 2)
            self.assertNotIn("Verdana", seen_html[0])
            self.assertIn("Verdana", seen_html[1])
            self.assertIn("DejaVu Sans", seen_html[1])

            calls = iter([subprocess.CompletedProcess([], 0, clean_dom, ""),
                          subprocess.CompletedProcess([], 0, breach_dom, "")])
            with mock.patch.dict(os.environ, {}, clear=False), \
                 mock.patch.object(render, "CHROME", str(fake_chrome)), \
                 mock.patch.object(subprocess, "run", side_effect=lambda *a, **kw: next(calls)):
                os.environ.pop("EXPLAIN_CODEBASE_CHROME", None)
                res2 = render.browser_check(html_file)
            self.assertEqual(len(res2), 1)
            self.assertEqual(res2[0].get("font"), "wide")



class BrowserTolerance(unittest.TestCase):
    """The browser check must not fail a label that layout placed exactly at its margin.

    Chrome samples curves and hints glyphs differently from render.py; a label placed at exactly
    ARROW_MARGIN from its arrow once measured 0.01 px closer in Chrome and the real map was refused.
    """

    def test_browser_margins_sit_one_pixel_inside_layout_margins(self) -> None:
        probe = render.BROWSER_PROBE
        self.assertIn("TOLERANCE = 1", probe)
        self.assertIn("MARGIN = 3 - TOLERANCE", probe)
        self.assertIn(f"ARROW_MARGIN = {render.ARROW_MARGIN:g} - TOLERANCE", probe)
        self.assertEqual(render.MARGIN, 3.0)


class ClosingInvariants(unittest.TestCase):
    """Regression tests for invariants 1-7 covering the full class of each audit finding."""

    def test_inv1_root_docs_only_no_ancestor_symlinks_and_no_core_worktree_escape(self) -> None:
        tmp, root, m = _make_repo(with_git=True)
        outside = tempfile.TemporaryDirectory()
        try:
            out_dir = Path(outside.name).resolve()
            no_chrome = root / "no-chrome"
            map_path = root / "docs/how-it-works.json"
            map_path.write_text(json.dumps(m), encoding="utf-8")

            # 1a. Planted core.worktree must not move the repository root or blind citation/staleness checks.
            ext_secret = root.parent / "outside-secret.py"
            ext_secret.write_text("secret = 1\n", encoding="utf-8")
            try:
                subprocess.run(["git", "-C", str(root), "config", "core.worktree", "../.."], check=True)
                found_root, in_git = render.find_root(map_path)
                self.assertTrue(in_git)
                self.assertEqual(found_root.resolve(), root)
                # Dirty detection still works even with core.worktree poisoned.
                (root / "src/api.py").write_text("".join(f"edited {i}\n" for i in range(1, 13)), encoding="utf-8")
                errs, ctx = render.check(copy.deepcopy(m), found_root, True)
                self.assertEqual(errs, [])
                self.assertIn("src/api.py", ctx["dirty"])
                (root / "src/api.py").write_text("".join(f"line {i}\n" for i in range(1, 13)), encoding="utf-8")
                subprocess.run(["git", "-C", str(root), "config", "--unset", "core.worktree"], check=True)
            finally:
                ext_secret.unlink(missing_ok=True)

            # 1b. Ancestor directory symlinks (repo/sub -> outside, nested, and inside docs/) are refused.
            (out_dir / "docs").mkdir()
            (out_dir / "src").mkdir()
            (out_dir / "src/api.py").write_text("".join(f"line {i}\n" for i in range(1, 13)), encoding="utf-8")
            (out_dir / "src/schema.sql").write_text("create table orders (id);\n", encoding="utf-8")
            m_nogit = copy.deepcopy(GOOD)
            (out_dir / "docs/how-it-works.json").write_text(json.dumps(m_nogit), encoding="utf-8")
            (out_dir / "nested/docs").mkdir(parents=True)
            (out_dir / "nested/src").mkdir()
            (out_dir / "nested/src/api.py").write_text("".join(f"line {i}\n" for i in range(1, 13)), encoding="utf-8")
            (out_dir / "nested/src/schema.sql").write_text("create table orders (id);\n", encoding="utf-8")
            (out_dir / "nested/docs/how-it-works.json").write_text(json.dumps(m_nogit), encoding="utf-8")

            (root / "sub").symlink_to(out_dir)
            (root / "docs/link").symlink_to(out_dir)
            (root / "docs/real").mkdir()
            (root / "docs/real/in.json").write_text(json.dumps(m), encoding="utf-8")
            (root / "docs/alias").symlink_to(root / "docs/real")

            for bad_map, victim_html in (
                (root / "sub/docs/how-it-works.json", out_dir / "docs/how-it-works.html"),
                (root / "sub/nested/docs/how-it-works.json", out_dir / "nested/docs/how-it-works.html"),
                (root / "docs/link/docs/how-it-works.json", out_dir / "docs/how-it-works.html"),
                (root / "docs/alias/in.json", root / "docs/real/in.html"),
            ):
                rc, out = _run_main(bad_map, chrome=no_chrome)
                self.assertEqual(rc, 1, f"expected refusal for {bad_map}, got rc={rc}: {out}")
                self.assertFalse(victim_html.exists(), f"unexpected write at {victim_html}")

            # 1c. Maps outside docs/ (in git or non-git) are refused.
            (root / "root-map.json").write_text(json.dumps(m), encoding="utf-8")
            (root / "other").mkdir()
            (root / "other/map.json").write_text(json.dumps(m), encoding="utf-8")
            (out_dir / "plain.json").write_text(json.dumps(m_nogit), encoding="utf-8")
            for non_docs in (root / "root-map.json", root / "other/map.json", out_dir / "plain.json"):
                rc, out = _run_main(non_docs, chrome=no_chrome)
                self.assertEqual(rc, 1, f"expected refusal outside docs/ for {non_docs}, got rc={rc}: {out}")
                self.assertFalse(non_docs.with_suffix(".html").exists())
        finally:
            tmp.cleanup()
            outside.cleanup()

    def test_inv2_git_no_repo_execution_no_status_quote_hex_ref_and_no_symlink_cites(self) -> None:
        tmp, root, m = _make_repo(with_git=True)
        marker_dir = tempfile.TemporaryDirectory()
        try:
            pwn = Path(marker_dir.name).resolve() / "pwned"
            script = Path(marker_dir.name).resolve() / "evil.sh"
            script.write_text(f"#!/bin/sh\necho \"$@\" >> {pwn}\nexit 0\n", encoding="utf-8")
            script.chmod(0o755)

            # Non-ASCII cited file + .gitattributes filter/diff drivers on src/api.py (filtered files are unverified per Finding 4).
            (root / "src/café.py").write_text("line 1\nline 2\n", encoding="utf-8")
            (root / ".gitattributes").write_text("src/api.py filter=evil diff=evil\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "cafe"], check=True)

            # Forge a commit with a gpgsig header to trigger log.showSignature / gpg.program if unguarded.
            raw_c = subprocess.run(["git", "-C", str(root), "cat-file", "commit", "HEAD"], capture_output=True, text=True, check=True).stdout
            hdr, body = raw_c.split("\n\n", 1)
            signed = hdr + "\ngpgsig -----BEGIN PGP SIGNATURE-----\n fake\n -----END PGP SIGNATURE-----\n\n" + body
            signed_sha = subprocess.run(["git", "-C", str(root), "hash-object", "-t", "commit", "-w", "--stdin"],
                                        input=signed, capture_output=True, text=True, check=True).stdout.strip()
            subprocess.run(["git", "-C", str(root), "update-ref", "HEAD", signed_sha], check=True)
            m["commit"] = signed_sha
            m["parts"][1]["src"] = ["src/api.py:1-12", "src/café.py:1-2"]

            # Plant hostile .git/config keys across every execution vector.
            for k, v in (
                ("filter.evil.clean", f"{script} clean"),
                ("filter.evil.smudge", f"{script} smudge"),
                ("filter.evil.process", f"{script} process"),
                ("diff.evil.textconv", f"{script} textconv"),
                ("diff.external", f"{script} extdiff"),
                ("log.showSignature", "true"),
                ("gpg.program", str(script)),
                ("core.fsmonitor", f"{script} fsmonitor"),
                ("core.hooksPath", str(Path(marker_dir.name).resolve())),
            ):
                subprocess.run(["git", "-C", str(root), "config", k, v], check=True)

            # Rewrite src/api.py with identical bytes to make index stat stale (as after unpacking an archive).
            (root / "src/api.py").write_text("".join(f"line {i}\n" for i in range(1, 13)), encoding="utf-8")

            # Spy on git subprocess invocations: no `git status`, must pass core.quotePath=false.
            seen_cmds: list[list[str]] = []
            real_run = subprocess.run

            def spy(cmd, *a, **kw):
                seen_cmds.append(list(cmd))
                return real_run(cmd, *a, **kw)

            with mock.patch.object(subprocess, "run", side_effect=spy):
                errs, ctx = render.check(copy.deepcopy(m), root, True)
            self.assertEqual(errs, [])
            self.assertEqual(ctx["dirty"], set())
            self.assertFalse(pwn.exists(), f"planted git config executed: {pwn.read_text() if pwn.exists() else ''}")
            self.assertTrue(seen_cmds)
            for cmd in seen_cmds:
                self.assertNotIn("status", cmd, f"git status must not be used: {cmd}")
                self.assertIn("core.quotePath=false", cmd)

            # Unstaged and staged modifications to non-ASCII cited file are detected accurately.
            (root / "src/café.py").write_text("edited 1\nline 2\n", encoding="utf-8")
            errs_u, ctx_u = render.check(copy.deepcopy(m), root, True)
            self.assertEqual(errs_u, [])
            self.assertIn("src/café.py", ctx_u["dirty"])
            self.assertFalse(pwn.exists())

            # Stage and commit without letting the test's own git commands invoke the planted filter/fsmonitor/hooks.
            safe_git = ["git", "-C", str(root), "-c", "filter.evil.clean=", "-c", "filter.evil.process=",
                        "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
            subprocess.run([*safe_git, "add", "src/café.py"], check=True)
            errs_s, ctx_s = render.check(copy.deepcopy(m), root, True)
            self.assertEqual(errs_s, [])
            self.assertIn("src/café.py", ctx_s["dirty"])

            # Committed change to non-ASCII file is reported with attribution.
            subprocess.run([*safe_git, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "edit cafe"], check=True)
            errs_c, _ = render.check(copy.deepcopy(m), root, True)
            self.assertTrue(any("src/café.py changed since" in e and 'part "api"' in e for e in errs_c), errs_c)
            m["commit"] = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

            # Hex-named ref (refs/heads/deadbee) must NOT pass as commit SHA.
            fake_hex = "deadbee" if not m["commit"].lower().startswith("deadbee") else "cafebab"
            subprocess.run(["git", "-C", str(root), "branch", fake_hex, "HEAD"], check=True)
            m_ref = copy.deepcopy(m)
            m_ref["commit"] = fake_hex
            self.assertTrue(render.check(m_ref, root, True)[0], "hex-named branch ref must not satisfy commit")

            # Intra-repo symlinks (both file symlink and intermediate dir symlink) must be refused in citations.
            (root / "src/alias.py").symlink_to("api.py")
            (root / "src/linkdir").symlink_to(".")
            for sym_cite in ("src/alias.py:1-3", "src/linkdir/api.py:1-3"):
                m_sym = copy.deepcopy(m)
                m_sym["parts"][1]["src"] = ["src/api.py:1-12", sym_cite]
                self.assertTrue(render.check(m_sym, root, True)[0], f"intra-repo symlink {sym_cite} must be refused")
        finally:
            tmp.cleanup()
            marker_dir.cleanup()

    def test_inv3_no_resting_labels_only_checked_open_question_labels(self) -> None:
        tmp, root, m = _make_repo(with_git=False)
        try:
            errs, ctx = render.check(m, root, False)
            self.assertEqual(errs, [])
            _, _, arrows = render.view(m["parts"], ctx["groups"], m["flows"], None)
            for arr in arrows.values():
                self.assertNotIn("resting_w", arr)
                self.assertNotIn("resting_size", arr)
                self.assertNotIn("label", arr)
            page, _ = render.build(m, root, ctx)
        finally:
            tmp.cleanup()

        # No resting label metadata ("r") in embedded JSON and no pre-populated resting <text> labels.
        mt = re.search(r'<script type="application/json" id="data">(.*?)</script>', page, re.S)
        self.assertIsNotNone(mt)
        ov = json.loads(mt[1])["views"][""]
        for entry in ov["arrows"].values():
            self.assertNotIn("r", entry)
        for text_el in re.findall(r'<text class="arrow[^"]*"[^>]*>.*?</text>', page):
            self.assertNotIn("data-label=", text_el)
            self.assertTrue(text_el.endswith("></text>"), f"arrow <text> should start empty: {text_el}")

        # page.html must never reveal unchecked labels on hover or give them pointer events.
        tpl = Path(__file__).with_name("page.html").read_text(encoding="utf-8")
        self.assertIn("pointer-events: none", tpl)
        self.assertNotIn("svg text.arrow:not(.on):not(.now)", tpl)
        self.assertNotIn("m.r", tpl)
        self.assertNotIn("dataset.label", tpl)

    def test_inv4_unbounded_text_refused_wide_chars_widened_and_9px_floor(self) -> None:
        tmp, root, m = _make_repo(with_git=False)
        try:
            # Control chars (incl. newline, tab, NUL, U+2028), format chars (bidi RLO, wide Arabic Cf), PUA refused across all text fields.
            bad_strings = ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x1bb", "a\u2028b", "a\u202eb", "a\u0604b", "a\ue000b")
            field_mutators = (
                lambda x, s: x.__setitem__("summary", s),
                lambda x, s: x.__setitem__("title", s),
                lambda x, s: x["parts"][1].__setitem__("label", s),
                lambda x, s: x["parts"][1].__setitem__("does", s),
                lambda x, s: x["flows"][0].__setitem__("question", s),
                lambda x, s: x["flows"][0]["steps"][0].__setitem__("verb", s),
                lambda x, s: x["flows"][0]["steps"][2].__setitem__("note", s),
                lambda x, s: x.__setitem__("unknowns", [s]),
            )
            for idx, mutate in enumerate(field_mutators):
                for bad_s in bad_strings:
                    mc = copy.deepcopy(m)
                    mutate(mc, bad_s)
                    self.assertTrue(render.check(mc, root, False)[0], f"field {idx} should refuse {bad_s!r}")

            # Group label and does also refuse control chars.
            mg = copy.deepcopy(m)
            mg["groups"] = [{"id": "grp", "label": "Bad\nGroup", "does": "Line1\nLine2"}]
            mg["parts"][1]["group"] = mg["parts"][2]["group"] = "grp"
            self.assertGreaterEqual(len(render.check(mg, root, False)[0]), 2)

            # >2 combining marks on one character refused (both Mn and Me); <=2 combining marks allowed.
            for bad_marks in ("a" + "\u0301" * 3, "a" + "\u0302" * 15, "a" + "\u20dd" * 3):
                mc = copy.deepcopy(m)
                mc["flows"][0]["steps"][0]["verb"] = bad_marks
                self.assertTrue(render.check(mc, root, False)[0], f"should refuse stacked marks {bad_marks!r}")
            m_ok_marks = copy.deepcopy(m)
            m_ok_marks["flows"][0]["steps"][0]["verb"] = "e\u0302\u0323 ok"  # 2 combining marks (decomposed Vietnamese)
            self.assertEqual(render.check(m_ok_marks, root, False)[0], [])

            # Unbounded archaic script character (Cuneiform 4.64 em) is refused.
            m_cun = copy.deepcopy(m)
            m_cun["flows"][0]["steps"][0]["verb"] = "\U0001242b"
            self.assertTrue(render.check(m_cun, root, False)[0])

            # Wide bounded characters (U+2E3A-2E3B, U+FDFA-FDFD) are accepted with widened width estimates.
            self.assertGreaterEqual(render.text_w("\u2e3a", 12), 2.0 * 12)
            self.assertGreaterEqual(render.text_w("\u2e3b", 12), 3.0 * 12)
            self.assertGreaterEqual(render.text_w("\ufdfa", 12), 6.0 * 12)
            self.assertGreaterEqual(render.text_w("\ufdfd", 12), 6.5 * 12)
            for wide_ch in ("\u2e3a", "\u2e3b", "\ufdfa", "\ufdfd"):
                mw = copy.deepcopy(m)
                mw["flows"][0]["steps"][0]["verb"] = wide_ch
                self.assertEqual(render.check(mw, root, False)[0], [], f"wide char U+{ord(wide_ch):04X} should be accepted")
        finally:
            tmp.cleanup()

        # page.html fit() clamps at 9 px; BROWSER_PROBE reports fitted size < 9 px as a breach.
        tpl = Path(__file__).with_name("page.html").read_text(encoding="utf-8")
        fit_fn = re.search(r"function fit\([^)]*\)\s*\{.*?\n  \}", tpl, re.S)
        self.assertIsNotNone(fit_fn)
        self.assertIn("Math.max(9,", fit_fn[0])
        self.assertIn("tooSmall", tpl)
        self.assertIn("tooSmall", render.BROWSER_PROBE)
        msg = render.format_browser_breach({"picture": "", "flow": 0, "kind": "min_font", "a": "api>db", "a_text": "1 step", "b": "9px"})
        self.assertIn("9 px", msg)

    def test_inv5_malformed_inputs_never_crash_and_no_internal_skip_key(self) -> None:
        tmp, root, m = _make_repo(with_git=False)
        try:
            no_chrome = root / "no-chrome"
            # Lone surrogates in text or citations never produce a traceback in check() or main().
            for mut in (
                lambda x: x.__setitem__("summary", "bad \ud800 text"),
                lambda x: x.__setitem__("unknowns", ["bad \ud800 unknown"]),
                lambda x: x["parts"][1].__setitem__("src", ["src/api\ud800.py:1-2"]),
                lambda x: x["parts"][1].__setitem__("src", ["src/api\x00.py:1-2"]),
                lambda x: x["flows"][0]["steps"][1].__setitem__("src", "src/api.py:1-" + "9" * 5000),
            ):
                mc = copy.deepcopy(m)
                mut(mc)
                errs, _ = render.check(mc, root, False)
                self.assertTrue(errs)
                # Ensure every error message encodes cleanly as UTF-8 (no surrogate crash on print).
                "\n".join(errs).encode("utf-8")

            # CLI on lone surrogate JSON, NUL in path, and 200k-deep JSON exits 1 cleanly with no traceback.
            surr_path = root / "docs/surr.json"
            surr_path.write_text('{"summary": "bad \\ud800", "parts": [], "flows": []}', encoding="utf-8")
            rc_s, out_s = _run_main(surr_path, chrome=no_chrome)
            self.assertEqual(rc_s, 1)
            self.assertIn("error:", out_s)

            deep_path = root / "docs/deep.json"
            deep_path.write_text("[" * 200000 + "]" * 200000, encoding="utf-8")
            rc_d, out_d = _run_main(deep_path, chrome=no_chrome)
            self.assertEqual(rc_d, 1)
            self.assertIn("error:", out_d)

            rc_n, out_n = _run_main(Path(str(root / "docs/bad\x00.json")), chrome=no_chrome)
            self.assertEqual(rc_n, 1)
            self.assertIn("error:", out_n)

            # Internal _skip key in input JSON must never be honoured.
            m_skip = copy.deepcopy(m)
            m_skip["flows"][0]["steps"][2]["_skip"] = True
            errs_sk, ctx_sk = render.check(m_skip, root, False)
            self.assertEqual(errs_sk, [])
            self.assertEqual(len(m_skip["flows"][0]["steps"]), 3)
            _, counts = render.build(m_skip, root, ctx_sk)
            self.assertEqual(counts["steps"], 3)
        finally:
            tmp.cleanup()

    def test_inv6_chrome_none_skips_browser_check_and_docs_explain_it(self) -> None:
        tmp, root, m = _make_repo(with_git=False)
        try:
            map_path = root / "docs/how-it-works.json"
            map_path.write_text(json.dumps(m), encoding="utf-8")
            broken_chrome = root / "broken-chrome"
            broken_chrome.write_text("#!/bin/sh\nexit 133\n", encoding="utf-8")
            broken_chrome.chmod(0o755)

            # Without opt-out, a broken discovered browser fails closed.
            rc_fail, _ = _run_main(map_path, chrome=broken_chrome)
            self.assertEqual(rc_fail, 1)
            self.assertFalse(map_path.with_suffix(".html").exists())

            # With EXPLAIN_CODEBASE_CHROME=none, browser check and screenshots are skipped cleanly.
            rc_ok, out_ok = _run_main(map_path, chrome=broken_chrome, env={"EXPLAIN_CODEBASE_CHROME": "none"})
            self.assertEqual(rc_ok, 0, out_ok)
            self.assertIn("not checked in a browser", out_ok)
            self.assertTrue(map_path.with_suffix(".html").exists())
        finally:
            tmp.cleanup()

        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        skill = Path(__file__).with_name("SKILL.md").read_text(encoding="utf-8")
        self.assertIn("EXPLAIN_CODEBASE_CHROME=none", readme)
        self.assertIn("EXPLAIN_CODEBASE_CHROME=none", skill)
        self.assertIn("outside the sandbox", skill)

    def test_inv7_readme_and_skill_match_code_exactly(self) -> None:
        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        skill = Path(__file__).with_name("SKILL.md").read_text(encoding="utf-8")
        self.assertNotIn("two real maps", readme)
        self.assertNotIn("scrambled", readme)
        self.assertIn("four synthetic maps", readme)
        self.assertIn("open question's labels", readme)
        self.assertNotIn("checks every label", readme)
        self.assertNotIn("It prints every problem and writes nothing", skill)
        self.assertIn("beside", skill)


class ReplayFixes(unittest.TestCase):
    """Regression tests for replay audit findings 1-8 and 10 (plus Finding 9 Mc over-reservation)."""

    def test_f1_lazy_fetch_blocked_failed_git_is_error_and_readme_trust(self) -> None:
        tmp, root, m = _make_repo(with_git=True)
        helper_dir = tempfile.TemporaryDirectory()
        try:
            hd = Path(helper_dir.name).resolve()
            pwn = hd / "lazy_pwned"
            uploadpack = hd / "evil_uploadpack.sh"
            uploadpack.write_text(f"#!/bin/sh\necho PWNED >> {pwn}\nexit 1\n", encoding="utf-8")
            uploadpack.chmod(0o755)

            # Delete HEAD's loose tree object and configure a promisor partial-clone remote.
            tree_sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD^{tree}"],
                                      capture_output=True, text=True, check=True).stdout.strip()
            obj = root / ".git/objects" / tree_sha[:2] / tree_sha[2:]
            self.assertTrue(obj.exists())
            obj.unlink()
            for k, v in (
                ("core.repositoryformatversion", "1"),
                ("extensions.partialClone", "evil"),
                ("remote.evil.url", str(hd)),
                ("remote.evil.promisor", "true"),
                ("remote.evil.uploadpack", str(uploadpack)),
                ("protocol.file.allow", "always"),
            ):
                subprocess.run(["git", "-C", str(root), "config", k, v], check=True)

            seen: list[tuple[list[str], dict]] = []
            real_run = subprocess.run

            def spy(cmd, *a, **kw):
                seen.append((list(cmd), dict(kw.get("env") or {})))
                return real_run(cmd, *a, **kw)

            with mock.patch.object(subprocess, "run", side_effect=spy):
                errs, _ = render.check(copy.deepcopy(m), root, True)
            self.assertFalse(pwn.exists(), "promisor uploadpack must never execute")
            self.assertTrue(errs, "failing git diff/ls-tree must produce an error, not pass silently")
            self.assertTrue(seen)
            for cmd, env in seen:
                self.assertEqual(env.get("GIT_NO_LAZY_FETCH"), "1")
                self.assertIn("protocol.allow=never", cmd)
                for proto in ("file", "ext", "ssh", "git", "http", "https"):
                    self.assertIn(f"protocol.{proto}.allow=never", cmd)

            map_path = root / "docs/how-it-works.json"
            map_path.write_text(json.dumps(m), encoding="utf-8")
            rc, out = _run_main(map_path, chrome=root / "no-chrome")
            self.assertEqual(rc, 1, out)
            self.assertFalse(map_path.with_suffix(".html").exists())
            self.assertFalse(pwn.exists())
        finally:
            tmp.cleanup()
            helper_dir.cleanup()

        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        self.assertIn("## Trust", readme)
        self.assertIn("fresh clone", readme)

    def test_f2_linked_worktree_and_submodule_accepted(self) -> None:
        tmp, root, m = _make_repo(with_git=True)
        extra = tempfile.TemporaryDirectory()
        try:
            ed = Path(extra.name).resolve()
            no_chrome = root / "no-chrome"

            # 1) Linked worktree (with .git file -> <main>/.git/worktrees/<name>)
            wt = ed / "wt"
            subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(wt)], check=True)
            (wt / "docs").mkdir(exist_ok=True)
            wt_map = wt / "docs/how-it-works.json"
            wt_map.write_text(json.dumps(m), encoding="utf-8")
            found_wt, in_git_wt = render.find_root(wt_map)
            self.assertTrue(in_git_wt)
            self.assertEqual(found_wt.resolve(), wt)
            rc_wt, out_wt = _run_main(wt_map, chrome=no_chrome)
            self.assertEqual(rc_wt, 0, out_wt)
            self.assertTrue(wt_map.with_suffix(".html").exists())

            # core.worktree poisoned inside the worktree's gitdir still keeps root pinned to wt.
            subprocess.run(["git", "-C", str(wt), "config", "core.worktree", "../.."], check=True)
            found_wt2, _ = render.find_root(wt_map)
            self.assertEqual(found_wt2.resolve(), wt)
            errs_wt, _ = render.check(copy.deepcopy(m), found_wt2, True)
            self.assertEqual(errs_wt, [])
            subprocess.run(["git", "-C", str(wt), "config", "--unset", "core.worktree"], check=True)

            # 2) Submodule (with .git file -> ../.git/modules/sub)
            sup = ed / "super"
            sup.mkdir()
            subprocess.run(["git", "-C", str(sup), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(sup), "-c", "protocol.file.allow=always",
                            "submodule", "add", "-q", str(root), "sub"], check=True)
            subprocess.run(["git", "-C", str(sup), "-c", "user.name=t", "-c", "user.email=t@t",
                            "commit", "-q", "-m", "add sub"], check=True)
            sub_root = sup / "sub"
            (sub_root / "docs").mkdir(exist_ok=True)
            sub_map = sub_root / "docs/how-it-works.json"
            sub_map.write_text(json.dumps(m), encoding="utf-8")
            found_sub, in_git_sub = render.find_root(sub_map)
            self.assertTrue(in_git_sub)
            self.assertEqual(found_sub.resolve(), sub_root)
            rc_sub, out_sub = _run_main(sub_map, chrome=no_chrome)
            self.assertEqual(rc_sub, 0, out_sub)
            self.assertTrue(sub_map.with_suffix(".html").exists())
        finally:
            tmp.cleanup()
            extra.cleanup()

    def test_f3_ordinary_multilingual_text_accepted_and_hostile_refused(self) -> None:
        tmp, root, m = _make_repo(with_git=False)
        try:
            # Every example listed by the replay audit as wrongly refused must now pass.
            allowed_texts = (
                "می\u200cخواند",           # Persian/Kurdish ZWNJ
                "ශ්\u200dරී",               # Sinhala ZWJ
                "র\u200d্যাব",              # Bengali ZWJ
                "👩\u200d💻",               # emoji ZWJ
                "👨\u200d👩\u200d👧",       # emoji family ZWJ
                "🏳\ufe0f\u200d🌈",        # rainbow flag ZWJ
                "🏴\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",  # England tag sequence
                "soft\u00adhyphen",         # soft hyphen
                "hebrew\u200e\u200f\u061c", # LRM, RLM, ALM
                "पढ़ें",                    # Hindi with nukta
                "चीज़ों",                   # Hindi with nukta + vowel + anusvara
                "ज़िंदगी",                  # Hindi
                "ਜ਼ਿੰਦਗੀ",                  # Punjabi
                "မြို့",                    # Burmese (4 marks)
                "ញ៉ាံ",                     # Khmer
                "སྐྱེས",                    # Tibetan
                "הַשָּׁנָה",                  # vocalised Hebrew
                "🫠🫨",                     # Unicode 14/15 emoji (unassigned on Python 3.10)
                "\u0378 ok",                # unassigned codepoint (must not depend on Python Unicode version)
                "𞤀𞤣𞤤𞤢𞤥",                   # Adlam script
                "𝑓(𝑥)",                     # math italics
            )
            for sample in allowed_texts:
                mc = copy.deepcopy(m)
                mc["flows"][0]["steps"][0]["verb"] = sample
                errs, _ = render.check(mc, root, False)
                self.assertEqual(errs, [], f"ordinary text {sample!r} must be accepted: {errs}")

            # Folder and file names containing ZWNJ must be accepted in citations and main().
            zwnj_dir = root / "src/می\u200cخواند"
            zwnj_dir.mkdir()
            (zwnj_dir / "mod.py").write_text("a\nb\nc\n", encoding="utf-8")
            m_zwnj = copy.deepcopy(m)
            m_zwnj["parts"][1]["src"] = ["src/api.py:1-12", "src/می\u200cخواند/mod.py:1-2"]
            self.assertEqual(render.check(m_zwnj, root, False)[0], [])

            zwnj_proj = root / "می\u200cخواند_proj"
            (zwnj_proj / "src").mkdir(parents=True)
            (zwnj_proj / "docs").mkdir()
            (zwnj_proj / "src/api.py").write_text("".join(f"line {i}\n" for i in range(1, 13)), encoding="utf-8")
            (zwnj_proj / "src/schema.sql").write_text("create table orders (id);\n", encoding="utf-8")
            zwnj_map = zwnj_proj / "docs/how-it-works.json"
            zwnj_map.write_text(json.dumps(GOOD), encoding="utf-8")
            rc_z, out_z = _run_main(zwnj_map, chrome=root / "no-chrome")
            self.assertEqual(rc_z, 0, out_z)

            # Hostile inputs must still be refused.
            hostile_texts = (
                "a" + "\u0301" * 27,
                "a" + "\u0302" * 15,
                "a" + "\u0301" * 3,
                "a\u0301\u0301\u200c\u0301\u0301",  # ZWNJ cannot split generic mark count
                "a" + "\u20dd" * 3,
                "क" + "\u093c" * 5,                 # > 4 marks total
                "a\nb", "a\x00b", "a\u2028b", "a\ud800b", "a\ue000b",
                "a\u202ab", "a\u202eb", "a\u2066b", "a\u2069b",  # bidi embeddings/overrides/isolates
                "a\u0604b",                         # wide Arabic Cf
                "a\U000e0061b",                     # stray tag character outside emoji tag sequence
                "\U0001242b",                       # Cuneiform 4.64 em
            )
            for bad in hostile_texts:
                mc = copy.deepcopy(m)
                mc["flows"][0]["steps"][0]["verb"] = bad
                self.assertTrue(render.check(mc, root, False)[0], f"hostile text {bad!r} must be refused")

            # Spacing vowel signs (Mc) must not badly over-reserve ordinary Hindi/Bengali/Tamil.
            self.assertLess(render.text_w("चीज़ों की जाँच करता है", 12), 145.0)
            self.assertLess(render.text_w("আদেশ সংরক্ষণ করে", 12), 130.0)
            self.assertLess(render.text_w("ஆணையைச் சேமிக்கிறது", 12), 160.0)
        finally:
            tmp.cleanup()

    def test_f4_crlf_autocrlf_filtered_oversized_and_submodule_files(self) -> None:
        # 1) * text eol=crlf clone: CRLF on disk vs LF in HEAD is clean, real edit is dirty.
        tmp, root, m = _make_repo(with_git=True)
        try:
            (root / ".gitattributes").write_text("* text eol=crlf\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "attrs"], check=True)
            m["commit"] = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            (root / "src/api.py").write_bytes("".join(f"line {i}\r\n" for i in range(1, 13)).encode("utf-8"))
            (root / "src/schema.sql").write_bytes(b"create table orders (id);\r\n")
            errs, ctx = render.check(copy.deepcopy(m), root, True)
            self.assertEqual(errs, [])
            self.assertEqual(ctx["dirty"], set())
            self.assertEqual(ctx.get("unverified", set()), set())
            # Real edit with CRLF IS dirty.
            (root / "src/api.py").write_bytes("".join(f"edited {i}\r\n" for i in range(1, 13)).encode("utf-8"))
            _, ctx_d = render.check(copy.deepcopy(m), root, True)
            self.assertEqual(ctx_d["dirty"], {"src/api.py"})
        finally:
            tmp.cleanup()

        # 2) core.autocrlf=true without .gitattributes
        tmp2, root2, m2 = _make_repo(with_git=True)
        try:
            subprocess.run(["git", "-C", str(root2), "config", "core.autocrlf", "true"], check=True)
            (root2 / "src/api.py").write_bytes("".join(f"line {i}\r\n" for i in range(1, 13)).encode("utf-8"))
            errs, ctx = render.check(copy.deepcopy(m2), root2, True)
            self.assertEqual(errs, [])
            self.assertEqual(ctx["dirty"], set())
            self.assertEqual(ctx.get("unverified", set()), set())
        finally:
            tmp2.cleanup()

        # 3) Filtered (LFS-style) and oversized files are "not verified", not "dirty"
        tmp3, root3, m3 = _make_repo(with_git=True)
        try:
            (root3 / ".gitattributes").write_text("src/schema.sql filter=lfs\n", encoding="utf-8")
            (root3 / "src/big.bin").write_bytes(b"0123456789\n" * 60)
            subprocess.run(["git", "-C", str(root3), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root3), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "lfs-big"], check=True)
            m3["commit"] = subprocess.run(["git", "-C", str(root3), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            # Simulate smudged LFS content differing from stored pointer blob in HEAD
            (root3 / "src/schema.sql").write_text("smudged lfs payload\n", encoding="utf-8")
            m3["parts"][1]["src"] = ["src/"]
            with mock.patch.object(render, "MAX_FILE_BYTES", 500):
                errs, ctx = render.check(m3, root3, True)
            self.assertEqual(errs, [])
            self.assertEqual(ctx["dirty"], set())
            self.assertIn("src/schema.sql", ctx.get("unverified", set()))
            self.assertIn("src/big.bin", ctx.get("unverified", set()))
            page, _ = render.build(m3, root3, ctx)
            self.assertIn("not verified", page)
            self.assertNotIn("uncommitted changes", page)
        finally:
            tmp3.cleanup()

        # 4) Submodule cited from superproject is "not verified", not "dirty"
        tmp_sub, sub_src, _ = _make_repo(with_git=True)
        tmp_sup = tempfile.TemporaryDirectory()
        try:
            sup = Path(tmp_sup.name).resolve()
            (sup / "src").mkdir()
            (sup / "src/api.py").write_text("".join(f"line {i}\n" for i in range(1, 13)), encoding="utf-8")
            subprocess.run(["git", "-C", str(sup), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(sup), "-c", "protocol.file.allow=always",
                            "submodule", "add", "-q", str(sub_src), "vendor"], check=True)
            subprocess.run(["git", "-C", str(sup), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(sup), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "sup"], check=True)
            sup_sha = subprocess.run(["git", "-C", str(sup), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            m_sup = copy.deepcopy(GOOD)
            m_sup["commit"] = sup_sha
            m_sup["parts"][2]["src"] = ["vendor/src/schema.sql"]
            errs, ctx = render.check(m_sup, sup, True)
            self.assertEqual(errs, [])
            self.assertEqual(ctx["dirty"], set())
            self.assertIn("vendor/src/schema.sql", ctx.get("unverified", set()))
            page, _ = render.build(m_sup, sup, ctx)
            self.assertIn("not verified", page)
            self.assertNotIn("uncommitted changes", page)
        finally:
            tmp_sub.cleanup()
            tmp_sup.cleanup()

    def test_f5_sha256_full_64_char_commit_accepted(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            root = Path(tmp.name).resolve()
            (root / "src").mkdir()
            (root / "docs").mkdir()
            (root / "src/api.py").write_text("".join(f"line {i}\n" for i in range(1, 13)), encoding="utf-8")
            (root / "src/schema.sql").write_text("create table orders (id);\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "init", "--object-format=sha256", "-q"], check=True)
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], check=True)
            sha64 = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            self.assertEqual(len(sha64), 64)
            m = copy.deepcopy(GOOD)
            m["commit"] = sha64
            errs, ctx = render.check(copy.deepcopy(m), root, True)
            self.assertEqual(errs, [])
            self.assertEqual(ctx["commit"], sha64)
            self.assertEqual(ctx["dirty"], set())
            (root / "src/api.py").write_text("changed\n" * 12, encoding="utf-8")
            _, ctx2 = render.check(copy.deepcopy(m), root, True)
            self.assertEqual(ctx2["dirty"], {"src/api.py"})
        finally:
            tmp.cleanup()
        self.assertIn("7–64", Path(__file__).with_name("SKILL.md").read_text(encoding="utf-8"))

    def test_f6_literal_pathspecs_with_glob_chars(self) -> None:
        tmp, root, m = _make_repo(with_git=True)
        try:
            (root / "src/a.py").write_text("a1\na2\n", encoding="utf-8")
            (root / "src/[ab].py").write_text("b1\nb2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "glob"], check=True)
            base_sha = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
            # Change src/a.py (NOT cited); leave src/[ab].py (cited) untouched.
            (root / "src/a.py").write_text("changed\na2\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "src/a.py"], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change a"], check=True)
            m["commit"] = base_sha
            m["parts"][1]["src"] = ["src/api.py:1-12", "src/[ab].py:1-2"]
            errs, ctx = render.check(copy.deepcopy(m), root, True)
            self.assertEqual(errs, [], f"glob pathspec must not match uncited src/a.py: {errs}")
            self.assertEqual(ctx["dirty"], set())
        finally:
            tmp.cleanup()

    def test_f7_exact_case_required_for_cited_paths(self) -> None:
        tmp, root, m = _make_repo(with_git=True)
        try:
            m_bad = copy.deepcopy(m)
            m_bad["parts"][1]["src"] = ["SRC/API.py:1-12"]
            m_bad["flows"][0]["steps"][0]["src"] = "SRC/API.py:1-3"
            m_bad["flows"][0]["steps"][1]["src"] = "SRC/API.py:5-6"
            m_bad["flows"][0]["steps"][2]["src"] = "SRC/API.py:8-9"
            self.assertTrue(render.check(m_bad, root, True)[0], "wrongly cased citation must be refused in git")
            self.assertTrue(render.check(copy.deepcopy(m_bad), root, False)[0], "wrongly cased citation must be refused without git")
        finally:
            tmp.cleanup()

    def test_f8_benign_symlinks_above_root_accepted(self) -> None:
        tmp, root, m = _make_repo(with_git=True)
        extra = tempfile.TemporaryDirectory()
        try:
            ed = Path(extra.name).resolve()
            no_chrome = root / "no-chrome"
            (root / "docs/how-it-works.json").write_text(json.dumps(m), encoding="utf-8")

            # 1) Repository folder itself accessed through a symlink by absolute path.
            link_repo = ed / "repo-link"
            link_repo.symlink_to(root)
            rc1, out1 = _run_main(link_repo / "docs/how-it-works.json", chrome=no_chrome)
            self.assertEqual(rc1, 0, out1)
            self.assertTrue((root / "docs/how-it-works.html").exists())
            (root / "docs/how-it-works.html").unlink()

            # 2) ~/code/proj is a symlink to another git repo while ~ itself is a git repo.
            home_repo = ed / "home"
            (home_repo / "code").mkdir(parents=True)
            subprocess.run(["git", "-C", str(home_repo), "init", "-q"], check=True)
            (home_repo / "code/proj").symlink_to(root)
            rc2, out2 = _run_main(home_repo / "code/proj/docs/how-it-works.json", chrome=no_chrome)
            self.assertEqual(rc2, 0, out2)
            self.assertTrue((root / "docs/how-it-works.html").exists())
        finally:
            tmp.cleanup()
            extra.cleanup()

    def test_f10_readme_docs_rule_and_page_stamp_on_chrome_none(self) -> None:
        readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
        checks_sec = readme.split("## What the renderer checks", 1)[1].split("## ", 1)[0]
        bullets = "\n".join(ln for ln in checks_sec.splitlines() if ln.startswith("- "))
        self.assertIn("docs/", bullets)

        tmp, root, m = _make_repo(with_git=True)
        try:
            map_path = root / "docs/how-it-works.json"
            map_path.write_text(json.dumps(m), encoding="utf-8")
            broken_chrome = root / "broken-chrome"
            broken_chrome.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            broken_chrome.chmod(0o755)
            rc, out = _run_main(map_path, chrome=broken_chrome, env={"EXPLAIN_CODEBASE_CHROME": "none"})
            self.assertEqual(rc, 0, out)
            page = map_path.with_suffix(".html").read_text(encoding="utf-8")
            header = re.search(r"<header>.*?</header>", page, re.S)
            self.assertIsNotNone(header)
            self.assertIn("not checked in a browser", header[0])
        finally:
            tmp.cleanup()



class QuestionMarkup(unittest.TestCase):
    """A question is one line of text: the summary row is a flex box, so its words must sit in one child.

    A question with code in it once split into columns, one per piece of code.
    """

    def test_question_text_is_one_flex_item(self) -> None:
        html = render.esc  # keep the import visible to readers
        out = render.rich("What happens when I run `train.py`?")
        wrapped = f"<summary><span>{out}</span></summary>"
        src = Path(render.__file__).read_text(encoding="utf-8")
        self.assertIn('<summary><span>{rich(f["question"])}</span></summary>', src)
        self.assertIn("<code>", wrapped)

if __name__ == "__main__":
    unittest.main()
