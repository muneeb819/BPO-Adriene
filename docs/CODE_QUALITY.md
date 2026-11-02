# Code quality: what is enforced, and how to check it yourself

This file exists so a reviewer does not have to take any of the following on
trust. Every claim has a command next to it that reproduces the result.

Run everything below from a fresh clone:

```bash
git clone https://github.com/muneeb819/BPO-Adriene.git && cd BPO-Adriene
make install
```

## The four gates

| gate | command | current state |
|---|---|---|
| tests | `make test` | 450 passed, 40 skipped |
| lint | `make lint` | all checks passed |
| types | `make type` | no issues in 64 files |
| fresh clone | CI job `fresh-clone` | `make install && test && lint && type` |

CI runs all four on every push to `main` and every pull request
(`.github/workflows/ci.yml`). The skipped tests are not hidden failures: they
report why they skipped, and the three legitimate reasons are no neural voice
models, no LiveKit extra, no cloud key.

## Reproducibility

Dependencies are pinned, not ranged:

```bash
make install                                   # installs from the lockfile
grep -c '==' requirements-lock.txt             # 60 pinned packages
diff <(make -s lock >/dev/null && cat requirements-lock.txt) requirements-lock.txt
```

`make lock` re-freezes in a scratch virtualenv. Verified by installing the
lockfile into two empty environments and diffing `pip freeze`: identical both
times.

The backend has its own pinned set (`backend/requirements-lock.txt`), resolved
for CPython 3.12 — the version the Docker image ships. That is a real
constraint, not an accident: `kokoro` declares `Requires-Python >=3.10,<3.13`,
so the backend path will not install on 3.13. The root lockfile has no such
cap.

## Structure

The three god files were `server/app.py`, `agent/tts.py` and
`agent/voice_library.py`.

```
server/app.py          2745 -> 217 lines
  server/context.py      16   the one shared Config
  server/pages.py      1235   the embedded console HTML
  server/routes/voices.py       777
  server/routes/debug.py        249
  server/routes/recordings.py   164
  server/routes/campaigns.py    127
  server/routes/voice_ws.py      96
  server/auth.py          93   password, Basic auth, cookie, ws handshake
  server/call_log.py      43   load + crash-safe atomic save

agent/tts.py           1045 -> 444 lines
  agent/tts_engines/chatterbox.py   334
  agent/tts_engines/local_http.py   150   shared Kokoro/Piper transport
  agent/tts_engines/kokoro.py       110
  agent/tts_engines/elevenlabs.py    78
  agent/tts_engines/_contract.py          what a mixin may assume

agent/voice_library.py  757 -> package, largest module 480
  agent/voice_library/creation.py   480
  agent/voice_library/models.py     167
  agent/voice_library/store.py      132
  agent/voice_library/__init__.py    56
  agent/voice_library/library.py     49
  agent/voice_library/_contract.py   36
```

Each extraction is its own commit and was verified by running the suite
against that commit alone, not only against the branch tip. Every moved route
was additionally requested through a `TestClient` — Starlette registers
`include_router` calls lazily as `_IncludedRouter`, so reading `app.routes` for
paths reports that nothing moved when it has.

`server/pages.py` at 1235 lines is the largest file in the tree and is left
that way deliberately: it is three HTML documents as string constants, with no
behaviour to separate. `server/routes/voices.py` at 777 lines is also left
alone — 17 endpoints over six helpers that only they use. Splitting either
would move lines around without separating responsibilities.

`backend/app/` is where the remaining large modules are; it is untouched by
this work.

## Security posture

| check | command | result |
|---|---|---|
| no shipped default secret | `grep -rn "dev-secret\|changeme" agent server backend` | no matches |
| launchers refuse to start without `SECRET_KEY` | `./start.sh` with it unset | exits 1 with instructions |
| `.env.example` ships `SECRET_KEY=` empty | `grep '^SECRET_KEY=' .env.example` | `SECRET_KEY=` |
| dashboard password documented | `grep -A3 WEB_PASSWORD .env.example` | warns when the console is LAN-reachable |

`WEB_PASSWORD` being unset leaves the dashboard open to the network. That is
documented in `.env.example` and `DEPLOYMENT.md` rather than silently
permitted — but it is a real footgun if you expose port 8055, and the only
safe default is to set it.

## Configuration completeness

`.env.example` documented about 20 of the settings the code reads. A test now
derives the expected set by scanning the whole tree for `env("NAME")`,
`os.getenv("NAME")` and `os.environ["NAME"]`:

```bash
pytest tests/test_deployment.py::test_env_example_documents_every_setting -v
pytest tests/test_deployment.py::test_env_example_has_no_unknown_keys -v
```

Both directions are checked — a setting in the code but not the file (nobody
can find it), and a setting in the file but not the code (it misleads).

## What is not done

Stated plainly, because a quality document that only lists wins is marketing:

- **`server/routes/voices.py` is 777 lines** and `server/pages.py` is 1235.
  Both are explained above rather than split for the sake of the number.
- **History is thin.** Before this work the repository was a 9-day, single
  author burst with 50 commits and no tags. It now carries focused,
  test-bearing commits and a release tag, but sustained multi-author history
  cannot be manufactured — it accumulates or it does not.
- **`backend/requirements.txt` ranges are still loose** at the top level; the
  lock beside it is what installs. `tests/test_dependency_pins.py` now fails if
  a package pinned in two files drifts to two versions, or if a lockfile stops
  covering what its `requirements.txt` asks for.
- **Dependabot alerts are disabled** on the repository, so the dependency PRs
  are version bumps, not security fixes.
- **No end-to-end test of the voice path** against a real microphone or a real
  PBX. The suite exercises the adapters and the API, not audio hardware.
- **Frontend has no automated tests.** The React console is built in CI
  (`make build` runs from the Dockerfile), but nothing asserts its behaviour.
