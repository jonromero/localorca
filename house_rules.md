# House rules

LocalOrca gives this file to Claude every time it interviews you and plans a new project (`orca.py new`).
Edit it freely: it is YOUR standing taste and lessons, so every new plan starts from them instead of
Claude's defaults. A project can add its own `house_rules.md` in its folder; that one is read too, after this one.

## Pick the stack for the builder, not for a human
The builder is a mid-size local model that can only learn it was wrong from automatic checks.
Prefer the stack that gives it the MOST fast, automatic feedback.

- JavaScript projects use TypeScript checking from T01: `strict` tsconfig, `tsc --noEmit` is part of test_cmd.
  If no build step is wanted, write .ts files run by Node's built-in type stripping, or plain .js with
  `// @ts-check` and JSDoc types. TypeScript itself is an allowed dev dependency even when the app has
  "no dependencies".
- Python projects: type hints everywhere and `mypy` (or `pyright`) in test_cmd before pytest.
- Avoid Rust, C++ and other slow-compiling or borrow-checked languages unless the brief truly needs them.
- Pick the most common, boring library for each job (lots of training data), never a niche one.
- Pin versions. Vendor or install front-end libraries locally, never load them from a CDN by a bare URL.

## Tests are the only judge
- Every "done when" is checked by test_cmd. Never "in the browser", "visually" or "manually".
- Browser apps: logic in pure modules, UI as small testable functions, and a test that every file the page loads
  exists. Keep test_cmd fast and browser-free: Orca opens the real app in a headless browser itself.

## Walking skeleton first, then vertical slices (the way we build, always)
- T01 to about T05 produce an app that really RUNS end to end (server, page or entry point, one visible or usable
  result, however crude). A game: flat ground and one coloured block. Only then grow it.
- Every later task is a vertical slice: it adds one visible or usable thing to the running app. Never a layer of
  invisible plumbing, never a module nothing on screen needs yet.
- Tests fake only the OUTSIDE world (network, clock, the model). Never a stand-in for code this project wrote.
- Orca starts the real app (`preview_cmd`, which must read the PORT env variable) and opens it in a headless browser after
  EVERY task from the skeleton on. Console error, uncaught error, failed request or a blank page blocks the task.
- EVERY project gets a real product check, whatever the stack. Screen apps: the headless browser check above. Anything else
  (CLI, API, library, bot, pipeline): the skeleton task creates `scripts/smoke.sh` (or .py or .js) that runs the REAL
  product the way a user would, with real input and no mocks, and exits nonzero on any problem. Examples: a CLI run on a
  sample file with its real output compared; an API started on the PORT env variable and hit with curl; a library imported
  by a short real program. Never a project without one.
- Why: hours of green unit tests once ended in a blank page, because no test ever ran the real wiring.

## Small tasks
- One task = one function (or 2 or 3 tiny related ones), one test file, about 50 to 100 new lines.
- At most 3 behaviours in a "done when". More than that: split into several tasks.
- Never one task for "all the actions", "the whole agent turn" or "the whole UI". Split by function group.
- Many small tasks beat a few big ones: a blocked big task throws away the parts that worked.

## Small and plain
- Fewest dependencies that do the job. No frameworks for small apps.
- Source files about 150 lines at most, test files about 250. One clear job per file.
- One file per function group. Never plan "part 1 / part 2" of the same file. A group that outgrows one file becomes a
  folder of small files (`actions/move.js`, `actions/blocks.js`) plus a thin index that re-exports them.
- Why: the builder is a small model. A 500 line file cannot be read in one step, and rewriting it in one reply gets cut off.
- Secrets live in .env, which is git-ignored from T01, with a .env.example next to it.
