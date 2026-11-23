# explain-codebase

A skill for Claude Code that explains how a codebase works as one self-contained HTML page.

The page has two things on it: a **map** of the code's parts, and a few **flows**. A flow answers one question a reader would ask ("What happens when a clerk submits an order?") as numbered steps, and each step is an arrow on the map. The map has no arrows of its own; it is the union of the flows. Every part inside the repository cites its source files, directories or line ranges, and every observed step cites its call site's file and line range (actors, outside services and unverified steps omit citations and are drawn dashed); a renderer checks those citations against the code before it draws anything.

Large codebases are drawn at two levels. The overview shows at most ten boxes; parts that work together form a group, which the reader clicks to open into a picture of its own.

## What the renderer checks

`render.py` reads the map the agent writes (`docs/how-it-works.json`) and refuses to draw it, listing the problems found, when:

- the map is not under the repository root's `docs/` directory, or a path component between the root and the output is a symlink;
- a cited file or line does not exist, a cited path uses a symlink, or a file changed since the commit the map records;
- a step's evidence is not in the code of the part that acts;
- a step does not touch a part its flow has already reached, or a part is on no route;
- a picture (the overview or any opened group) would have more than ten boxes, shrink below readable scale, or exceed a text, flow or step limit;
- an arrow's label cannot be placed clear of the other labels, the boxes and the arrows of its question, including its own arrow.

When Chrome is available it checks the open question's labels in every picture (the overview and each opened group) in a real browser on a temporary copy and takes screenshots before placing `docs/how-it-works.html` beside the JSON; if the check fails, nothing is written.

## Install

```bash
git clone <this repository> ~/.claude/skills/explain-codebase
```

Requirements: Python 3.10 or later, with no third-party packages. [uv](https://docs.astral.sh/uv/) runs the renderer from its script header, and plain `python3` works too. Google Chrome or Chromium is optional. Without it, the screenshots and the browser check are skipped with a note. Set `EXPLAIN_CODEBASE_CHROME` to point at a browser in an unusual place, or `EXPLAIN_CODEBASE_CHROME=none` to skip the browser check and screenshots (for example inside a sandbox where an installed browser cannot start); the renderer then prints "not checked in a browser".

## Use

In Claude Code, in a repository:

```
/explain-codebase
/explain-codebase src/billing "What happens when an invoice is paid?"
```

Or ask in plain words: "how does this work", "draw me the architecture", "what happens when I run X". With no question given, the agent chooses the two to five things the code exists to do. `SKILL.md` holds the full method the agent follows.

The JSON map stays beside the page. A later session reads it instead of exploring the code again, and when the code changes the renderer names the parts and steps whose citations went stale.

The page needs no server: open the HTML file. It follows the system's light or dark setting, and the reader can switch between them.

## Trust

The renderer runs read-only git commands in the repository, and running git inside a `.git` folder from someone you do not trust is unsafe in general (as git's own documentation warns), so render such a repository from a fresh clone.


## Known limits

The renderer refuses obvious attempts to write or read outside the repository, but it is not a sandbox. These cases are known and not handled:

- A symlink inside the repository that points at another repository is followed if you give the renderer a path that runs through it. Run it on `docs/how-it-works.json` from the repository root.
- Files cited inside a git submodule are not checked for changes since the map's commit, and their GitHub links may not resolve.
- On macOS, a cited path that differs from the tracked one only in Unicode normalisation, or a directory renamed only in letter case, can escape the check for changed files.
## Tests

```bash
uv run test_render.py      # or: python3 test_render.py
```

`fixtures/` holds four synthetic maps with neutral words (`dense-1.json` through `dense-4.json`) used by the layout and browser label-clearance tests.

## Credits

The method draws on ideas from [Archify](https://github.com/tt-a1i/archify) (pinned commit, cited line ranges, a stopping rule), [GitDiagram](https://github.com/ahmedkhaleel2004/gitdiagram) (the code that makes a call owns the arrow), [Visual Explainer](https://github.com/nicobailon/visual-explainer) (one claim per figure, labelled arrows) and [CodeBoarding](https://github.com/CodeBoarding/CodeBoarding) (relationships from code analysis, names from the model). No code is taken from them.

## License

MIT. See `LICENSE`.
