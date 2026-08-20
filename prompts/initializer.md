You are the first session on this project. Nothing exists yet except a specification, and your job is to build the environment that every session after you will work inside. **You are not here to implement features.**

Run `pwd` first. You can only edit files inside that directory.

## Read the specification

The specification is at `{{SPEC_PATH}}`. Read all of it before you write anything. It is the only statement of what this project is for, and it is not editable by you or by any session after you.

## What you are here to produce

### 1. `{{FEATURE_LIST_PATH}}` — the definition of done

Expand the specification into a comprehensive list of end-to-end features. This file is the thing that stops later sessions from declaring the project finished when it is half built, so it has to be complete enough to be worth trusting. Size it to the specification rather than to your patience: a small utility is a couple of dozen features, a substantial application well over a hundred. Do not stop early because the list already feels long.

Every feature is something a person could sit down and check. "A user can open a new chat, type a query, press enter, and see a response" is a feature. "Implement the message store" is not: it is a task, and no one can look at a screen and tell you whether it is done.

The file is a JSON object in exactly this shape:

```json
{
  "version": 2,
  "features": [
    {
      "id": "chat-new-conversation",
      "category": "functional",
      "priority": 1,
      "description": "New chat button creates a fresh conversation",
      "steps": [
        "Navigate to the main interface",
        "Click the 'New Chat' button",
        "Verify a new conversation is created",
        "Check that the chat area shows the welcome state",
        "Verify the conversation appears in the sidebar"
      ],
      "state": "no-test"
    }
  ]
}
```

Field rules, all of which are enforced by the harness and will fail the run if broken:

- `id` — short, kebab-case, unique, and stable. Later sessions address features by this string, so it must never change.
- `category` — a grouping of your choosing, for example `functional`, `visual`, `data`, `error-handling`, `performance`.
- `priority` — an integer. Lower runs first. The harness hands each session the lowest-numbered failing feature, so this field is the build order. Put foundations first: something that cannot be verified until something else exists must have a higher number than the thing it depends on.
- `description` — one line, phrased as an observable outcome.
- `steps` — the checks a person would perform, in order. Specific enough that a session with no memory of this conversation can follow them exactly.
- `state` — `"no-test"` for every feature. All of them. Nothing has been built yet, so no test has been written and none has been seen to fail.

Add no other fields. `evidence` is written later, by `mark_feature.py`, and it is the only other field the file ever grows. It records what the harness saw when it ran a feature's check — the command, the test files, their hash, and when — and it is not something you write.

Write the `steps` so that a machine could carry most of them out. Every feature a later session marks as passing has to come with a command that reproduces its check, so a feature whose steps can only be followed by a human with an opinion is a feature that will end up recorded as a manual exception.

### 2. A test suite, and a way to run it

Set up whatever this project's ecosystem uses to run automated tests, and leave it working with at least one trivial test that passes. No session after you is allowed to mark a feature as passing without naming a command that proves it, and that command will be run before anything is recorded, so a project with nowhere to put a test is a project where every feature becomes a manual exception.

It must be runnable **one file, and ideally one test, at a time**. Every session records the narrowest command that proves its own feature, not the whole suite, so a runner that can only be invoked wholesale forces every check to be the entire suite — and a failure then points at nothing in particular. Print an example of the single-file invocation at the end of `{{INIT_SCRIPT_PATH}}`.

Do not write tests for features. There are none yet. Write the harness the tests will live in: the directory, the runner, the configuration, and one passing example that shows the shape.

### 3. `{{INIT_SCRIPT_PATH}}` — the way in

A shell script that takes the repository from a fresh clone to a running, testable state: install dependencies, run migrations, start whatever has to be running, and **run the full test suite**. It must be idempotent, because every session runs it. Make it executable.

Running the suite from `init.sh` is what makes a session's first act a real check on what it inherited, rather than a look around and an assumption.

Print, at the end, exactly what a session should do next to see the thing work: which URL, which command, which port. Every session pays the cost of rediscovering this if you do not write it down, and that cost is paid in tokens that could have gone to the work.

If something cannot be automated (a secret, a credential, an account), say so on stdout with the exact steps a human would take, and exit non-zero rather than continuing in a broken state.

### 4. `{{SERVE_SCRIPT_PATH}}` — the way in to a running instance

`{{INIT_SCRIPT_PATH}}` is the way in for a **build**. `{{SERVE_SCRIPT_PATH}}` is the way in for a **running instance**, and they are not the same thing: the init script runs the suite and exits, which for a web application leaves nothing listening. A later session told to verify a feature through its real interface has to be able to start the thing, and it has no memory of you to ask.

So this script starts the application in the foreground and leaves it running. It must print, before it blocks, everything needed to reach what it just started:

- the URL and port
- any test account, seed credential, or fixture data a session would need to log in
- anything that has to be true first, and how to make it true

Do not have it print instructions and exit; start the thing. The harness owns stopping it and will terminate the whole process group when the session is over, so do not write your own cleanup or trap.

**A project with nothing to serve still gets this file.** A library, a pure pipeline, a code generator: write the script, have it print one line saying there is nothing to serve and why, and exit zero. An honest no-op is worth more than a missing file, because a missing file is indistinguishable from one you forgot.

Make it executable.

### 5. `{{PROGRESS_PATH}}` — the log between sessions

Start it. One heading, and an entry describing what you set up, what you deliberately did not build, and anything a later session would otherwise have to rediscover. Later sessions append to this file; they do not rewrite it.

### 6. The scaffolding `{{INIT_SCRIPT_PATH}}` needs

Enough project skeleton for the init script to run and for a session to verify that the application starts: the directory layout, the dependency manifests, an entry point that runs and serves or prints something. Nothing more. Every actual feature belongs to a later session, and a feature you implement now is one that is implemented without being verified.

### 7. A commit

Commit everything you created. Write the message to explain **why** the environment is shaped this way, not to list the files, which the diff already carries.

## How you will be judged

The next session is a stranger with no memory of you. It will read `{{PROGRESS_PATH}}`, read the git log, run `{{INIT_SCRIPT_PATH}}`, read `{{FEATURE_LIST_PATH}}`, start the application with `{{SERVE_SCRIPT_PATH}}`, and get to work. If any of those five leave it guessing, this session failed, however good the code was.

The harness checks that all of them exist before it runs a single coding session, and refuses the run if any is missing.

{{VERIFICATION_GUIDANCE}}
