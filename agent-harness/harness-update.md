# Harness update — implementation plan

> **Superseded, 2026-08-18.** Parts B and A landed and are described in `DESIGN.md`. **Part C — the audit verb — was built and then removed the same day** on build-versus-buy grounds: `/code-review` and `/security-review` ship with Claude Code and do that job better. See the "audit phase, built and then removed" section of `DESIGN.md` for the reasoning. This file is kept as the record of what was planned and why, not as work to do. Nothing in part C should be built from it without a reason that names something the built-in reviews cannot do.

A work order for a Claude Code agent. Three changes to `scripts/agent-harness/`, decided 2026-08-18, none of them written yet. Land them in the order **B → A → C**: B is a small independent warm-up, A is the foundation, and C is built on top of A.

**You are editing the harness with ordinary tools. Do not run the harness against a repository to test it** — every session it starts burns the usage window this work needs. Everything here is provable with `uv run pytest` against the fake `claude` CLI already in `tests/conftest.py`.

| Part | What it is |
| :-- | :-- |
| [1. Ground rules](#1-ground-rules) | Read before touching anything |
| [2. Why](#2-why) | The problem these changes solve |
| [3. The target design](#3-the-target-design) | States, transitions, evidence, CLI shape. Read this twice. |
| [4. Change A, slice by slice](#4-change-a-the-four-state-model) | The main work, five slices |
| [5. Change B](#5-change-b-opus-on-high-effort) | A small independent change. Land it first. |
| [6. Change C](#6-change-c-audit-a-fifth-verb) | New harness verb: one session that attacks and inspects what was built, writes a ranked findings list, and changes nothing |
| [7. The manual pass on this change](#7-the-manual-pass-on-this-change) | The same chain, run by hand against the harness itself |
| [8. Definition of done](#8-definition-of-done) | The checklist |
| [Reference](#reference--the-harness-as-it-stands) | Wiring diagrams of the harness as it is today, §R1–R8 |

Where this plan says "Reference §R6" it means the guard-layers section of the reference at the bottom, not part 6 above.

---

## 1. Ground rules

**Environment.** `uv` only. Never conda, never bare `python` or `pip`. The commands you need:

| Task            | Command                         |
| :-------------- | :------------------------------ |
| Tests           | `uv run pytest`                 |
| One test file   | `uv run pytest tests/test_x.py` |
| Lint            | `uv run ruff check`             |
| Format Python   | `uv run ruff format`            |
| Format markdown | `npx prettier --write <file>`   |

Run from the repository root, not from `scripts/agent-harness/`.

**Commits.** `CLAUDE.md` §1: never run `git commit` without the user approving that specific commit and its message first. Propose the message, wait, then commit. Each slice below suggests one. Messages explain **why**, not what.

**Docs.** `CLAUDE.md` §3: every doc edit needs the user's approval before it lands, including `README.md` and `DESIGN.md`. Slices A5 and C8 touch both — propose the wording, do not land it silently.

**Baseline.** Before you start: `uv run pytest` must be green (187 tests) and `git status` clean. If either is not, stop and say so.

**Proving a change is real.** This repo has a convention and you are held to it. For any behaviour you claim to have fixed or added, show the test failing without your change:

```
git stash            # remove the source change, keep the test
uv run pytest tests/test_the_new_thing.py     # MUST fail
git stash pop
uv run pytest tests/test_the_new_thing.py     # MUST pass
```

A test that passes both ways is testing nothing. Report the pre-fix failure output for each new test — that is the evidence, not your assurance.

**Scope.** Stay inside `scripts/agent-harness/`. One known-unrelated failure exists elsewhere in the repo (`docs/project-decisions.md` fails `ruff format --check`); leave it alone.

### How to work this plan

This is more work than one context window holds. **Delegate slice by slice to sub-agents and stay the integrator yourself.**

- One sub-agent per slice. Give it the slice, §3 (the target design), and whichever Reference sections it needs — not the whole document.
- Ask it back for a summary and the pre-fix failure output, **not** file dumps. Your context is for review and integration; theirs is for reading code.
- **Run the full suite yourself between slices, not only at the end.** Sub-agents cannot see each other's work, so two of them will independently add the same helper or diverge on a name. Catching that at the seam is cheap; catching it after six slices is not.
- Keep a running note of what has landed, in this file or beside it, so a fresh session can pick the work up mid-flight. A usage cutoff should cost one slice, not the plan.

**On being interrupted.** Sessions do get cut off mid-run — the previous hardening chain here lost two agents to transient API errors. The harness already handles this well for its own runs: the workspace is committed after every session and before every early stop, so at most one session's work is lost. Change A improves it further, and this is worth knowing: a feature left in `test-failing` records that its test exists and was seen to fail, so an interrupted session resumes with only the implementation left to write. Under the old boolean that fact was lost entirely.

One rough edge falls out of that, and slice A3's error messages should cover it: if a session dies after recording `test-failing` but before committing its test file, the recorded digest refers to content that only exists in the working tree. If that tree is then cleaned, the green transition fails on a digest mismatch. The message must say the test file changed or was lost since it was recorded, and that the way out is to re-record `test-failing` — not just "digest mismatch".

---

## 2. Why

A session today writes the code, then writes the `--test` command that "proves" it, then runs `mark_feature.py`. The harness runs that command and requires it to pass. That closes the gap between _claiming_ and _checking_ — but not the gap between checking and checking something worthwhile. `--test "test -f README.md"` passes and proves nothing. `DESIGN.md` §11 concedes this; the 2026-08-18 pentest logged it as finding A.

**Change A** makes the harness observe the ordering instead of asking for it. A feature cannot reach `passing` unless the harness has already watched its test **fail**, before the implementation existed, and then watched the same unchanged test **pass**. The transition is something the harness saw happen rather than something a session asserted.

Test _quality_ is deliberately not automated. A human reviews the tests at PR time. This change only enforces that the test came first.

**Change B** makes unattended runs reproducible by pinning reasoning effort instead of inheriting whatever an interactive session was configured with.

---

## 3. The target design

### 3.1 Four states

`passes: bool` on a feature is replaced by `state`, one of four values. This is the change that makes everything else fall out cleanly: the old boolean could not express "the test exists and fails," which is the state the whole mechanism turns on.

| State | Meaning | Evidence held |
| :-- | :-- | :-- |
| `no-test` | Nothing written. The initializer writes every feature like this. | none |
| `test-failing` | The test exists and the harness watched it fail. | command, test paths, digest |
| `passing` | The test exists and the harness watched it pass. | command, test paths, digest |
| `broken` | It was `passing`; the recorded check now fails. A regression. | command, test paths, digest |

`broken` is new information the old boolean threw away. Today a regression and a never-started feature are both `passes: false` and indistinguishable, even though `coding.md` step 2 explicitly asks each session to find regressions. Now a run can report them.

### 3.2 The transition table

Naming the states is not enough on its own. If the CLI is "set the state to X," a session jumps straight to `passing` and the ordering is never enforced. The table below is what actually enforces it, and it is the heart of this change.

```
                    +-------------+
                    |   no-test   |<---------------------+
                    +-------------+                      |
                          |                              |
             check MUST FAIL                             |
                          |                              |
                          v                              |
                  +---------------+                      |
        +-------->| test-failing  |<------+          reset from
        |         +---------------+       |          ANY state,
    check MUST          |                 |          no check,
      FAIL       check MUST PASS       check         drops evidence
        |        + command match     MUST FAIL           |
        |        + digest match          |               |
        |               v                |               |
        |         +-----------+          |               |
        |         |  passing  |----------+               |
        |         +-----------+                          |
        |               ^                                |
        |          check MUST PASS                       |
        |          + command match                       |
        |          + digest match                        |
        |               |                                |
        |         +-----------+                          |
        +---------|  broken   |                          |
                  +-----------+--------------------------+
```

As a table, which is how it should be written in code:

| From | To | Requirement |
| :-- | :-- | :-- |
| `no-test` | `test-failing` | check runs and **fails**; record command + digest |
| `test-failing` | `test-failing` | check runs and **fails**; re-records a changed test |
| `test-failing` | `passing` | check runs and **passes**; command + digest match |
| `passing` | `broken` | recorded check runs and **fails** |
| `broken` | `passing` | recorded check runs and **passes**; digest matches |
| `broken` | `test-failing` | check runs and **fails**; re-records a changed test |
| _any_ | `no-test` | nothing. Drops all evidence. |

Everything not in that table is **forbidden**, and the three that matter are:

- `no-test` → `passing` — the entire point of the change
- `no-test` → `broken` — nothing has ever worked, so nothing can have broken
- `test-failing` → `broken` — it has never passed

> **The governing principle:** a transition that _reduces_ claimed progress needs no proof, because no session has an incentive to falsely claim less. Only movement toward `passing` needs evidence. That is why `→ no-test` is free and `→ passing` is the most constrained edge in the table.

> **Why `broken` → `passing` requires the digest to match:** fixing a regression should mean fixing the code, not rewriting the test until it goes green. If the test genuinely must change, the honest path is `broken` → `test-failing` (re-observe red with the new test) → `passing`.

### 3.3 The evidence record

`Verification` is replaced by `Evidence`, and the field on a feature is renamed `verified_by` → `evidence`. The rename is worth its cost because the record now exists in `test-failing` too, where "verified by" would be a lie.

```python
@dataclass(frozen=True)
class Evidence:
    kind: str  # "command" or "manual"
    detail: str  # the command, or the reason for manual
    test_paths: tuple[str, ...] = ()  # what was digested
    digest: str = ""  # sha256 over those files' contents
    observed_at: str = ""  # ISO 8601, when the check last ran
```

`kind` and `detail` keep their current meanings, so `summarize`'s automated-versus-manual counting carries over unchanged.

**The digest** is `sha256` over the sorted `(relative path, file bytes)` pairs of every `--test-path`. It exists to stop one specific dodge: write `assert False`, observe red, rewrite the test into something real, observe green. Requiring the test files to be byte-identical between red and green forces the final test to exist before the implementation, which is the whole objective.

Its limit, stated honestly: a session could name a decoy path and leave the real test file undigested. That is a deliberate act of falsification, recorded in the evidence, and visible to whoever reviews the PR. The digest raises the cost of the dodge; it does not make it impossible.

**The manual escape hatch stays.** `--test "manual: <reason>"` is the one legal `no-test` → `passing` edge, for checks no command can make. It records `kind: "manual"`, holds no digest, and is counted separately by `status` so a project where everything is manual looks exactly like one where nothing was verified. Use of it should stay rare.

### 3.4 The CLI shape

The four-state model removes the naming conflict entirely, so the command keeps its current simple shape. The positional argument is renamed `status` → `state`, because now it honestly is one:

```
  mark_feature.py <feature-id> <state> [--test ...] [--test-path ...]
                                  |
                                  +-- no-test        (no other arguments)
                                  +-- test-failing   --test REQUIRED
                                  |                  --test-path REQUIRED, repeatable
                                  +-- passing        --test REQUIRED
                                  +-- broken         (no other arguments — the
                                                      recorded check is re-run)
```

A session's normal shift is two calls:

```
  # 1. having written the test, and nothing else:
  mark_feature.py chat-new-conversation test-failing \
      --test "uv run pytest tests/test_chat.py::test_new_conversation" \
      --test-path tests/test_chat.py

  # 2. having written the implementation:
  mark_feature.py chat-new-conversation passing \
      --test "uv run pytest tests/test_chat.py::test_new_conversation"
```

> An earlier draft of this plan proposed argparse subcommands. That was a workaround for `red` not fitting in a `pass`/`fail` slot. The state enum removes the conflict, so the workaround is dropped — keep the flat shape.

### 3.5 The file on disk

```json
{
  "version": 2,
  "features": [
    {
      "id": "chat-new-conversation",
      "category": "functional",
      "priority": 1,
      "description": "New chat button creates a fresh conversation",
      "steps": ["...", "..."],
      "state": "no-test",
      "evidence": {
        "kind": "command",
        "detail": "uv run pytest tests/test_chat.py::test_new_conversation",
        "test_paths": ["tests/test_chat.py"],
        "digest": "sha256:1f3a...",
        "observed_at": "2026-08-18T14:02:11+00:00"
      }
    }
  ]
}
```

`evidence` is absent while the state is `no-test`.

### 3.6 Migration: a hard break, on purpose

`"version"` already exists in the document and is currently ignored. Bump it to `2` and **enforce it**. A version-1 list, or any feature carrying `passes`, must raise a `HarnessError` that names the problem and says the repository has to be re-initialised.

Do not silently coerce old lists. A quiet coercion turns "this list predates the ordering rule" into "these features were verified under rules that did not exist," which is precisely the class of false confidence this whole change exists to remove. No repositories are currently mid-run, so the break costs nothing today and a coercion path would cost forever.

---

## 4. Change A: the four-state model

Five slices, in order. Each is independently revertable and leaves the suite green.

### Slice A1 — the state enum and the schema

**File:** `agent_harness/features.py`

1. Add `FeatureState(StrEnum)` with `NO_TEST = "no-test"`, `TEST_FAILING = "test-failing"`, `PASSING = "passing"`, `BROKEN = "broken"`. A `StrEnum` rather than bare constants because it serialises to its own value through `json.dumps` for free, and a typo becomes an `AttributeError` at import instead of a string that silently matches nothing.
2. Replace `Verification` with `Evidence` per §3.3. Rename `_read_verification` → `_read_evidence` and extend it to read the three new fields.
3. `Feature`: `passes: bool` → `state: FeatureState`; `verified_by` → `evidence: Evidence | None`.
4. `REQUIRED_FEATURE_FIELDS`: `"passes"` → `"state"`. `MUTABLE_FEATURE_FIELDS`: `("state", "evidence")`.
5. `parse_feature_list`: validate `state` is one of the four, naming the feature and the legal values on failure. Add the version-2 check and the legacy-format error from §3.6.
6. `FeatureSummary`: keep `total`, `passing`, `failing`, `automated`, `manual` so existing callers survive, and add `no_test`, `test_failing`, `broken`. `failing` becomes "not `passing`". `is_complete` becomes "every feature is `passing`".
7. `next_feature`: **a deliberate behaviour change, decided and approved — implement it.** Pick `broken` features before anything else, then by priority as today. Fixing what is broken comes before starting anything new: building on a known regression is the exact failure `coding.md` step 2 warns about, and until now the loop had no way to act on it even when a session found one. Give it its own test, and make the reason a comment rather than leaving a future reader to wonder why priority is not the only key.
8. Add `digest_paths(paths, repo_root) -> str` per §3.3.

**Tests** — extend `tests/test_harness_features.py`:

- each state parses; an unknown state is rejected naming the legal values
- a version-1 document raises, and the message says to re-initialise
- a feature carrying `passes` raises
- `is_complete` is false when anything is `broken`
- `next_feature` prefers `broken` over a lower-priority `no-test`
- `digest_paths` is stable across ordering and changes when a byte changes

> **Where quality will quietly leak in this slice.** There are 57 references to `passes` across the suite, and translating them is mechanical enough to delegate — which is exactly why it is dangerous. The failure mode is not a broken test; a broken test is loud. It is a test whose assertion got _weaker_ during translation and still passes, so the count goes up and the coverage goes down. Review this diff yourself, hunk by hunk, asking of each one whether it asserts as much as it did before. Do not accept "187 → 195, all green" as evidence that this slice went well.

**Commit:** `feat: replacing the passing boolean with four feature states so the harness can tell a regression from unstarted work, and a written test from none`

### Slice A2 — the transition table

**File:** `agent_harness/features.py`

1. Add the table from §3.2 as data — a dict keyed by `(FeatureState, FeatureState)` whose value is the requirement (`CHECK_MUST_FAIL`, `CHECK_MUST_PASS`, `NO_CHECK`). Data, not a chain of `if`s, so the legal set is readable in one screen and testable cell by cell.
2. Add `transition_requirement(current, target)` returning the requirement or `None` when forbidden.
3. Rewrite the flip rule in `find_structural_changes`. It currently special-cases "flipped to passing with no `verified_by`". Replace it with two checks, which are simpler than what they replace:
   - the state changed by an edge not in the table → structural change
   - the state is `test-failing`, `passing`, or `broken` but `evidence` is absent → structural change

   This is layer 3 from Reference §R6, so it is what makes the table enforceable rather than advisory. A hand-edited jump from `no-test` to `passing` is caught after the session even if the hook never saw it.

**Tests** — extend `tests/test_harness_features.py`:

- every legal edge in §3.2 is allowed, one test per row
- every forbidden edge is rejected, explicitly including `no-test` → `passing`
- a state change with missing evidence is rejected
- the existing tamper tests (add, remove, immutable-field change) still pass untouched

**Commit:** `feat: making the legal state transitions a table the tamper check reads, so a session cannot reach passing without the harness having watched its test fail first`

### Slice A3 — `mark_feature.py`

**File:** `tools/mark_feature.py`

1. Positional `status` → `state`, `choices` = the four values. Help text names what each one means.
2. Add `--test-path`, repeatable. Required for `test-failing`; rejected for the others.
3. `--test` required for `test-failing` and `passing`; rejected for `no-test` and `broken`.
4. `run_check` gains an expected-outcome parameter. When a failure is expected, a zero exit is the error — and its message must explain _why_ a passing test is a refusal, because that message is what the model reads and acts on. Something to the effect of: this check passes before the feature exists, so it is not testing the feature.
5. New flow, preserving the existing ordering property that **the feature id is validated before any check runs**:

```
  read document -> find feature -> current state
        |
        +-- transition_requirement(current, target)
        |     None -> error naming the legal targets from here
        |
        +-- validate the arguments for the target state
        |
        +-- target is `passing`?
        |     command must equal the recorded evidence.detail
        |     digest of the recorded test_paths must equal evidence.digest
        |     (both checked BEFORE the check runs — they are cheap and
        |      they are the ones whose failure the model must act on)
        |
        +-- run the check, requiring pass or fail per the table
        |
        +-- write the new state and evidence
```

6. The manual hatch: `--test "manual: <reason>"` with target `passing` from `no-test` is the one bypass. Keep the existing empty-reason rejection and the no-op-command rejection (`true`, `:`, `exit 0`, bare `echo`) exactly as they are — they are still the first thing a session reaches for.

**Tests** — extend `tests/test_harness_mark_feature_e2e.py`, real subprocess invocations through the existing `run_mark` helper:

- `test-failing` is refused when the check passes, and the message says why
- `test-failing` records command, paths and digest
- `passing` is refused from `no-test`
- `passing` is refused when the recorded command differs
- `passing` is refused when a test file changed since `test-failing`
- `passing` succeeds when the check passes and both match
- `broken` re-runs the recorded check and is refused if it still passes
- `no-test` from any state drops the evidence
- a typo'd id fails before any check runs (the existing property, keep its test)

**Commit:** `feat: requiring a session to record its test failing before it can record it passing, so the evidence a feature works is a transition the harness watched rather than a claim it was handed`

### Slice A4 — the prompts

The mechanism is worthless if the session is still told to do it in the old order.

**`prompts/coding.md`** — the real work here is **reordering**, not rewording:

- Step 2: broken features are now marked `broken`, not `fail`.
- Step 4 currently says "Where you can, watch the new test fail before your change and pass after it." That soft suggestion is now the hard mechanic and it has to move: **write the test, record it failing, then implement.** The test-writing instruction has to precede the implementation instruction.
- Step 5 becomes the two-call sequence from §3.4, with both commands spelled out.
- Keep the existing warning that a truthful failure beats the appearance of progress.

**`prompts/system_contract.md`** — rule 2 demands a test be left behind. It must now demand the test come **first**, and say plainly that the harness records it failing and will refuse a feature that never had a failing test.

**`prompts/initializer.md`** — the JSON example and the field rules:

- `"version": 1` → `2`; `"passes": false` → `"state": "no-test"`
- the `passes` bullet becomes a `state` bullet: `"no-test"` for every feature, all of them
- the closing line "`verified_by` is written later, by the command that records a feature as passing, and it is the only other field the file ever grows" → `evidence`, written by `mark_feature.py`
- §2 asks for a test suite. Add one line: it must be runnable **one file at a time**, since every check should name the narrowest command that proves its feature.

**Tests:** `prompts.render` fails on unreplaced `{{TOKEN}}` markers, so run the existing prompt tests. If a token is added or removed, the render tests must cover it.

**Commit:** `docs: reordering the coding prompt so the test is written before the implementation, matching the ordering the harness now enforces`

### Slice A5 — the loop, the status line, and the docs

**`agent_harness/loop.py`**

- `DEFAULT_VERIFICATION_GUIDANCE` ends: "Unit tests are worth writing and are not evidence for this purpose." That now contradicts the mechanic, where the red→green test **is** the recorded evidence. Reword so the demand for end-to-end verification survives without calling the recorded test worthless.
- Check the arithmetic still holds: `summary_after.passing - summary_before.passing` and `summary_before.is_complete` work unchanged if `FeatureSummary` kept those names in A1. Confirm rather than assume.
- A session that moves a feature to `test-failing` **counts as progress** for the stall detector. It is real work, and although the current rule (more passing, or HEAD moved) would usually catch it via the commit, a session that writes a test and stops before committing must not read as stalled. Add the state change to the progress test rather than relying on the commit.

**`agent_harness/cli.py`** — `_run_status` prints an evidence breakdown. Add the four-state counts, and surface `broken` prominently: a repository with a regression is the one case where a human should look immediately.

**`README.md`** and **`DESIGN.md`** — doc edits, so propose and wait:

- README line ~187 carries the same "unit tests are not evidence" sentence as `loop.py`. Same treatment.
- README documents the feature-list schema and `mark_feature.py` usage; both change.
- `DESIGN.md` §11 concedes the gap this change closes. It should now say what was done, what remains open (test quality is unjudged, by choice), and that the breaker/builder proposal at the bottom is superseded as the near-term direction.

**Commit:** `docs: recording that the checking gap is closed by ordering rather than by an adversarial second agent, and what is deliberately left unjudged`

---

## 5. Change B: opus on high effort

Independent of Change A and much smaller. **Land it first** — it is a clean warm-up commit and it does not interact with anything above.

### The change

```
  agent_harness/config.py

    DEFAULT_MODEL  = "opus"     <- already this, unchanged
    DEFAULT_EFFORT = None       ->  "high"
```

### Why it is not a one-line change

`DEFAULT_EFFORT = None` is deliberate, not an oversight. `session.py` omits `--effort` from the argv entirely when it is `None`, so the CLI's own configured level stands, and the comment in `config.py` argues for exactly that: naming a level "would silently override whatever the user configured for themselves."

Setting `"high"` reverses that position, so **rewrite the reasoning rather than deleting it**. The replacement argument: an unattended overnight run should not vary with whatever level an interactive session happened to be set to, because that makes two runs of the same feature list behave differently for reasons invisible from the outside. Reproducibility beats deference for a tool that runs while nobody is watching, and `--effort` remains for anyone who wants otherwise.

### Everything that moves with it

| File | What |
| :-- | :-- |
| `agent_harness/config.py` | the constant, and the comment above it, which currently argues the opposite case |
| `agent_harness/cli.py` | `--effort` help says "Omitted by default, which leaves the CLI's own configured level in force rather than overriding it." Straightforwardly false afterwards. |
| `tests/test_harness_session.py:80` | `test_omits_effort_entirely_when_none_was_asked_for` builds a request with no explicit effort and asserts `--effort` is absent. It inherits the default, so it goes red. |
| `DESIGN.md:109` | states "unset means the flag is not sent at all so the user's configured level stands" as a design position. Doc edit — propose it. |
| `README.md` (~line 137) | the options table has `--model` but no `--effort` row at all. Add one showing `high`. |
| `agent_harness/loop.py` | `describe_session_model` prints `effort {settings.effort or 'cli default'}`. The fallback stops being reachable via the CLI. Harmless — leave it for direct `RunSettings` use. |

On the test: the behaviour it pins is still worth pinning. Keep it, constructing `effort=None` explicitly, and add a sibling asserting the default now emits `--effort high`.

### The cost, stated plainly

A default run becomes ten sessions of opus at high effort against a $10 per-session ceiling — up to $100, with high effort pushing sessions nearer that ceiling than they currently sit, plus one more session for `audit` once Change C lands. `--total-budget-usd` is unset by default and is the only thing that bounds a whole run. This repo's owner runs on a subscription rather than per-token billing, so the ceiling that actually bites here is the usage window, not the dollar figure — but the README is written for anyone, so say the cost plainly there in the same breath as the new default.

**Commit:** `feat: pinning reasoning effort so two unattended runs of the same feature list behave the same regardless of local CLI configuration`

---

## 6. Change C: `audit`, a fifth verb

A new harness capability. Today `run` stops the moment every feature is `passing`, and that is the worst possible moment to hand a project to a human: it has never been attacked, nobody has looked for what the feature list failed to ask for, and the only reviewer is about to start from scratch.

`harness.py audit` adds a stage after the build, the way `init` is a stage before it. **One session, and it changes no code at all.** It attacks and inspects what was built, and writes a ranked list of findings — for a later session to work, and for a human to review from.

Land it **after** Change A is done and proven. It depends on the four-state model for the follow-on work, not for the audit itself.

### 6.1 It reports; it does not repair

This is the design decision everything else follows from. Earlier drafts had `audit` patch its own findings and then simplify the codebase automatically. It does neither. It produces a report and stops.

What that buys, and it is a lot:

- **Scope cannot run away.** An audit will legitimately surface things the spec never asked for — no rate limiting, no CSRF token, an index missing on a hot query. Those are worth knowing and worth writing down. They are not worth an autonomous session building them without anyone having agreed to. Reporting keeps the finding and drops the risk.
- **Far less permission machinery.** No settings regenerated between phases and no test files to protect from a simplify session. The guard still gains two rules for this verb — `findings.json` as a protected filename, and the network allowlist in §6.5 — but a session that writes only its own findings file needs nothing beyond those.
- **No reset machinery.** Nothing changed, so there is nothing to verify afterwards and nothing to roll back.
- **A junk finding costs ~30 seconds of human attention instead of a session.** That is what makes the audit's weak spot tolerable — see §6.6.
- **The human checkpoint is unconditional.** An earlier draft added `--stop-after attack` for this. The redesign makes it the only behaviour, which is strictly better than a flag someone has to remember.

> The phase is named `audit` rather than `harden` because it hardens nothing. It finds. Someone else fixes.

```
  harness.py init    ->  one session, writes the definition of done
  harness.py run     ->  N sessions, works a list down            (unchanged)
  harness.py audit   ->  ONE session, writes findings.json, changes nothing   NEW
  harness.py status  ->  where did it get to                      (+ findings)
  harness.py doctor  ->  is the environment sane                  (unchanged)
```

And the follow-on, which needs no new machinery at all:

```
  harness.py run --list findings.json    # work the findings, red/green,
                                         # same enforcement as features
```

That is the whole "a later session picks items up" story: findings use the feature-list schema, so `run` already knows how to work them. It happens because a human typed it, not because the audit decided to.

### 6.2 What it looks for

Wider than security. The prompt should ask for all of it, because one session reading the whole tree with fresh eyes is cheap and the human reviewing afterwards benefits from every category:

| Category | Looking for |
| :-- | :-- |
| `security` | Auth and authorisation holes, injection, secrets in the repo, missing validation on anything user-supplied |
| `correctness` | Crash inputs, boundary and malformed data, state left behind by failure paths, spec invariants that do not actually hold |
| `performance` | Work done per request that should be done once, N+1 queries, missing indexes, unbounded reads |
| `test-quality` | Tests asserting nothing meaningful, tests that would pass against a broken implementation, coverage gaps on the paths that matter |
| `duplication` | The same logic in several places, especially where the copies have already drifted |
| `complexity` | Code that is harder to change than the problem requires — the simplifications a human should consider |

The last three are what an automated simplify phase would have attempted. Reporting them instead of performing them is the same value delivered to a person who can judge it.

### 6.3 `findings.json`, ranked

Same schema as `feature_list.json`, same four states, same tamper rules. Reusing it is the trick that makes `run --list findings.json` free.

```json
{
  "version": 2,
  "kind": "findings",
  "features": [
    {
      "id": "auth-token-not-expired",
      "category": "security",
      "priority": 1,
      "description": "An expired session token is still accepted by /api/me",
      "steps": [
        "Log in and capture the session token",
        "Wait past the configured expiry",
        "curl -H 'Authorization: Bearer <token>' localhost:8000/api/me",
        "Observe 200 and the user's profile instead of 401"
      ],
      "state": "no-test"
    }
  ]
}
```

- **`priority` is the ranking**, and the audit's job is to set it honestly: severity first, not discovery order. `run` already works lowest-priority-first, so the ranking is what decides where a follow-on session starts and where a human's eye lands.
- **`steps` must be a reproduction that was actually performed**, not a description of a suspected problem. This is the single most important rule in the prompt. See §6.4.
- **`category` carries the kind**, from the table above. No schema change needed — the field already exists and is free-form.

`state` is `no-test` for every finding, exactly as the initializer writes features. A follow-on `run` then drives each one `no-test` → `test-failing` → `passing`, which means **the reproduction becomes a permanent regression test**. That is worth stating plainly: the audit's output is not a list of complaints, it is a list of tests that do not exist yet.

**Guard:** add `findings.json` to `PROTECTED_FILENAMES`. Additions are legal only for the audit session, which writes the file before it exists — the same way `init` writes the feature list. Every session after that may only move states through `mark_feature.py`.

**The human view is rendered, not written.** The audit session writes JSON only. `harness.py status` renders the ranked list for reading, and that keeps a second hand-maintained markdown file from drifting out of sync with the machine-readable one. One source of truth, two views.

### 6.4 `serve.sh`: the way in to a running instance

An audit session told to attack "through the real interface" has to answer, on its own: how do I start this thing? On what port? With what credentials? Is there seed data, or a test account? `init.sh` does not answer that — it brings the project up and runs the suite, which for a web app means the tests ran and exited, leaving nothing listening.

That gap matters more than it looks. When a session cannot reach the running system, the cheapest available action is to read the source and reason about what is probably wrong, which produces findings that sound right and were never reproduced. Compare what the two actually yield:

```
  reachable      curl -H 'Authorization: Bearer <expired>' localhost:8000/api/me
                 -> 200 with the profile body
                 a human checks this in ten seconds

  not reachable  "the expiry check at auth.py:44 appears not to run on
                  the /api/me path"
                 might be right; might be missing a middleware three files
                 away; ten minutes to find out, per finding
```

The second kind is worse than no audit at all, because a ranked findings list carries the authority of a test report. Whoever reads it reasonably assumes each item was demonstrated, and spends their review budget on guesses.

**So the initializer writes `serve.sh`, as a fifth required artifact.** Doing it at init is cheap — the session that just built the scaffolding knows exactly how the thing starts — and it pays off everywhere, not only in `audit`. Coding sessions verifying a feature end to end need the same thing, and the harness's whole verification story rests on exercising the running system rather than reading it.

- `config.py` gains `SERVE_SCRIPT_FILENAME = "serve.sh"`; `workspace.py` gains the path property; `missing_initializer_artifacts` gains it, so a run refuses to start without it.
- `initializer.md` gains it as a numbered deliverable beside `init.sh`, with the distinction stated plainly: **`init.sh` is the way in for a build, `serve.sh` is the way in for a running instance.** It should print how to reach what it started — the URL, the port, any test credentials — because the next session has no memory of this one.
- A project with nothing to serve (a library, a pure pipeline) still gets the file. It exits with a message saying so. Requiring the file to exist keeps the artifact check a one-liner; letting it be an honest no-op keeps it truthful.
- `coding.md` should mention it too. This is not an audit-only artifact.

> **Slice collision, flag it before delegating:** `initializer.md` is edited by both slice A4 and slice C1. Whichever runs second must read the other's change rather than rewriting the file from the plan.

`--verification-notes` stays what it is: the human's override when a project needs more explanation than `serve.sh` can carry.

**Teardown, and who owns it.** A script that starts a server raises the obvious question of who stops it. The answer must be the harness, not the session — a session told to clean up after itself will usually do it and will occasionally die before it gets there, and the failure that leaves behind is nasty: a process still holding the port, so the _next_ run's `serve.sh` fails to bind for reasons that look nothing like the real cause.

So the harness owns the lifecycle, on both sides:

```
  audit
    |
    +-- start serve.sh in its OWN PROCESS GROUP
    |     subprocess.Popen(..., start_new_session=True)
    |     record the pgid
    |
    +-- run the audit session, also in its own process group
    |
    +-- finally:                      <- a real `finally`, not the happy path
          SIGTERM the session group, grace period, SIGKILL
          SIGTERM the serve.sh group, grace period, SIGKILL
          print what was killed and whether it needed the SIGKILL
```

Three things about that worth stating:

- **The `finally` is the whole point.** Teardown has to run on success, on error, on timeout, and on `KeyboardInterrupt`. Every one of those is a normal way for a session to end here, and three of them are the ones that leak.
- **Killing the process group, not the process**, is what catches the children — the actual server behind a wrapper script, the database container, the worker.
- **This fixes a gap that already exists.** Today a coding session that starts a dev server leaks it; the harness kills nothing and never has. Putting the _session_ in its own group and reaping it applies to `run` as much as to `audit`, so build it once in `session.py` and both get it.

Never kill silently. Print what was terminated and whether it took a `SIGKILL`, because a server that ignores `SIGTERM` is a fact about the project worth knowing.

> POSIX-only as written. `start_new_session` and process groups do not exist on Windows, which needs `CREATE_NEW_PROCESS_GROUP` and a different signal. The harness targets macOS and Linux; say so rather than pretending the code is portable.

### 6.5 The audit attacks this machine and nothing else

"Penetration test" is a phrase that invites scope this harness must not have. An audit session that decides to probe a staging URL it found in a config file, or a third-party API the project integrates with, would be attacking systems nobody in this loop is authorised to attack. That must be prevented in code, not only asked for in a prompt.

**In `audit.md`, stated first and unmistakably:** the target is the local instance started by `serve.sh` and nothing else. No external hosts, no third-party services, no cloud consoles, no network target that is not this machine. If the project integrates with something remote, the integration is tested against a local stub or it is not tested. A finding that would require touching someone else's system is written up as a note for a human, never attempted.

**In the guard, enforced.** The hook already inspects every Bash command, so this is a new rule in the same place, generated into the audit session's settings:

```
  Bash segment contains a network client?
    curl wget nc netcat ncat telnet ssh scp sftp nmap
    dig host nslookup ping traceroute
    openssl s_client, httpie/http, ab siege hey wrk
    sqlmap nikto gobuster ffuf
        |
        +-- extract every host it targets
        |
        +-- host in the ALLOWLIST?          localhost
        |     yes -> allow                  127.0.0.1
        |     no  -> DENY                   ::1 / [::1]
        |                                   0.0.0.0
        |                                   *.localhost
        |
        +-- proxy flag present?   curl -x / --proxy, http_proxy=,
              yes -> DENY         --resolve, --connect-to
                                  (they route a "local" URL elsewhere)
```

An allowlist, not a denylist, and this is one of the rare places where that is straightforward: the set of acceptable hosts is small, closed, and known in advance. Contrast the file-write guard, where the space of bad shapes is open-ended and a denylist was forced (Reference §R6).

**Its ceiling, stated honestly.** This is a strong speed bump, not a sandbox:

- A session can open a socket from inside a language runtime — `python -c "import socket..."` — and parsing arbitrary program text for network calls is not feasible.
- A hostname that resolves externally cannot be checked without doing DNS at guard time, which is itself a network call and is racy.
- It stops what we are actually worried about: a capable model, told to find weaknesses, deciding on its own initiative that the staging environment is fair game. It does not stop a deliberate bypass.

Real containment would be OS-level network isolation for the session, which is out of scope here and worth writing into `DESIGN.md` as a known limit rather than leaving implied.

**And an audit trail:** every finding records the host its reproduction targeted. A reviewer can then confirm at a glance that the whole run stayed local, instead of taking it on trust.

### 6.6 What this still does not fix

**The audit writes its own definition of done.** Coding sessions can be trusted after Change A because the feature list was written by a different session, from a spec, and cannot be edited. The audit has no equivalent — "found nothing" and "did nothing" look identical from outside. It must therefore log **every attempt and its outcome**, held or not, and a run below `MIN_ATTACK_ATTEMPTS` should be reported as a failed audit rather than a clean bill of health.

That gate is a floor on effort, not on quality: eight shallow attempts clear it as easily as eight real ones. So the findings list is a set of leads, not a verdict, and neither the README nor `status` should imply otherwise.

**The report-only design is what makes that acceptable.** When the audit patched its own findings, a weak audit meant sessions spent building things nobody asked for. Now a weak audit means a short list a human skims and discards. Same unsolved problem, an order of magnitude less consequence.

### 6.7 New surface

**`config.py`**

```
FINDINGS_FILENAME     = "findings.json"
SERVE_SCRIPT_FILENAME = "serve.sh"
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})

# PROVISIONAL. Nothing has been measured yet; this is a placeholder chosen so
# the check exists, not a threshold anyone has evidence for. Revisit it once a
# few real audits have run and it is possible to say what a serious attempt
# count actually looks like. Say so in the comment so it does not quietly
# become load-bearing.
MIN_ATTACK_ATTEMPTS = 8
```

**`cli.py`** — the `audit` subparser, sharing `_add_session_arguments` with `init` and `run`. Preflight refuses unless every feature is `passing`, with `--force`; auditing a half-built project mostly rediscovers that it is half-built. `run` gains `--list <path>` so it can be pointed at `findings.json`, defaulting to the feature list.

**`guard.py`** — the network rule from §6.5, active only for the audit session's generated settings.

**`prompts/audit.md`** — the substance of this change, and it deserves the care `coding.md` got. It carries: the out-of-bounds boundary from §6.5 **first**, the categories from §6.2, the hard reproduction rule, the honest-ranking rule from §6.3, the attempt log, and one more thing said plainly — **you fix nothing.** A model that has just found a bug will want to fix it, and the value of this phase depends on it not doing that.

**`status`** — render the ranked findings beside the feature tally. A project that is feature-complete with three open security findings is not one anyone should start reviewing blind.

### 6.8 Slices

| Slice | Work |
| :-- | :-- |
| **C1** | `serve.sh` as a required initializer artifact: config, workspace, `missing_initializer_artifacts`, `initializer.md`, `coding.md`. Independent of everything else — it can land with Change A. |
| **C2** | `findings.json` as a second list: config, workspace paths, `PROTECTED_FILENAMES`, additions legal only before the file exists |
| **C3** | The network allowlist in `guard.py`, plus per-session settings carrying it. Hook-level tests for each denied client and each allowed host. |
| **C4** | `run --list <path>` — generalise the coding loop to work either list. Small, and it is the entire follow-on story. |
| **C5** | Process-group start and `finally` teardown in `session.py`, applied to `run` and `audit` alike (§6.4). Land it before the verb, so `audit` is never written without it. |
| **C6** | The `audit` verb: preflight, starting `serve.sh`, the single session, `MIN_ATTACK_ATTEMPTS`, the commit of `findings.json` |
| **C7** | `prompts/audit.md` |
| **C8** | `status` rendering the ranked findings; README and DESIGN, including the network-isolation limit from §6.5 and the POSIX-only note from §6.4 |

Exercise it end to end through the fake `claude` CLI. Cover: an audit that finds nothing but logs enough attempts, an audit below the attempt floor, a session trying to add to `findings.json` after it exists, `run --list findings.json` driving a finding through red/green, `audit` refusing on an incomplete feature list, and a run refusing to start when `serve.sh` is missing.

For the network rule specifically, test at the hook level rather than through a session: `curl localhost:8000` allowed, `curl https://example.com` denied, `curl -x proxy.example.com localhost:8000` denied, `nc example.com 443` denied, `curl 127.0.0.1:5000/api` allowed.

### 6.9 A patch phase, later, if the audit earns it

Not now, and deliberately not now. But the threshold is worth recording, because it is a real one rather than a matter of taste: **once a few real audits produce findings that turn out to be consistently accurate and consistently worth fixing**, a patch phase could run after the audit rather than a human triaging it.

It needs no new machinery — that is the point of `findings.json` sharing the feature-list schema. `run --list findings.json` already does the work, under the same red/green enforcement features get. The only change would be `audit` invoking it automatically instead of printing a suggestion.

What must be true first is not a code property, it is evidence: that the audit's ranking is honest, that its reproductions hold, and that it is not reporting missing features as defects. Until that is observed across several runs, the human triage step is the thing keeping scope from running away, and it stays.

---

## 7. The manual pass on this change

Change C gives the harness an `audit` verb for _projects it builds_. It does nothing for the harness itself, and `audit` only reports — so the fixing and simplifying still have to happen by hand here. Run this chain against this work, after parts 4–6 are landed and green.

Four sessions, in this order. **Each is a fresh session with no memory of the implementation** — a builder cannot red-team its own work, because it knows what it meant rather than what it wrote.

| # | Session | Job | Hard constraints |
| :-- | :-- | :-- | :-- |
| 1 | **Attack** | Break the new machinery. Reach `passing` without a genuine red. Forge or replay a digest. Point `--test-path` at a decoy, a symlink, a directory, something outside the repo. Get an addition into `findings.json` from a follow-on `run --list findings.json` session. Defeat the transition table by cycling through `no-test`. Re-attack the old surface too, since `features.py` changed under the guard's backstop. | Reproduce every finding or it is not a finding. Log attempts that held, with their count. Fix nothing. Leave the tree clean. Write the log to a **tracked file**, not a scratchpad. |
| 2 | **Patch** | Fix each confirmed exploit, each with a regression test proving the exploit worked before the fix and does not after. | Never weaken the guard or delete a test to close a finding. If something is architectural and not fixable in this pass, say so rather than papering over it. |
| 3 | **Simplify** | Remove accidental complexity across `scripts/agent-harness/`. Unlike the `audit` verb, this session **does** change code — a human is watching, which is the whole difference. | May not modify any test file. Suite stays green. The overlapping guard layers are deliberate — read Reference §R6 before deciding anything is redundant. |
| 4 | **Re-verify** | Full suite, `ruff check`, `ruff format --check`, `npx prettier --check`, **and re-run session 1's exploit reproductions.** Fix or revert whatever the simplification broke. | If the simplification cannot be repaired quickly, revert it. It was supposed to be behaviour-preserving; a broken one has no value to salvage. |

> Session 1's log is the reason this is worth doing: it is the only artifact that says what was attacked and what held. Last time this chain ran, that log was written to a session scratchpad and nearly lost. Write it to `scripts/agent-harness/pentest-<date>.md`, next to the existing one.

**Run each phase as its own top-level session, not as sub-agents of one orchestrator.** The chain is long enough that a single context will not hold it, and the previous run of this chain lost two agents mid-flight to transient API errors. Separate sessions with the findings file as the handoff survive that; a nested orchestration does not.

---

## 8. Definition of done

**Change A and B**

- [ ] `uv run pytest` green, and the count has grown by at least the tests listed in A1–A3
- [ ] `uv run ruff check` and `uv run ruff format --check` clean **within `scripts/agent-harness/`**
- [ ] `npx prettier --check` clean on every markdown file touched
- [ ] For each new behaviour, the stash/pop pre-fix failure is reported, not just claimed
- [ ] `next_feature` prefers `broken` over everything else, with its own test and a comment saying why (decided — implement it, do not re-open it)

**Change C**

- [ ] `audit` changes **no file** other than `findings.json` — proven by a test asserting the rest of the tree is byte-identical afterwards
- [ ] Exercised end to end through the fake CLI: an audit that finds nothing but logs enough attempts, an audit below the attempt floor, a session trying to add to `findings.json` after it exists, `audit` refusing on an incomplete feature list
- [ ] `run --list findings.json` drives a finding through `no-test` → `test-failing` → `passing` with the same enforcement features get
- [ ] `harness.py run` is unchanged: same stop reasons, same behaviour, plus the hand-off line pointing at `audit`
- [ ] `status` renders findings in priority order, and says plainly that they are leads rather than a verdict
- [ ] `audit.md` states the out-of-bounds boundary (local instance only) and the you-fix-nothing rule
- [ ] The network allowlist is proven at the **hook** level: `curl https://example.com` denied, `curl -x proxy.example.com localhost:8000` denied, `nc example.com 443` denied, `curl localhost:8000` and `curl 127.0.0.1:5000/api` allowed
- [ ] `DESIGN.md` records that the network rule is a speed bump, not a sandbox, and that real containment would need OS-level isolation
- [ ] `serve.sh` is a required initializer artifact: a run refuses to start without it, and `initializer.md` distinguishes it from `init.sh`
- [ ] Teardown runs from a `finally` and is proven for all four exits — success, error, timeout, `KeyboardInterrupt` — with a test asserting the process group is gone afterwards
- [ ] Session process-group reaping applies to `run` as well as `audit`, since the leak it fixes already exists there

**Throughout**

- [ ] Every commit message proposed to the user and approved before landing
- [ ] Every doc edit (`README.md`, `DESIGN.md`) proposed to the user and approved before landing
- [ ] No repository was driven by the harness to test any of this
- [ ] The manual chain in part 7 run, with session 1's log committed as a tracked file

**Report at the end:** what changed per slice, the pre-fix failure output proving each new test is real, anything in the plan that turned out wrong or impossible, and any decision you had to make that this document did not cover.

---

# Reference — the harness as it stands

Everything below describes the harness **before** the changes above, at commit `2f75e99` on `developer-tooling/agent-harness`. `DESIGN.md` says why each piece exists; this says what is connected to what.

## R1. The four verbs

`harness.py` is the entry point; `agent_harness/cli.py` is the argument parsing and dispatch. Everything below hangs off one of these four.

```
                        harness.py <verb> --repo <target repo>
                                     |
      +--------------+---------------+---------------+
      |              |               |               |
   doctor          init             run           status
      |              |               |               |
      v              v               v               v
  preflight     preflight       preflight       preflight
  only          + 1 session     + N sessions    (read only)
                                                      |
  "is the        "build the      "work the        "where did
   environment    definition      list down"       it get to"
   sane"          of done"
```

`doctor` and `status` cost nothing and start no session. `init` runs exactly one session. `run` runs up to `--sessions` of them (default 10).

## R2. Preflight — the gate every verb passes through

`agent_harness/preflight.py`. Fails loudly and early, because the audience for the failure is someone reading a log the morning after.

```
  run_preflight(repo_path, require_clean_tree)
        |
        +-- `claude` on PATH? ......................... no -> HarnessError
        +-- `git` on PATH? ............................ no -> HarnessError
        +-- claude --version parses? .................. no -> HarnessError
        |     |
        |     +-- < 2.0.0 (MINIMUM) ................... -> HarnessError
        |     +-- != 2.1.234 (TESTED) ................. -> warning, continues
        |
        +-- claude --help documents every flag
        |   in REQUIRED_CLI_FLAGS? .................... no -> HarnessError
        |
        +-- resolve repo root (git rev-parse) ......... not a repo -> HarnessError
        |
        +-- work tree clean? .......................... dirty -> HarnessError
        |                                                (unless --allow-dirty)
        v
  PreflightReport(cli_version, repo_root, warnings, authentication)
```

> The CLI-flag check exists because `claude` is a moving external surface. A renamed flag becomes a startup error naming the flag, instead of a session that silently behaves differently than its prompt intends.

## R3. `init` — one session, and who writes what

The split matters: the **harness** writes the spec and the plumbing, the **initializer session** writes the definition of done.

```
  init --spec <file>
        |
        v
  preflight  ->  Workspace(repo_root)
        |
        +-- refuse if feature_list.json already exists (unless --force)
        +-- refuse if --spec file is missing
        |
        v
  HARNESS WRITES:
        .agent-harness/spec.md            <- copied from --spec
        .agent-harness/.gitignore         <- excludes local/
        .agent-harness/local/logs/        <- empty
        .agent-harness/local/settings.generated.json   <- wires the hook
        |
        v
  git commit (workspace only)   "so the run starts from a known commit"
        |
        v
  +---------------------------------------+
  |  INITIALIZER SESSION                   |
  |  prompt: prompts/initializer.md        |
  |  reads:  spec.md                       |
  |  writes: feature_list.json             |
  |          progress.md                   |
  |          init.sh                       |
  |          + the project scaffolding     |
  |            and test suite init.sh needs|
  +---------------------------------------+
        |
        v
  missing_initializer_artifacts(workspace)
        |  any of the four missing?
        +-- yes -> HarnessError, "re-run init --force"
        |
        v
  git commit (workspace only)   "so the next session reads them committed"
        |
        v
  print: N features defined, M already passing, $X spent
```

> The artifact check is there because the initializer is a model, so "it said it was done" is not evidence. Every coding session depends on those four files existing.

> The spec is **copied into the repo** rather than passed as a prompt string, so every later session reads the same source of truth off disk and the record of what was asked for lives in git history.

## R4. `run` — the coding loop

This is the centre of the thing. `agent_harness/loop.py`. One feature per session, and the loop — never the session — decides when the run ends.

```
  run --sessions N --total-budget-usd B
        |
        v
  preflight -> workspace present? (else "run init first")
        |
        v
  regenerate local/ + settings.generated.json
        |
        v
  +===== for index in 1..N ==========================================+
  |                                                                  |
  |  read feature_list.json  ->  document_before, summary_before     |
  |        |                                                         |
  |        +-- every feature passes? ............ STOP "complete"    |
  |        +-- total spend >= B? ................ STOP "budget"      |
  |        |                                                         |
  |  target = next_feature()                                         |
  |        (lowest priority number, ties by declaration order —      |
  |         the harness picks so two runs do the same work in the    |
  |         same order)                                              |
  |        |                                                         |
  |  head_before = git rev-parse HEAD                                |
  |        |                                                         |
  |  render prompts/coding.md  +  prompts/system_contract.md         |
  |        |                                                         |
  |        v                                                         |
  |  +------------------------------------------+                    |
  |  |  CODING SESSION  (see R5)                 |                   |
  |  |  label: session-NNN, uuid session id      |                   |
  |  +------------------------------------------+                    |
  |        |                     |                                   |
  |        |                     +-- timeout / unparseable           |
  |        |                          -> restore list if tampered    |
  |        |                          -> commit -> STOP "unfinished" |
  |        v                                                         |
  |  TAMPER CHECK  find_structural_changes(before, after)            |
  |        |                                                         |
  |        +-- changed? -> restore pre-session list                  |
  |                     -> commit -> STOP "feature list tampered"    |
  |        |                                                         |
  |  session reported is_error? ............ commit -> STOP "error"  |
  |        |                                                         |
  |  summary_after; features_passed += delta                         |
  |        |                                                         |
  |  PROGRESS CHECK                                                  |
  |    made progress = (more features pass) OR (git HEAD moved)      |
  |        |                                                         |
  |        +-- no -> stalled += 1                                    |
  |             |     stalled >= 2? -> commit -> STOP "stalled"      |
  |        +-- yes -> stalled = 0                                    |
  |        |                                                         |
  |  git commit (workspace only)  "session <index>"                  |
  |  warn if the session left project changes uncommitted            |
  |                                                                  |
  +==================================================================+
        |
        v
  print: stopped because <reason>. N sessions, M features newly
         passing, $X spent.
```

**Every exit is named.** The eight stop reasons:

| Stop | Fires when |
| :-- | :-- |
| session limit | the `for` loop runs out (the default outcome) |
| complete | every feature is marked passing |
| budget | accumulated cost reaches `--total-budget-usd` |
| session could not finish | the session timed out or returned nothing parseable |
| tampered | the feature list changed in a way only a human may change it |
| session error | the CLI reported `is_error` |
| stalled | 2 consecutive sessions neither committed nor passed a feature |
| dry run | `--dry-run`, prints the command and prompt and executes nothing |

> A long run that ends quietly is indistinguishable from a long run that succeeded. Hence: every stop prints why.

> Note the harness commits **only its own workspace files**, never project code. An uncommitted project change is a signal the loop needs to see, so it is warned about rather than swept up.

## R5. Inside one session

```
  SessionRequest ---> build_argv() ---> subprocess
        |
        v
  claude --print
         --output-format stream-json --verbose
         --model <opus>            --permission-mode <bypassPermissions>
         --session-id <uuid>       --max-budget-usd <10.0>
         [--effort <level>]        (omitted -> CLI's own default)
         --settings   .agent-harness/local/settings.generated.json
         --append-system-prompt "<system_contract.md>"
         "<coding.md, fully rendered>"          <- prompt is positional, last
        |
        v
  stdout: a stream of JSON events, ending in one result object
        |
        +--> logs/session-NNN-<uuid>.jsonl   the forensic copy
        +--> logs/session-NNN-<uuid>.md      the readable transcript
        +--> logs/session-NNN-<uuid>.json    the structured record
             (cost, turns, duration, denials, thinking tokens, final message)
```

> All three artifacts share the label and session id, so one `grep` finds every trace of one session. The uuid is caller-assigned so any session can be reopened later with `claude --resume <id>`.

> On timeout, partial stream and transcript are written **before** the error is raised. A session that ran for an hour and died should not also lose its record.

## R6. The write path, and the three layers guarding it

The feature list is the run's ground truth. A session may flip `passes` and nothing else, and that rule is enforced three times over rather than once.

```
   SESSION wants to record a verified feature
        |
        |  the ONLY sanctioned route:
        v
  +-------------------------------------------------------------+
  |  tools/mark_feature.py <feature-id> pass --test "<command>"  |
  +-------------------------------------------------------------+
        |
        +-- find_workspace()      walk up from cwd for .agent-harness/
        +-- read_document()       load + validate the whole list
        +-- parse_verification()  reject: empty
        |                                 bare no-op (true, :, exit 0, echo)
        |                                 "manual:" with no reason
        +-- set_feature_status()  validates the id FIRST, computes the update
        +-- run_check()           runs the named command in a shell,
        |                         cwd = repo root, 300s timeout
        |                              |
        |                              +-- non-zero exit -> nothing recorded
        +-- write_document()      only now does the file change
        +-- print the new tally
```

> The id is validated **before** the check runs, so a typo'd id fails immediately instead of after a full suite run whose "check passed" line would sit next to the error inviting the wrong conclusion.

Everything that is not that route is blocked, at three separate layers:

```
  LAYER 1 — the prompt
    system_contract.md rule 3: never edit feature_list.json or spec.md.
    A rule that only exists in a prompt is one the model may talk itself
    out of at three in the morning. Hence layers 2 and 3.

  LAYER 2 — the PreToolUse hook (runtime block)

    session's tool call
        |
        v
    settings.generated.json
      matcher: Edit|Write|MultiEdit|NotebookEdit|Bash
        |
        v
    tools/protect_feature_list.py     <- stdin: the hook event JSON
        |
        v
    agent_harness/guard.py :: decide()
        |
        +-- file-writing tool?
        |     join with cwd -> realpath (defeats symlinks)
        |     -> is it spec.md / feature_list.json, and does it exist?
        |          yes -> DENY
        |
        +-- Bash?
        |     split into segments on && || ; | newline
        |     for each segment:
        |       runs mark_feature.py in command position,
        |         with no $( ) or backticks?  -> allow
        |       names a protected file AND mutates it
        |         (sed -i, tee, >, cp, mv, ln, python -c,
        |          node/ruby/php/deno/bun, git checkout/restore/
        |          stash/apply, fd-numbered redirects)?  -> DENY
        |
        v
    exit 0 = allow    |    exit 2 = block, stderr goes back to the model
                      |
    unreadable event -> exit 0, FAILS OPEN by design (layer 3 catches it)

  LAYER 3 — the post-session backstop

    loop compares the list it held before the session against the file
    on disk after it:

      find_structural_changes(before, after) flags
        - a feature added          (padding the list with easy wins)
        - a feature removed
        - any immutable field changed (id, category, priority,
                                       description, steps)
        - a flip to passing carrying no verified_by
                                  (the sanctioned path always supplies it)

      any of those -> restore the pre-session list, commit, stop the run
```

> Layer 2 is a **denylist** — it enumerates bad shapes. That is a known design property, not an oversight; see `pentest-2026-08-18.md` for what got past it and `DESIGN.md` for the allowlist alternative.

> Layer 3 exists precisely because layer 2 fails open. The state that matters is re-read from disk after every session regardless of what the hook did or did not see.

## R7. Where it all lives on disk

Inside the **target** repository, everything the harness touches is under one directory — one thing to look at, one thing to delete.

```
  <target repo>/
    .agent-harness/
      spec.md                 committed   harness writes it from --spec
      feature_list.json       committed   initializer writes; sessions flip
                                          `passes` via mark_feature.py only
      progress.md             committed   initializer writes; every session
                                          APPENDS (never rewrites)
      init.sh                 committed   initializer writes; brings the
                                          project up and runs the suite
      .gitignore              committed   harness writes it
      local/                  gitignored
        settings.generated.json           regenerated every run — holds
                                          absolute paths for THIS machine
        logs/
          init-<uuid>.jsonl / .md / .json
          session-001-<uuid>.jsonl / .md / .json
          session-002-<uuid>.jsonl / .md / .json
          ...
```

> The committed/local split is load-bearing. The committed four are how one session speaks to the next, so they have to survive in git. Generated settings and transcripts carry machine-specific paths and noise nobody wants in a diff.

The **harness itself** lives outside the target repo entirely, and is invoked by absolute path:

```
  scripts/agent-harness/
    harness.py                 entry point
    agent_harness/
      cli.py                   the four verbs
      config.py                constants, SessionRequest, SessionResult
      preflight.py             R2
      workspace.py             the path model for .agent-harness/
      loop.py                  R3 and R4, the orchestration
      session.py               argv, subprocess, stream parsing
      records.py               transcript rendering, session numbering
      features.py              the feature schema, verification, tamper check
      guard.py                 the hook's judgment (R6, layer 2)
    prompts/
      initializer.md           what the one init session is told
      coding.md                what every coding session is told
      system_contract.md       appended to the system prompt, both kinds
    tools/
      mark_feature.py          the sanctioned write path
      protect_feature_list.py  the hook shim (stdin -> guard -> exit code)
    tests/                     187 tests
```

> The tools are run **by the agent, from inside the target repo, by absolute path**, which is why both start with a `sys.path` bootstrap: the harness is not an installed package.

## R8. Defaults, in one place

From `agent_harness/config.py`. These are what a run does if you pass nothing.

| Setting | Default | Note |
| :-- | :-- | :-- |
| model | `opus` |  |
| effort | _unset_ | flag omitted entirely, so the CLI's own level stands |
| permission mode | `bypassPermissions` | unattended runs cannot answer a prompt |
| per-session budget | `$10.00` | enforced by the CLI itself, survives a harness bug |
| session timeout | 3600s |  |
| max sessions per run | 10 |  |
| consecutive stalls | 2 | then the run stops |
| clean work tree | required | `--allow-dirty` opts out |

> The safety story for `bypassPermissions` is the clean-tree requirement: the harness starts every session from a known-good commit, so a bad session is revertable.
