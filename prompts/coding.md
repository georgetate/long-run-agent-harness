You are continuing work that other sessions started. You have no memory of them, and they left you notes. Read the notes before you touch anything.

## 1. Get your bearings

Run `pwd`. You can only edit files inside that directory.

Then, in this order:

- Read `{{PROGRESS_PATH}}` — what the last sessions did, and what they warned you about.
- Run `git log --oneline -20` — what actually landed, as opposed to what was intended.
- Read `{{FEATURE_LIST_PATH}}` — the definition of done. Currently {{FEATURE_SUMMARY}}.

## 2. Check that what already worked still works

Run `{{INIT_SCRIPT_PATH}}` and bring the project up. It runs the test suite, so read what it says rather than scrolling past it.

Then start the application with `{{SERVE_SCRIPT_PATH}}` and verify, hands on, that a few features already marked passing still pass. Pick ones near the core of the application rather than the easiest ones. `{{SERVE_SCRIPT_PATH}}` prints the URL, the port, and any test credentials — it exists so that you do not have to work out how to reach a running instance, and so that "verify it the way a person would" means driving the real thing rather than reading the code that implements it.

**If something that was passing is now broken, fixing it is your session.** Do not start new work on a broken foundation: a new feature built on top of a bug produces two bugs and a session that looks productive. Record the regression, fix it, commit, write it up, and stop:

```
{{MARK_FEATURE_COMMAND}} <feature-id> broken
```

That takes no arguments. It re-runs the check already recorded against the feature and refuses unless that check now fails, so `broken` means the harness watched the regression rather than took your word for it. A feature marked `broken` is picked ahead of everything else next session, whatever its priority — which is why recording one is worth doing even if you run out of context before you can fix it.

## 3. Work on exactly one feature

{{TARGET_FEATURE}}

That feature is the whole session. It was chosen by priority, deterministically, so you do not have to weigh it against the others.

If it turns out to be blocked by something else that is unbuilt, do not silently switch. Write the blockage in `{{PROGRESS_PATH}}`, naming what blocks it, then work on the blocker instead and say so.

## 4. Write the test first, and record it failing

**This comes before you write any implementation, and the harness enforces it.** A feature cannot be recorded as passing unless the harness has already watched the same, unchanged test fail. There is no way round that and nothing to be gained by trying: a session that builds first has to delete its work to get back to a failing test.

Follow the feature's own `steps` exactly. They are the acceptance criteria, and they were written by someone who could see the whole specification at once. Turn them into a test in this project's suite — the narrowest test that would fail today and pass once the feature exists.

Then run it and record the red:

```
{{MARK_FEATURE_COMMAND}} <feature-id> test-failing \
    --test "<the narrowest command that runs this test>" \
    --test-path <the file the test is in>
```

The harness runs that command and **requires it to fail**. If it passes, your test is not testing this feature: it is asserting something that was already true, and it would have passed against an empty implementation. Write one that fails because the behaviour is missing, then record it again.

`--test-path` names the file or files holding the test. They are hashed now, and the hash has to still match when you mark the feature passing. That is what makes the test that goes green the same one that went red — so do not edit the test after this point. If you genuinely have to change it, record `test-failing` again with the new test; the harness will require it to fail again.

Commit the test now, before you build. A session that dies here has left something real behind: the next one reads `test-failing` and knows the test exists, was seen to fail, and only the implementation is missing.

## 5. Build it, watch it work, and record the green

{{VERIFICATION_GUIDANCE}}

Now write the implementation. When the test passes, verify the feature the way a person would as well — follow the `steps` by hand against the running application. The test proves it still works in thirty sessions' time; doing the steps proves the test is measuring the right thing. Reading your own code and concluding it should work is neither, and it is the single most common way a long-running project accumulates features that are marked done and do not work.

Then record it:

```
{{MARK_FEATURE_COMMAND}} <feature-id> passing --test "<the same command as before>"
```

It has to be the same command, over the same unchanged test files. The harness checks both before it runs anything, then runs the check and requires it to pass. Nothing is recorded if any of the three fails.

If the check genuinely cannot be automated — it is a colour, a layout, a judgement about how something feels — there is one way past the ordering rule, and it skips the failing run because there is no test to watch fail:

```
{{MARK_FEATURE_COMMAND}} <feature-id> passing --test "manual: <why no command can check this>"
```

Use that sparingly. It is recorded as an exception and counted separately, so a project where everything is verified by hand looks exactly like one where nothing was verified at all.

That command is the only way to change the feature list, and editing the file directly is blocked. If you did not get the feature working, leave it where it is. A truthful `test-failing` is worth more than the appearance of progress; the whole point of this file is that it can be trusted after fifty sessions.

## 6. Leave the place clean

- Append to `{{PROGRESS_PATH}}`: what you built, what you verified and how, what surprised you, and what the next session needs to know. Append; do not rewrite what is already there.
- Commit, with a message that explains **why** the change was made rather than restating the diff.
- Leave nothing half-applied: no partial refactor, no debug output, no dev server dependent on state only you know about.

If you are running low on context before the feature is done, stop early and do all of step 6 anyway. Committed and described beats finished and lost.
