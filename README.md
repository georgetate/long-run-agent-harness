# Current state

This repository is currently largely untested. It was drafted using Claude Code from an [Anthropic article](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) and never verified by a human. Additionally, the paths will probably need adjustment because I moved it's home directory. It's ideas are potentially promising, though nothing in here has ever been verified by a human at this point: Thursday August 20th, 2026. 

# Agent harness

Runs Claude Code against a repository across many sessions, one feature at a time, until the work is done or something stops it. It is a developer tool: it never ships, and it is not part of this project's pipeline. It exists to take an idea you have already thought through and grind it out over hours you are not watching.

It is an implementation of Anthropic's [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents), with its Puppeteer-and-web-apps specifics generalised and its prompt-only rules replaced by enforcement. Why each piece is shaped the way it is: [DESIGN.md](DESIGN.md).

## The idea in one paragraph

An agent session has a context window, and a real project does not fit in one. So the work is done in shifts, and each new shift arrives with no memory of the last. Everything one session wants the next to know has to be written into the repository before it stops: what the project is (`spec.md`), what is left (`feature_list.json`), what happened (`progress.md` and the git log), and how to start it up (`init.sh`). The harness's job is to set that up once, then run session after session against it, and to stop loudly when something is wrong rather than quietly when nothing is happening.

## What you need

- The `claude` CLI on `PATH`, version 2.0 or newer, already authenticated. The harness drives it as a subprocess.
- `git`, and a target repository that is a git work tree with a clean status.
- Python 3.11 or newer. No packages: the harness is standard library only, deliberately.

## Who pays for the sessions

The harness never chooses an authentication method. It runs `claude`, which uses whatever it is already logged in as, so a session bills exactly the way your interactive Claude Code sessions bill. On a claude.ai login that is your subscription, not metered API credits. `doctor` prints which one is in force before you spend anything:

```
harness: auth, signed in to a claude.ai account on the max plan, so sessions draw on that subscription rather than metered API credits
```

Two things worth knowing:

- **An exported `ANTHROPIC_API_KEY` changes the answer.** Sessions inherit this process's environment, so a key in your shell is a key the session uses, and usage goes to that key instead. `doctor` and `run` both say so when they see one.
- **The dollar figures are still real numbers.** The CLI reports `total_cost_usd` per session whether or not anything was charged; on a subscription, read it as the equivalent cost of the tokens rather than as a bill. `--max-budget-usd` is enforced regardless: a session that exceeds it ends with `error_max_budget_usd`, and the loop stops on the first errored session.

On a subscription, an unattended run draws down the same rolling usage limits as your own sessions do. A long overnight run and a working morning compete for the same allowance, which is an argument for `--total-budget-usd` and a modest `--sessions` rather than for leaving it running open-ended.

## Quick start

```bash
HARNESS=~/Code/automationsLLC/software/roof-bidding-pipeline/developer-tooling-agent-harness/scripts/agent-harness/harness.py

# 1. Check the environment before spending anything.
uv run --script $HARNESS doctor --repo ~/code/my-project

# 2. Write the environment once, from a spec you have already thought through.
uv run --script $HARNESS init --repo ~/code/my-project --spec ~/notes/my-idea.md

# 3. See exactly what a session would be sent, without running one.
uv run --script $HARNESS run --repo ~/code/my-project --dry-run

# 4. Run it.
uv run --script $HARNESS run --repo ~/code/my-project --sessions 20 --total-budget-usd 40

# 5. Ask where it got to, any time, without starting a session.
uv run --script $HARNESS status --repo ~/code/my-project
```

`harness.py` is the entry point; everything else in this directory is plumbing it calls. It carries inline script metadata, so `uv run --script` works from inside any repository, including ones with no Python environment of their own. Always through `uv`, never a bare `python3`.

Worth an alias:

```bash
alias harness='uv run --script ~/Code/automationsLLC/software/roof-bidding-pipeline/developer-tooling-agent-harness/scripts/agent-harness/harness.py'
harness status --repo .
```

## What is in here

```
agent-harness/
├── harness.py                     # the entry point. Everything else is plumbing it calls
├── README.md                      # this file: how to run it, and what it does to a repository
├── DESIGN.md                      # why each part is shaped this way, and what real runs changed
├── agent_harness/                 # the harness itself; imported, never run directly
│   ├── __init__.py
│   ├── cli.py                     # the four commands, their options, and errors as one readable line
│   ├── config.py                  # the pinned CLI contract (flags, result keys, versions), defaults, value types
│   ├── preflight.py               # everything asserted before a token is spent, including how sessions authenticate
│   ├── session.py                 # the only module that knows a session is a subprocess: build argv, run, parse
│   ├── loop.py                    # orchestration: one initializer session, then coding sessions until a named stop
│   ├── features.py                # the four states, the transition table, the digest, tamper detection
│   ├── guard.py                   # the allow/deny decision the hook applies; pure, and unit tested hardest
│   ├── records.py                 # session numbering, and rendering an event stream into a readable transcript
│   ├── workspace.py               # the `.agent-harness/` layout the harness keeps inside a target repository
│   └── prompts.py                 # loads `prompts/*.md` and substitutes their `{{TOKEN}}` markers
├── prompts/                       # files rather than string literals, because this is what gets tuned
│   ├── initializer.md             # first session only: the feature list, progress log, init.sh and serve.sh
│   ├── coding.md                  # every later session: get bearings, one feature, test first, record, commit
│   └── system_contract.md         # appended to the system prompt of both; the rules that hold all session
├── tools/                         # invoked from inside the target repository, by absolute path
│   ├── mark_feature.py            # the only sanctioned way to change a feature's state
│   └── protect_feature_list.py    # the PreToolUse hook; exit 2 blocks a call and returns its stderr to the model
└── tests/                         # no network and no real CLI; the fake one in conftest plays each failure
    ├── conftest.py                # the fake claude CLI, one mode per session shape worth proving
    ├── test_harness_preflight.py  # version parsing, flag presence, what the auth summary may and may not say
    ├── test_harness_session.py    # argv construction and result parsing, pinned to a real CLI result object
    ├── test_harness_features.py   # validation, the transition table, selection order, tampering, the digest
    ├── test_harness_records.py    # session numbering across runs, and what a transcript must contain
    ├── test_harness_guard.py      # what the hook allows and blocks, including the two cases real runs found
    ├── test_harness_hook_e2e.py   # the hook as the CLI invokes it, including the pentest bypasses
    ├── test_harness_loop_e2e.py   # whole runs against the fake CLI: every stop condition, and the bypasses
    ├── test_harness_mark_feature_e2e.py   # the write path: the ordering rule, the digest, every refusal
    ├── test_harness_session_teardown.py   # process groups: an orphan cannot outlive its session
    └── test_harness_workspace_commit.py   # the harness commits its own files and never project code
```

Nothing here is installed as a package. The harness is designed to run against repositories other than this one, so it is reached by path.

## What it puts in your repository

```
your-project/
└── .agent-harness/
    ├── spec.md              # your idea, copied in. The run's source of truth.
    ├── feature_list.json    # the definition of done, one entry per verifiable feature
    ├── progress.md          # what each session did, appended to, never rewritten
    ├── init.sh              # how to build the project and run its suite, written by the initializer
    ├── serve.sh              # how to start a running instance, so a session can drive the real thing
    ├── .gitignore           # ignores local/ below
    └── local/               # gitignored
        ├── settings.generated.json
        └── logs/            # three files per session, see below
```

The first five are committed on purpose: they are how one session speaks to the next, and a session that cannot read them starts from nothing. `local/` is not, because it holds absolute paths from your machine and full session transcripts.

`init.sh` is the way in for a **build** and `serve.sh` is the way in for a **running instance**, which are not the same thing: the init script runs the suite and exits, and for a web app that leaves nothing listening for the next session to drive. Both are required, and a run refuses to start without them. A project with nothing to serve still gets the file, saying so and exiting zero, because a missing file is indistinguishable from one the initializer forgot.

Nothing else is copied into your repository. The guard hook and the mark-feature tool are wired by absolute path back to this directory, so upgrading the harness upgrades every repository it drives.

## Writing the spec

The spec is the one input that decides whether any of this works, because the initializer expands it into the feature list and nothing later gets to revisit it. Write it the way you would write a brief for a contractor who cannot phone you: what the thing is, who uses it, what it must do, what it must not do, what "finished" looks like. Vague specs produce vague features, and a vague feature is one no session can honestly mark as passing.

The harness copies it to `.agent-harness/spec.md` and neither it nor any session may edit it afterwards. Changing your mind means editing that file yourself and, usually, re-running `init --force`.

## The four commands

| Command | What it does |
| :-- | :-- |
| `doctor` | Checks the CLI version, the flags the harness depends on, git, and the repository. Spends nothing. |
| `init` | Runs the single initializer session that writes the feature list, the progress log, and `init.sh`. |
| `run` | Runs coding sessions, one feature each, until a stop condition fires. |
| `status` | Prints how many features pass, which one is next, and the tail of the progress log. Spends nothing. |

Useful options on `init` and `run`:

| Option | Default | Why you would change it |
| :-- | :-- | :-- |
| `--model` | `opus` | `--model haiku` for a cheap rehearsal of the whole loop on a throwaway repository. |
| `--effort` | `high` | Pinned rather than inherited, so two unattended runs of the same list behave the same on any machine. Lower it (`medium`, `low`) to trade quality for cost. |
| `--sessions` | `10` | How many shifts to run. The loop stops early on its own if it should. |
| `--session-budget-usd` | `10.0` | Hard per-session ceiling, enforced by the CLI itself rather than by the harness. |
| `--total-budget-usd` | none | Stop the whole run once this much has been spent. Set it for anything unattended. |
| `--session-timeout` | `3600` | Wall-clock seconds before a session is killed. |
| `--verification-notes` | generic guidance | A file describing how _this_ project is verified end to end. See below. |
| `--dry-run` | off | Print the exact command and the fully rendered prompt, run nothing, write nothing. |
| `--allow-dirty` | off | Start against a repository with uncommitted changes, accepting that a revert takes them. |
| `--permission-mode` | `bypassPermissions` | Unattended sessions cannot answer a prompt. See the sharp edges below. |

A default run is up to ten sessions of `opus` at `high` effort against a `$10` per-session ceiling, so up to `$100`, and high effort sits nearer that ceiling than an inherited default would. `--total-budget-usd` is unset by default and is the only thing that bounds a whole run; set it for anything unattended. On a subscription rather than per-token billing, the ceiling that actually bites is the usage window, not the dollar figure.

## How a feature gets marked passing

A session cannot edit the feature list, and it cannot record a feature as passing without the harness having already watched the same, unchanged test **fail**. That is two commands, in this order, and the order is the mechanism:

```bash
# 1. having written the test, and nothing else:
<mark command> <feature-id> test-failing \
    --test "<command that runs the test>" \
    --test-path <the file the test is in>

# 2. having written the implementation:
<mark command> <feature-id> passing --test "<the same command>"
```

The first **requires the check to fail**. A check that passes before the feature exists is not testing the feature, and saying so is the refusal. The second requires the same command, the same test files byte-for-byte, and a passing run: all three, checked before anything is written. What gets stored is the command, the files, and their hash, so the claim is re-checkable by anyone later without reading a word of prose:

```json
{
  "id": "top-words-ordering",
  "state": "passing",
  "evidence": {
    "kind": "command",
    "detail": "pytest tests/test_wordcount.py::test_ordering",
    "test_paths": ["tests/test_wordcount.py"],
    "digest": "sha256:1f3a…",
    "observed_at": "2026-08-18T14:02:11+00:00"
  }
}
```

The digest exists to stop one specific dodge: write `assert False`, record the red, rewrite the test into something real, record the green. Its limit, stated honestly: a session could name a decoy path and leave the real test undigested. That is deliberate falsification, it is recorded in the evidence, and it is visible at review. The digest raises the cost; it does not make it impossible.

### The four states

A feature is in one of four states, and the harness will only move it along an edge it has watched happen:

| State          | Meaning                                                     |
| :------------- | :---------------------------------------------------------- |
| `no-test`      | Nothing written. Every feature starts here.                 |
| `test-failing` | The test exists and the harness watched it fail.            |
| `passing`      | The same unchanged test then passed.                        |
| `broken`       | It was passing; the recorded check now fails. A regression. |

`broken` is information the old boolean threw away, since a regression and a never-started feature used to be indistinguishable. A `broken` feature is picked ahead of everything else next session, whatever its priority, because building on a known regression produces two bugs and a session that looks productive.

Dropping a feature back to `no-test` clears its evidence and needs no proof: no session has an incentive to falsely claim less than it achieved. Only movement toward `passing` needs evidence.

### The manual exception

Some checks genuinely cannot be automated. Those are the one route to `passing` that skips the recorded failure, because there is no test to watch fail, and they have to justify themselves:

```bash
<mark command> <feature-id> passing --test "manual: the check is a colour judgement"
```

A reason is required, and manual verifications are counted separately, so a project where everything was verified by hand looks exactly as suspicious as it is. `status` prints the breakdown:

```
features: 6/33 passing, 27 to go
states: 6 passing, 2 with a test written and failing, 24 not started, 1 broken
evidence: 5 backed by a re-runnable command, 1 verified by hand, 0 passing with nothing recorded

BROKEN: 1 feature(s) that used to pass no longer do. The next session will work these before anything else.
  chat-send-message (priority 2) — A typed message receives a response
```

This is also why the initializer is told to stand up a test suite before anything else and to make `init.sh` run it: the first act of every later session is running that suite, which turns "what did I inherit" into a fact rather than an assumption.

### One feature, one test file

Give each feature its own test file where you reasonably can. The digest is over whole files, so a file holding two features' tests changes its hash whenever either one is written, and `status` will then report the older claim as unverifiable. That is a report rather than a halt, deliberately: the loop only re-hashes the features a session actually moved, because otherwise one honest addition to a shared file would stop the run and keep stopping it. What it costs is that a test quietly weakened in a shared file is something a human notices in `status`, not something the loop refuses.

## Telling it how to verify

The article's biggest single finding was that Claude marks features done without checking them end to end, and that giving it browser automation and demanding user-level testing changed the results dramatically. Browser automation is specific to web apps, so what the harness carries by default is the demand rather than the tool: start the thing with `serve.sh` and drive it by the route a user takes. The recorded test **is** evidence: it is what tells the next session, thirty sessions from now, that the feature still works. What it cannot do alone is tell you the feature works _now_, because it exercises the units you wrote against the assumptions you had while writing them, which is exactly the pair that fails together. So the prompts demand both.

If your project has a better answer, write it in a file and pass `--verification-notes`. It replaces that section of both prompts. Good things to put in it: the exact command that runs the app, which MCP server is available for driving a browser, the fixture data to test against, what a correct result looks like.

## What stops a run

Every stop is announced, with the reason, on the last line of output.

| Stop | What it means |
| :-- | :-- |
| every feature passes | The run is done. |
| the session limit | `--sessions` reached. Run it again to continue. |
| the run budget is spent | `--total-budget-usd` reached. |
| a session errored | Including hitting its own `--session-budget-usd`. Read its log. |
| two sessions in a row moved nothing | No commit, no feature passed. The loop was spending money without progress. |
| the feature list changed structurally | Something rewrote the definition of done. The run stops immediately and says what changed. |

## Reading a run afterwards

`.agent-harness/local/logs/` holds three files per session, sharing one name:

| File | What it is |
| :-- | :-- |
| `.md` | The transcript. Every thought, tool call, tool result and reply in order, with the exact command that produced the session at the top. Start here. |
| `.jsonl` | The raw event stream, verbatim and unparsed. The forensic copy, for when the transcript's rendering is not enough. |
| `.json` | The structured summary: model, effort, thinking tokens, turns, cost, duration, any denied tool calls, and the closing message. |

Sessions are numbered continuously per repository — `session-001`, `session-002`, and so on — carrying on from whatever the log directory already holds rather than restarting at one on each `run`. Gaps are stepped over rather than filled, since a gap means an interrupted run whose record is worth keeping.

All three are written before the result is parsed, because the sessions worth reading are the ones that ended badly.

Every session is started with an id the harness chose, so you can reopen one directly:

```bash
claude --resume <session-id>
```

`progress.md` is the human-readable version, and it is usually the file to read first.

## The escape hatch

Sessions cannot edit `feature_list.json` or `spec.md`. A hook blocks it, and the loop re-checks after every session in case the hook was talked around. You can edit both freely: the hook only ever sees the agent's tool calls. When the spec really did change, or a feature turns out to be wrong or impossible, that is the intended way to fix it.

## Running it overnight

```bash
nohup uv run --script $HARNESS run --repo ~/code/my-project \
  --sessions 40 --total-budget-usd 120 --session-timeout 2700 \
  > ~/harness-overnight.log 2>&1 &
```

Everything the run needs to survive being interrupted is committed as it goes, so `Ctrl-C`, a killed process, or a closed laptop costs at most the session in flight. Starting again picks up from the feature list.

## Sharp edges, stated plainly

- **Sessions run with `bypassPermissions`.** An unattended session cannot answer a permission prompt, and a denied tool call makes the agent work around the restriction rather than stop. The protection is that the target repository must be a clean git work tree, so everything a session does is revertable. Do not point this at a repository whose uncommitted state you care about, and do not point it at anything holding credentials you would not hand to a model.
- **The Bash guard is a heuristic.** It reads command strings segment by segment. It catches drift, not intent; a determined command can get past it. The loop's after-the-fact comparison of the feature list is the real backstop.
- **One session at a time.** No parallelism, by choice. Several sessions in one repository would fight over the working tree, and worktree isolation is a bigger feature than it looks.
- **The model still writes the test it is judged by.** The ordering rule closes most of what that used to cost: the session can no longer write the test _after_ seeing the implementation work, and a check that passes before the feature exists is refused where it is offered. What it cannot do is judge whether the test is any good, and a weak test written first is still a weak test. Skim the tests, not just `progress.md`.

## Working on the harness itself

```bash
uv run pytest scripts/agent-harness/tests   # unit tests, no CLI or network needed
uv run ruff check scripts/agent-harness
uv run ruff format scripts/agent-harness
uv run basedpyright scripts/agent-harness
```

Every CI gate covers this directory. `ruff` and Prettier run repo-wide, basedpyright's `include` in `pyproject.toml` lists `scripts`, so the harness is type checked in `strict` alongside the rest of the project, and the tests have their own job because they are not on pytest's `testpaths`.

The tests cover the pure logic and, through a fake `claude` CLI in `conftest.py`, whole runs: every stop condition, the write path's refusals, the hook as the real CLI invokes it, and each bypass a session might reach for. What they do not cover is a real model. The fake plays the shapes it was told to play, so it can prove the harness reacts correctly and can prove nothing about what a session will actually do. That is what a rehearsal against a throwaway repository with `--model haiku` is for, and it is how both of the guard bugs in the git history were found.

Known gap: `basedpyright` reports several hundred errors in `tests/`, all of them untyped pytest fixtures and none in `agent_harness/` or `tools/`. The CI job is red on that today. Fixing it is either annotating the fixtures or narrowing basedpyright's `include`, and which of those is right is a project-wide call rather than a harness one.
