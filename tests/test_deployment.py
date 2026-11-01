"""Deployment readiness: the files an operator relies on must stay correct.

These tests are about *deployment*, not features — they fail when the launchers,
the service unit, the container, the readiness report or the documented
configuration drift away from the code. That drift is exactly what breaks an
install on someone else's machine.

NOTE ON ARCHITECTURE (2026-11): the bot is a SINGLE process — FastAPI serves the
REST API, the WebSocket call flow, the Kokoro voice engine and the built console
on one port (:8055). An earlier design ran two processes (`cli.py serve` plus a
separate `neural_tts_server.py` on :8880, with a `bpo-voice.service` unit and a
`deploy/docker-entrypoint.sh`); those files are gone and these tests used to
assert them, so the whole file was failing. It now pins the architecture the
code actually has.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

CONFIG_PY = ROOT / "agent" / "config.py"
ENV_EXAMPLE = ROOT / ".env.example"

# Environment variables the *runtime* sets for itself or inherits from the
# shell/terminal. They are not configuration and must never be in .env.example.
_ENV_NOISE = {
    "TERM", "ANSICON", "FORCE_COLOR", "WT_SESSION", "TZ", "PATH", "HOME",
    "LANG", "PYTHONPATH", "PYTHONUNBUFFERED", "PIP_NO_CACHE_DIR",
    "HF_HUB_DISABLE_XET", "NODE_ENV",
}


def _documented_env_names() -> "set[str]":
    """Every environment variable the code base reads, wherever it reads it.

    Config.load() handles almost all of them, but a few (SECRET_KEY, DEBUG,
    DATABASE_URL, the LiveKit worker's settings) are read directly by other
    modules. Scanning the whole tree means .env.example is checked against
    reality instead of against one file that is easy to outgrow.
    """
    names: "set[str]" = set()
    for path in ROOT.rglob("*.py"):
        if any(part in {".git", "node_modules", "frontend", "__pycache__",
                        "venv", ".venv"} for part in path.parts):
            continue
        source = path.read_text(errors="ignore")
        names |= set(re.findall(r'env(?:_list)?\(\s*"([A-Z0-9_]+)"', source))
        names |= set(re.findall(r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', source))
        names |= set(re.findall(r'os\.getenv\(\s*["\']([A-Z0-9_]+)["\']', source))
        names |= set(re.findall(r'os\.environ\[\s*["\']([A-Z0-9_]+)["\']', source))
    return names - _ENV_NOISE


def _example_env_names() -> "set[str]":
    """Names documented in .env.example, including commented-out ones.

    A commented `# OPENAI_API_KEY=sk-...` documents an optional setting just as
    well as an uncommented line; requiring every key to be live would mean a
    fresh copy exports a dozen empty cloud keys.
    """
    names = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        m = re.match(r"\s*#?\s*([A-Z][A-Z0-9_]*)\s*=", line)
        if m:
            names.add(m.group(1))
    return names


# ---------------------------------------------------------------------------
# .env.example — the file every deployment starts from
# ---------------------------------------------------------------------------

def test_env_example_documents_every_setting():
    """A missing line is a setting nobody can find after the install."""
    missing = _documented_env_names() - _example_env_names()
    assert not missing, f"not documented in .env.example: {sorted(missing)}"


def test_env_example_has_no_unknown_keys():
    """Keys that the code never reads are worse than missing ones — they mislead."""
    extra = _example_env_names() - _documented_env_names()
    assert not extra, f"in .env.example but read by nothing: {sorted(extra)}"


def test_env_example_configures_a_working_local_setup():
    """Copying the example must give a machine that can speak without any key."""
    from dotenv import dotenv_values

    from agent.config import Config

    values = {k: str(v).strip() for k, v in dotenv_values(ENV_EXAMPLE).items() if v}
    env = {**{k: v for k, v in os.environ.items()}, **values}
    with pytest.MonkeyPatch.context() as mp:
        for key, value in env.items():
            mp.setenv(key, value)
        cfg = Config.load()

    assert cfg.tts_provider == "kokoro", "the documented default must be the in-process keyless engine"
    assert cfg.kokoro_voice, "a default voice makes 'Test voice' work on a fresh install"
    assert cfg.port and cfg.host
    assert cfg.stt_provider in {"console", "mock", "deepgram"}
    assert not cfg.web_password, "the example must not ship a dashboard password"


def test_env_example_warns_about_the_dashboard_password():
    text = ENV_EXAMPLE.read_text()
    assert "WEB_PASSWORD" in text
    assert re.search(r"password.{0,160}(reachable|networked|LAN|open)", text, re.I | re.S), \
        "the example must warn that an open dashboard is reachable by others"


def test_env_example_documents_a_standby_that_is_not_installed():
    """The standby has to be explained, or people configure failover to nothing."""
    text = ENV_EXAMPLE.read_text().lower()
    assert "standby" in text and "empty = no failover" in text


def test_env_example_requires_a_secret_key_without_a_default():
    """A shipped default secret is a shipped vulnerability."""
    text = ENV_EXAMPLE.read_text()
    line = next(ln for ln in text.splitlines() if ln.startswith("SECRET_KEY="))
    assert line.strip() == "SECRET_KEY=", f"SECRET_KEY must have no default value: {line!r}"
    assert re.search(r"SECRET_KEY", text.split("SECRET_KEY=")[0]) and "REQUIRED" in text, \
        "SECRET_KEY must be marked required"


# ---------------------------------------------------------------------------
# Launchers
# ---------------------------------------------------------------------------

LAUNCHERS = ("start.ps1", "start.bat", "start.sh")


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_starts_the_app(name):
    """Without this, a fresh install starts nothing."""
    text = (ROOT / name).read_text()
    assert "uvicorn" in text and "backend.app.main:app" in text, f"{name} does not start the app"


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_bootstraps_a_fresh_machine(name):
    """A clone with no venv, no deps and no .env must still start with one command."""
    text = (ROOT / name).read_text()
    assert "-m venv" in text, f"{name} does not create a virtual environment"
    assert "requirements.txt" in text, f"{name} does not install dependencies"
    assert ".env.example" in text and ".env" in text, f"{name} does not create .env"


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launchers_do_not_default_to_the_dev_secret(name):
    """No shipped fallback secret — an unset SECRET_KEY must stop the launch."""
    text = (ROOT / name).read_text()
    assert "dev-secret" not in text, f"{name} still hardcodes a fallback secret"


def test_start_sh_is_valid_and_executable():
    path = ROOT / "start.sh"
    assert os.access(path, os.X_OK), "start.sh must be executable"
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_start_sh_supports_the_documented_flags():
    text = (ROOT / "start.sh").read_text()
    for flag in ("--serve", "--check", "--tests", "--port", "--no-voice"):
        assert flag in text, f"start.sh does not accept {flag}"
    header = text.split("set -e")[0]
    for flag in ("--serve", "--check", "--tests", "--port", "--no-voice"):
        assert flag in header, f"{flag} is accepted but not documented in the usage header"


def test_launchers_agree_on_the_port():
    """The launcher, .env.example and Dockerfile must agree, or the link lies."""
    assert re.search(r"^PORT=8055\s*$", ENV_EXAMPLE.read_text(), re.M)
    for name in LAUNCHERS:
        assert "8055" in (ROOT / name).read_text(), f"{name} does not use port 8055"
    for stale in ("8880", "8000"):
        for name in LAUNCHERS:
            assert stale not in (ROOT / name).read_text(), \
                f"{name} still mentions the retired port {stale}"


# ---------------------------------------------------------------------------
# systemd unit
# ---------------------------------------------------------------------------

def test_systemd_units_are_complete():
    app = (ROOT / "deploy" / "bpo-adriene.service").read_text()

    assert "ExecStart=" in app and "uvicorn backend.app.main:app" in app
    assert "Restart=always" in app and "WantedBy=multi-user.target" in app
    assert "[Service]" in app and "[Unit]" in app and "[Install]" in app
    assert "EnvironmentFile=-" in app, "the unit must read .env (optional, so it can start without)"
    assert "WorkingDirectory=/opt/bpo-adriene" in app


def test_systemd_units_agree_with_the_documented_paths():
    dep = (ROOT / "DEPLOYMENT.md").read_text()
    assert "deploy/bpo-adriene.service" in dep, "the unit is not mentioned in DEPLOYMENT.md"
    assert "/opt/bpo-adriene" in dep


# ---------------------------------------------------------------------------
# Containers — one image, one process, one port
# ---------------------------------------------------------------------------

def test_dockerfile_runs_the_single_process_and_is_healthy():
    text = (ROOT / "Dockerfile").read_text()
    assert "EXPOSE 8055" in text
    assert "HEALTHCHECK" in text, "a deployment image needs a healthcheck"
    assert "/api/health" in text, "the healthcheck must hit the real liveness route"
    assert "uvicorn" in text and "backend.app.main:app" in text
    # requirements first: rebuilt images must reuse the dependency layer
    assert text.index("COPY backend/requirements.txt") < text.index("COPY backend/ ./backend/")
    for stale in ("8880", "neural_tts_server.py"):
        assert stale not in text, f"the image still references the retired two-process design ({stale})"


def test_compose_reads_env_and_persists_data():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "env_file" in text and ".env" in text, "compose must load .env"
    assert "bot-data:/app/data" in text, "recordings and the voice library must survive a rebuild"
    assert "8055:8055" in text


# requirements — what a fresh install actually gets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("package", ["piper-tts", "onnxruntime", "soundfile", "fastapi",
                                      "uvicorn", "pytest"])
def test_requirements_cover_a_working_deployment(package):
    text = (ROOT / "requirements.txt").read_text().lower()
    assert package in text, f"{package} is missing from requirements.txt"


def test_requirements_install_everything_the_readiness_advice_tells_you_to_run():
    """The report ends with "verify: python -m pytest tests/ -q".

    A fresh clone used to install without pytest, so following that advice on a
    brand-new server failed with "No module named pytest" — caught by installing
    a clean clone end to end.
    """
    req = (ROOT / "requirements.txt").read_text().lower()
    assert "pytest" in req
    for launcher in ("start.sh", "start.ps1", "DEPLOYMENT.md", "README.md"):
        text = (ROOT / launcher).read_text(errors="ignore")
        if "pytest" in text:
            assert "pytest" in req, f"{launcher} tells the operator to run pytest"


# ---------------------------------------------------------------------------
# The readiness report itself
# ---------------------------------------------------------------------------

def _run_healthcheck(env_extra: "dict[str, str]") -> dict:
    env = {**os.environ, **env_extra}
    env.pop("ELEVENLABS_API_KEY", None)
    env.pop("DEEPGRAM_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "scripts/healthcheck.py", "--offline", "--json"],
        cwd=ROOT, capture_output=True, text=True, env=env, timeout=180)
    assert result.stdout.strip(), result.stderr
    return json.loads(result.stdout)


def test_healthcheck_is_ready_with_the_local_engine_and_no_keys():
    """A keyless local deployment is a *valid* deployment, not a broken one."""
    report = _run_healthcheck({"TTS_PROVIDER": "piper", "STT_PROVIDER": "console",
                               "TTS_STANDBY_PROVIDER": ""})
    models = list((ROOT / "data" / "models" / "piper").glob("*.onnx"))
    if models:
        assert report["ready"], report["failures"]
    else:
        # no voice models on this machine: the only failure may be about fetching them
        assert all("fetch_neural_voices" in f["detail"] for f in report["failures"]), \
            report["failures"]
    assert not any("ElevenLabs key" in f["label"] for f in report["failures"]), \
        "an empty local setup must not be failed for a missing cloud key"


def test_healthcheck_fails_when_the_selected_cloud_engine_has_no_key():
    report = _run_healthcheck({"TTS_PROVIDER": "elevenlabs"})
    labels = [f["label"] for f in report["failures"]]
    assert any("ElevenLabs" in label for label in labels), report
    assert not report["ready"]


def test_healthcheck_flags_live_calls_without_speech_to_text():
    report = _run_healthcheck({"TTS_PROVIDER": "piper", "STT_PROVIDER": "console"})
    notes = " ".join(n["label"] + n["detail"] for n in report["notes"])
    assert "deepgram" in notes.lower()


def test_healthcheck_measures_real_audio():
    """The check that proves a caller would hear something must not be fooled."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import healthcheck as hc

    import io
    import struct
    import wave

    def wav(samples: "list[int]", rate: int = 16000) -> bytes:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(struct.pack("<%dh" % len(samples), *samples))
        return buf.getvalue()

    seconds, peak, rms = hc._audio_stats(wav([10000] * 16000))
    assert 0.9 < seconds < 1.1 and peak == 10000 and rms > 900
    assert hc._audio_stats(wav([0] * 16000))[1] == 0            # silence is detected
    assert hc._audio_stats(b"")[1] == 0                          # empty is detected


def test_healthcheck_voice_checks_report_a_dead_engine():
    """A stopped voice server must be a FAIL with the command that fixes it."""
    sys.path.insert(0, str(ROOT / "scripts"))
    import healthcheck as hc

    from agent.config import Config

    cfg = Config.load()
    cfg.tts_provider = "piper"
    cfg.tts_standby_provider = ""
    cfg.piper_base_url = "http://127.0.0.1:1"          # nothing listens there
    hc._results.clear()
    hc.check_voices(cfg, offline=False)
    failures = [r for r in hc._results if r[0] == hc.FAIL]
    assert any("voice server" in label for label, _, _ in
               [(r[1], r[2], "") for r in failures]), hc._results
    detail = " ".join(r[2] for r in failures)
    assert "neural_tts_server.py" in detail, "the failure must say how to start the engine"


# ---------------------------------------------------------------------------
# Documentation an operator is told to read
# ---------------------------------------------------------------------------

def test_deployment_guide_covers_the_essentials():
    text = (ROOT / "DEPLOYMENT.md").read_text()
    for topic in ("setup_local.py", "start.ps1", "start.sh", "systemctl",
                  "docker compose", "healthcheck.py", "WEB_PASSWORD", "DEEPGRAM_API_KEY",
                  "Troubleshooting", "Backup"):
        assert topic in text, f"DEPLOYMENT.md does not cover {topic}"


def test_readme_points_at_the_deployment_guide():
    readme = (ROOT / "README.md").read_text()
    assert "DEPLOYMENT.md" in readme
