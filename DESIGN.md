# Design decisions

Why this harness is shaped the way it is. The source is Anthropic's [Effective harnesses for long-running agents](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents); this file records where the implementation follows it, where it deviates, and what those choices cost.

## The problem being solved

An agent works in discrete sessions and each one begins with no memory of the last. The article's image for it is a project staffed by engineers working in shifts where every engineer arrives amnesiac. Compaction does not fix this: it summarises, and a summary of a half-implemented feature is not the same as a working tree that was left in a known state.

Two failure modes follow, and the article names both. The agent tries to one-shot the whole project, runs out of context mid-implementation, and leaves the next session to guess. Or, later in a project, a session looks around, sees that a lot exists, and declares the job done.

Both are the same underlying problem: **the agent is allowed to decide what finished means.** Nearly every decision below follows from taking that decision away from it.

## 1. The headless CLI, not the Agent SDK

Both drive the same engine, since the Python SDK spawns the `claude` binary underneath. The choice is about the control surface around a session, not the quality of a session.

The CLI won on reuse. This harness is meant to run against many repositories, and with the CLI nothing installs into any of them: the target repo is an argument, the harness is stdlib-only, and a session is a shell command you can paste and run by hand, which is the single most useful property to have while debugging a run that failed at 3am. New agent roles are argv and a prompt file rather than code. Parallel sessions, if ever wanted, are just processes.

What that costs: the CLI's flags and result keys are a moving surface, where the SDK's typed API is pinned in a lockfile. That cost is paid down in §5. The other cost is real and unpaid: the SDK can enforce rules with in-process tools and permission callbacks, which is a stronger mechanism than the hook used here.

Because that trade may be worth revisiting, every line that knows a session is a subprocess lives in `session.py`, in two pure functions and one impure one. Swapping to the SDK is a rewrite of that file, not of the harness.

## 2. The harness lives outside the repository it drives

The only thing installed into a target repository is a `.agent-harness/` directory of data. The guard hook and the mark-feature tool are wired by absolute path back to the harness, generated per run into a gitignored settings file.

The alternative, copying tooling into each repository, means every repo carries a snapshot of the harness at the moment it was set up, and upgrading the guard means visiting all of them. Namespacing under one directory means every repository looks identical to the harness and there is exactly one thing to delete when a run is abandoned.

Inside the workspace, the committed/local split is load-bearing. The spec, feature list, progress log, `init.sh` and `serve.sh` are committed because the whole premise is that they are how one session speaks to the next; a session reads them out of the repository. Generated settings and session transcripts are not, because they carry machine-specific absolute paths and nobody wants a transcript in a diff.

## 3. Enforcement instead of exhortation

The article's feature list is JSON, and its rule is a strongly-worded prompt: _"It is unacceptable to remove or edit tests because this could lead to missing or buggy functionality."_ It also notes that JSON was chosen over Markdown because the model is less likely to overwrite it. Both of those are admissions that the rule is only as strong as the model's mood.

Here the rule is a mechanism, in three layers:

1. **A narrow write path.** `tools/mark_feature.py` can move exactly one feature along exactly one edge of the state machine, and only after watching its check behave the way that edge requires. It is incapable of anything else.
2. **A `PreToolUse` hook** that denies every other route to the file: direct edits, and shell commands that look like writes.
3. **A post-session comparison.** The loop reads the feature list before and after each session and stops the run if the shape changed, if a claim arrived without a record behind it, or if a record a session wrote no longer matches the files it names.

The third layer exists because the first two can fail. The hook fails open when it cannot parse an event, and its Bash matching is a heuristic that a determined command can slip past. A guard is invisible while it works, so it needs a check that is loud when it stops.

Additions are refused alongside deletions and rewrites: a list that can grow can be padded with features that are already true, which is the same "declare victory" failure by a slower route.

The human is deliberately outside all of it. Hooks only see the agent's tool calls, so editing the list by hand is the intended way to correct a spec that really did change.

## 4. Feature choice is arithmetic, not judgment

The article has each session read the list and choose "the highest-priority feature that's not yet done". Here `priority` is a required integer field and the harness computes the choice: lowest number still failing, ties broken by declaration order, injected into the prompt.

Two runs over the same list then do the same work in the same order, which is what makes a run reproducible enough to compare. The session also spends its first tokens working rather than re-deciding what matters, and "highest priority" stops meaning whatever the model finds most appealing this morning.

The prompt keeps one escape: if the chosen feature turns out to be blocked by something unbuilt, say so in the progress file and work on the blocker instead. Silent substitution is what is being prevented, not judgment itself.

## 5. Pinning the CLI so a rename is an error, not a mystery

Every flag the harness passes and every result key it reads is declared in `config.py`, and `preflight.py` asserts all of them before a session starts. A missing flag refuses the run and names the flag. A missing result key raises instead of defaulting, because a run that reports zero cost and zero turns looks fine.

Version handling is asymmetric on purpose: older than the minimum refuses, newer than the version this was probed against only warns. Refusing every upgrade would break the tool routinely, and the flag-presence check is what actually catches a breaking change. The result schema was verified against a real CLI 2.1.234 run rather than assumed.

The failure this is aimed at is not a renamed flag. It is a renamed flag going unnoticed, so the session still runs while quietly ignoring the instruction the flag carried, and the damage shows up much later as the model appearing to be bad at its job.

## 6. Every stop condition is explicit and named

A run that ends quietly is indistinguishable from a run that succeeded, and that distinction matters most in the morning, when nobody was watching. The loop stops on: the list being complete, the session limit, the run budget, a session error, structural tampering, evidence that no longer matches the repository, and stalling.

Stalling is the answer to the article's second failure mode. An agent that surveys the project and calls it finished does not error, it returns cheerfully having done nothing. So progress is measured from the repository — did the passing count rise, did a test get recorded failing, did `HEAD` move — rather than from the model's account of itself. Two of those in a row ends the run.

A recorded red counts as progress deliberately. It is the half of the shift that cannot be faked, and a session that runs out of context between writing the test and committing it has done real work; charging it one of the run's two lives would end runs that were going fine.

## 7. End-to-end verification, generalised

The article's most striking result was that Claude marked features complete without checking them, and that giving it Puppeteer and telling it to test as a user changed the outcome dramatically.

Puppeteer is a web-app answer, and this harness has to work for a CLI, a service or a data pipeline. So what is carried by default is the demand, not the tool: start the thing with `serve.sh` and exercise it by the route a user takes. The recorded test is evidence, and it is the only evidence that survives to session thirty; what it cannot do alone is establish that the feature works now, since it tests the code you wrote against the assumptions you had while writing it, and those fail together. Both, therefore, and `serve.sh` exists because `init.sh` runs the suite and exits, which leaves a web app with nothing listening to drive. A project with a better answer supplies it with `--verification-notes`, which replaces that section of both prompts.

## 8. `bypassPermissions`, and a clean tree as the safety net

Unattended sessions cannot answer a permission prompt, and a denied tool call does not stop the agent, it makes it work around the restriction. A harness whose default mode causes that is a harness that produces confusing sessions.

So the default is full bypass, and the safety comes from somewhere honest: the target repository must be a clean git work tree before a run starts, which is what makes everything a session does revertable. `--allow-dirty` exists and says what it costs.

## 9. Prompts are files

`prompts/` holds the three prompts as Markdown: the initializer, the coding session, and the invariant contract appended to the system prompt of both. They are the part of this most likely to be tuned, and tuning them should not look like a code change. Substitution is literal `{{TOKEN}}` replacement rather than `str.format`, because the prompts contain JSON examples full of braces, and an unreplaced marker is an error rather than something that ships in the prompt.

## 10. No dependencies

`pyproject.toml` is untouched and `harness.py` carries inline script metadata with an empty dependency list. A developer tool that must run against any repository is worth more than one that is marginally nicer to write. The Agent SDK route would have brought roughly fifteen to twenty packages, via `mcp`, for what is ultimately a subprocess wrapper.

## 11. Verification has to leave something behind

The article stops at telling the agent to test end to end, and this harness originally did the same. Watching a real session showed what that buys: the model ran a command in a shell, wrote a paragraph in `progress.md` describing what it saw, marked the feature passing, and left nothing anyone could re-run. The prose was accurate. It was also unfalsifiable, and thirty sessions later it is indistinguishable from prose about a check that never happened.

So marking a feature passing now requires naming the command that proves it, and that command is run before anything is recorded. A failing command records nothing. The command itself is stored on the feature, so the evidence is re-runnable rather than narrated.

The escape hatch is deliberately expensive. `manual: <reason>` is accepted, the reason is required, and manual verifications are counted separately and printed by `status`. An escape hatch that costs nothing becomes the default route and the mechanism turns decorative; one that has to justify itself and shows up in a tally does not.

Two consequences fall out of this. The initializer is now told to stand up a test suite and one passing example before anything else, because a project with nowhere to put a test is one where every feature becomes a manual exception. And `init.sh` runs the suite, which makes the first act of every session a real check on what it inherited rather than a look around and an assumption.

**The ordering closes the rest of it.** A feature cannot reach `passing` unless the harness has already watched the same, unchanged test fail, before the implementation existed. That turns the transition from something a session asserts into something the harness saw happen, and it is what makes `--test "test -f README.md"` useless rather than merely weak: a check that passes before the feature exists is refused where it is offered.

Three things hold it up, and they are different checks rather than one repeated. The transition table is enforced inside `mark_feature.py`, one call at a time, because that is the only place a single move is visible. The loop's before-and-after comparison catches a list that changed shape. And `find_unverifiable_evidence` re-hashes recorded tests against the working tree after each session, which is the only one of the three that can catch a record that is complete, plausible, and false.

Worth stating plainly: **the table cannot be enforced at the session boundary.** One honest session goes `no-test` → `test-failing` → `passing`, so the comparison sees `no-test` → `passing`; and since every state reaches `no-test` for free and `no-test` reaches every other state, every before/after pair is the endpoint of some legal path. Asking "is this a legal edge" there would reject real work while forbidding nothing.

The re-hash is bounded to the features a session moved, and that bound is a trade rather than an optimisation. Test files hold more than one test in every real suite, so an honest addition changes the bytes an earlier feature was digested against; re-hashing everything read that as tampering and stopped the run, then stopped the next run the same way, since nothing about the repository had changed. What the bound costs is that a test weakened in place, on a feature the session did not touch, is reported by `status` rather than refused by the loop. `status` still re-hashes everything, because a person can tell "this file grew a second test" from "this test was rewritten to assert less" and a hash cannot.

What this still does not do is judge the _quality_ of the test. A session writes the test it is then measured by, and a weak test written first is still a weak test. That is left to a human at review time, deliberately.

## 12. A session's whole record, not its last sentence

The first version kept the CLI's final result object per session, which is the one part of a session that says nothing about what it did. When a session behaved oddly there was nothing to read.

Switching to `--output-format stream-json` costs nothing at the parse boundary, because the last event is byte-for-byte the object already being parsed, and everything before it is the record that was missing. Each session now leaves three files: the raw stream verbatim, a rendered Markdown transcript with the reasoning and every tool call and result, and the structured summary. All three are written before the result is parsed, since the sessions worth reading are the ones that ended badly.

The transcript renderer never raises. It runs after the session is over and its only job is to produce something readable; throwing on an unfamiliar event shape would destroy the artifact exactly when the session did something unexpected, which is the only time anyone opens it.

Effort is reported for the same reason. `--effort` is passed through under the CLI's own name rather than a second vocabulary, and both the level and the thinking tokens the session actually spent are recorded.

The level is pinned to `high` rather than inherited. Deferring to whatever an interactive session happened to be configured with makes two runs of the same feature list behave differently for a reason invisible from outside the run, and the difference surfaces only as a worse result nobody can account for. Reproducibility beats deference for a tool that runs while nobody is watching; `--effort` remains for anyone who wants otherwise, and `None` still means "inherit".

## 13. The harness owns every process it starts

Every subprocess runs in a session of its own and is reaped as a group, from a real `finally`. A session that starts a dev server and dies leaves it holding a port, and the failure that produces lands somewhere else entirely: the _next_ run's `serve.sh` cannot bind, for reasons that look nothing like the real cause. Ownership sits with the harness rather than the session, because a session told to clean up after itself will usually do it and will occasionally die before it gets there, and "usually" is not a lifecycle. Killing the group rather than the process is what catches the children.

The same change fixed a bug that had been there from the start. `subprocess.run` and `communicate()` wait for end-of-file on the pipes rather than for the process, and a session that leaves a child running has handed that child its stdout. The pipe stays open after the session itself has exited, so the harness sat there for the full session timeout on a session that had finished in a minute. Output is now read on threads, the wait is on the process, the group is reaped — which closes the descriptors the orphan was holding — and only then is the output collected. One test went from 206 seconds to 7.

This is POSIX-only as written. `start_new_session` and process groups do not exist on Windows, which needs `CREATE_NEW_PROCESS_GROUP` and a different signal. The harness targets macOS and Linux, and the teardown degrades to killing the single process elsewhere rather than pretending to be portable.

## What real runs changed

Both guard bugs in the git history were found by running the thing against a throwaway repository with `--model haiku`, not by reading the code.

**The guard blocked the initializer from creating the feature list.** The model did not stop or report the obstacle. It worked around the block and finished the session cheerfully, without the one file the entire run depends on. The rule became "protect what exists", and the loud artifact check is the only reason a silent work-around became a thirty-second diagnosis.

**The guard blocked `git add .agent-harness/feature_list.json`**, because the substring matcher found `dd ` inside `add`. The session had just recorded a verified feature and was doing exactly what it was told to do next. A guard that punishes compliance leaves the model no route but the ones the guard does not recognise. Matching is now word-bounded and per command segment, and git is allowed against the protected files outright, except for the subcommands that roll a file back.

**A session verified a feature and left nothing behind.** It ran the check by hand, described it accurately in the progress log, and marked the feature passing. Nothing was reproducible, and the only record was prose that cannot be distinguished from prose about a check nobody ran. That is what §11 exists to fix.

**A log directory said sessions had gone missing.** Sessions were numbered from one on every `run` invocation, so five completed sessions appeared as two `session-001`s and two `session-002`s. Nothing had been lost; nothing could be read either. Numbering now continues from what the directory already holds.

**The initializer left the feature list uncommitted.** Told to "commit everything you created", it did not count files the harness had written, and the harness had also dirtied the very work tree it had just insisted was clean. The harness now commits its own workspace files, before and after the session. Project code is still never committed by the harness: an uncommitted project change is a signal the loop needs to see.

## Deliberately not built

- **Parallel sessions.** They would fight over one working tree. Doing it properly means git worktrees, which is a larger feature than it looks.
- **Specialised agent roles** (tester, reviewer, cleanup), which the article names as open future work. With this design they are a prompt file and an argv recipe, so the cost of adding them later is low and the evidence that they help is not in yet.
- **Resuming a specific session.** Sessions are meant to be disposable; the repository is the state. The session ids are recorded so `claude --resume` works by hand when you want to see what happened.
- **A cost dashboard.** Per-session cost is in each log record and the run prints a total.

## The 2026-08-18 penetration test

Once findings 1-9 from a code review were fixed, one session was pointed at the harness with the opposite instruction to every other: not to build, but to break. It played the in-session agent trying to get features marked passing without doing the work, and it ran every attack against the real guard, the real `mark_feature.py`, and the real backstop rather than reasoning about them. The log it left is `pentest-2026-08-18.md`, a point-in-time record as of commit `02f3a02`: four confirmed exploits, thirteen attacks the guard held. It is kept because the held attacks are the useful half — they are the regression surface, now pinned as tests — and because the exploits show the shape of the next one.

Three of the four (a command-substitution rider on a sanctioned `mark_feature.py` call, a symlink to the protected file, and inline `node`/`ruby`/`php` writes the guard did not model) were closed in the commits after the log, each with a test that fails without the fix. They shared one root cause: the guard is a denylist, so it is only ever as complete as the last thing someone thought to forbid. The fourth is the one §11 already named. `--test true` marked a feature passing, because the guarantee was only ever "a command that exits 0 ran", and a no-op exits 0. Bare no-ops were refused first, which was a speed bump. The durable fix named there — take the check out of the session's hands — is now built, though by the other route: the harness watches the ordering rather than re-running checks in a clean tree. A no-op check cannot be recorded, because it cannot be watched failing.

## Superseded: a breaker and a builder, pitted against each other

This proposed making adversarial pressure a standing part of the loop, with a breaker agent writing falsifying tests and the loop alternating between it and a builder. **It is superseded**, and by two different things.

The conflict of interest it was designed to remove — the session writing the test it is then measured by — is narrowed by the ordering rule in §11. The session still writes its own test, but it can no longer write it _after_ seeing the implementation work, which is where most of the weakness came from.

The adversarial pass it wanted is not this harness's job. See below.

## The audit phase, built and then removed

An `audit` verb was built here and taken out again on 2026-08-18. It ran one session after the feature list completed, told to attack what had been built and to write ranked findings into `findings.json` under the feature-list schema, so that `run --list findings.json` could work them down. It carried a network allowlist confining the session to the local instance, an attempt log with a floor under it, and a check that the session had repaired nothing.

It was removed on build-versus-buy grounds, and the reasoning is worth keeping because the temptation will come back. `/code-review` and `/security-review` ship with Claude Code, the former running a multi-agent review with adversarial verification of each finding. What was built here was one session, an attempt floor nothing had measured, and roughly 370 lines of network denylist whose own comments conceded it was a speed bump rather than a sandbox. That is competing with a maintained product on its own ground, using a worse implementation, in a tool whose actual job is something the built-ins do not do at all.

**What this harness is for is the long build**: a definition of done the agent cannot edit, evidence it cannot author, and a loop that survives fifty sessions. Nothing built in does that. Reviewing a finished branch, on the other hand, is well covered, and the sensible pipeline is `harness.py run` to build and then `/code-review` on the branch to attack it.

The prompt that was written for the audit is worth recovering from git history if anyone rebuilds this, particularly its reproduce-or-it-is-not-a-finding rule. The verb itself should not come back without a reason that names something `/code-review` cannot do.

## Still open

- **Test quality.** §11 closes claiming-versus-checking and leaves checking-versus-checking-something-worthwhile open. A session writes its own test first, which is better than writing it last, and it is still the author. The answer is a human reading the tests at review time, and there is no mechanism proposed for it.
- **No real repository has been driven by any of this.** Every mechanism above is proven against a fake CLI. The design decisions are hypotheses until a real project has been built with it, and the first one will be worth more than the next three code reviews.
