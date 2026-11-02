# Contributing

The goal of this file is that a change arrives in a form a reviewer — or a
buyer reading the git history — can act on months later.

## One change, one commit, tests included

A commit should contain **the behaviour and the test that pins it**. If you
cannot write the test, that is usually a sign the change is two changes, or
that its behaviour is not yet decided.

```bash
git checkout -b feat/skip-blank-disclosures
# ... change agent/conversation.py ...
# ... add tests/test_conversation.py::test_empty_disclosure_is_skipped_not_crashed ...
git add agent/conversation.py tests/test_conversation.py
git commit -m "fix: handle empty disclosure in Conversation.start

A campaign with a blank disclosure line left the caller in silence.

Adds test_empty_disclosure_is_skipped_not_crashed."
```

Rules of thumb:

| do | don't |
|---|---|
| one behaviour per commit | one commit with a feature, a refactor and reformatting |
| name the test in the message | "misc fixes", "updates", "more stuff" |
| extract a module in its own commit | extract *and* change behaviour together |
| let a lint/type fix-up be its own commit | bury 500 autofixes inside a feature |

Refactors follow the same rule: move code, keep tests passing, say so. A
refactor commit that also changes behaviour cannot be reviewed — there is no
way to tell which diff caused a regression.

## Before you push

```bash
make lint     # ruff — must pass
make type     # mypy — must pass
make test     # pytest, both suites — must pass
```

CI runs exactly these on every push and pull request, plus a `fresh-clone` job
that installs from the lockfile and runs them again. If it is green locally it
will be green there.

## Dependencies

`requirements.txt` holds the supported ranges; **`requirements-lock.txt` is
what installs**. To change a dependency:

```bash
# 1. edit the range
vim requirements.txt
# 2. re-freeze the lock in a scratch venv
make lock
# 3. commit both, together, with the reason
```

Same for `backend/requirements.txt` and `backend/requirements-lock.txt`, which
also must be resolved for CPython 3.12 (see the note in that file about torch).

## Tests

- `tests/` — the bot: brain, conversation, campaigns, TTS, voice library, API.
- `backend/tests/` — the FastAPI app: state machine, brain, LiveKit.

A bare `pytest` runs both (configuration lives in `pyproject.toml`).

Tests that need something the machine may not have must **skip, never fail**:

```python
needs_voices = pytest.mark.skipif(
    not nt.available(), reason="no local neural voices — run scripts/fetch_neural_voices.py")

@needs_voices
def test_piper_preview_renders_audio(): ...
```

The three reasons that legitimately skip: no neural voice models
(`scripts/fetch_neural_voices.py`), no LiveKit extra
(`requirements-livekit.txt`), no cloud key.

Never make a test pass by weakening its assertion. If the code is wrong, fix
the code; if the expectation is stale, say in the commit message which
behaviour changed and why.

## Reporting a bug

Include the output of `make check` (readiness: keys, voices, audio) and the
engine you selected. Most "no audio" reports are a missing voice model or a
`TTS_PROVIDER` pointing at an engine with no key — `make check` names which.

## Releasing

```bash
# 1. CI green on main
git tag -a v0.2.0 -m "v0.2.0 — <what changed>"
git push origin v0.2.0
```

Tag only from a green `main`. The tag message carries the changelog; there is
no separate CHANGELOG file to drift out of date.
