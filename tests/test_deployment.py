"""Deployment readiness: the files an operator relies on must stay correct.

These tests are about *deployment*, not features — they fail when the launchers,
the installer, the service units, the container, the readiness report or the
documented configuration drift away from the code. That drift is exactly what
breaks an install on someone else's machine.
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


def _documented_env_names() -> "set[str]":
    """Every environment variable `Config.load()` reads."""
    source = CONFIG_PY.read_text()
    return set(re.findall(r'env(?:_list)?\(\s*"([A-Z0-9_]+)"', source))


def _example_env_names() -> "set[str]":
    names = set()
    for line in ENV_EXAMPLE.read_text().splitlines():
        m = re.match(r"\s*([A-Z][A-Z0-9_]*)\s*=", line)
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
    # DEBUG is read directly by the app, not through Config.
    allowed = {"DEBUG"}
    assert not (extra - allowed), f"in .env.example but read by nothing: {sorted(extra - allowed)}"


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

    assert cfg.tts_provider == "piper", "the documented default must be the keyless local engine"
    assert cfg.piper_voice, "a default voice makes 'Test voice' work on a fresh install"
    assert cfg.port and cfg.host
    assert cfg.stt_provider in {"console", "deepgram"}


def test_env_example_warns_about_the_dashboard_password():
    text = ENV_EXAMPLE.read_text()
    assert "WEB_PASSWORD" in text
    assert re.search(r"password.{0,120}(reachable|networked|LAN)", text, re.I | re.S), \
        "the example must say when a password is required"


def test_env_example_documents_a_standby_that_is_not_installed():
    """The standby has to be explained, or people configure failover to nothing."""
    text = ENV_EXAMPLE.read_text().lower()
    assert "standby" in text and "empty = no failover" in text


# ---------------------------------------------------------------------------
# Launchers
# ---------------------------------------------------------------------------

LAUNCHERS = ("start.ps1", "start.bat", "start.sh")


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_starts_the_voice_engine(name):
    """Without this, a fresh install has a dashboard and no audio."""
    text = (ROOT / name).read_text()
    assert "neural_tts_server.py" in text, f"{name} does not start the voice server"
    assert "cli.py" in text, f"{name} does not start the app"


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_bootstraps_a_fresh_machine(name):
    text = (ROOT / name).read_text()
    assert "-m venv" in text, f"{name} does not create a virtual environment"
    assert "requirements.txt" in text, f"{name} does not install dependencies"
    assert ".env.example" in text and ".env" in text, f"{name} does not create .env"


def test_start_sh_is_valid_and_executable():
    path = ROOT / "start.sh"
    assert os.access(path, os.X_OK), "start.sh must be executable"
    result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_start_sh_supports_the_documented_flags():
    text = (ROOT / "start.sh").read_text()
    for flag in ("--serve", "--check", "--tests", "--no-voice", "--port", "--voice-port"):
        assert flag in text, f"start.sh does not accept {flag}"


def test_launchers_do_not_hardcode_a_stale_port():
    """The launcher and .env must agree, or the dashboard link lies."""
    example = ENV_EXAMPLE.read_text()
    assert re.search(r"^PORT=8000\s*$", example, re.M)
    assert "8880" in (ROOT / "start.sh").read_text()
    assert "8880" in (ROOT / "start.ps1").read_text()


# ---------------------------------------------------------------------------
# systemd units
# ---------------------------------------------------------------------------

def test_systemd_units_are_complete():
    voice = (ROOT / "deploy" / "bpo-voice.service").read_text()
    app = (ROOT / "deploy" / "bpo-adriene.service").read_text()

    assert "ExecStart=" in voice and "neural_tts_server.py" in voice
    assert "--warm" in voice, "the unit should pre-load the voices"
    assert "Restart=always" in voice and "WantedBy=multi-user.target" in voice
    assert "[Service]" in voice and "[Unit]" in voice and "[Install]" in voice

    assert "ExecStart=" in app and "cli.py serve" in app
    assert "Restart=always" in app
    assert "EnvironmentFile=-" in app, "the unit must read .env (optional, so it can start without)"
    assert "Requires=bpo-voice.service" in app, "the app depends on the voice engine"
    assert "After=" in app


def test_systemd_units_agree_with_the_documented_paths():
    dep = (ROOT / "DEPLOYMENT.md").read_text()
    for unit in ("deploy/bpo-voice.service", "deploy/bpo-adriene.service"):
        assert unit in dep, f"{unit} is not mentioned in DEPLOYMENT.md"
    assert "/opt/bpo-adriene" in dep


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

def test_dockerfile_runs_both_processes_and_is_healthy():
    text = (ROOT / "Dockerfile").read_text()
    assert "EXPOSE 8000" in text and "8880" in text
    assert "docker-entrypoint.sh" in text
    assert "HEALTHCHECK" in text, "a deployment image needs a healthcheck"
    # requirements first: rebuilt images must reuse the dependency layer
    assert text.index("COPY requirements.txt") < text.index("COPY . .")
    assert "libsndfile1" in text, "soundfile needs libsndfile to decode uploads"


def test_entrypoint_starts_voice_then_app():
    path = ROOT / "deploy" / "docker-entrypoint.sh"
    assert os.access(path, os.X_OK), "the entrypoint must be executable"
    text = path.read_text()
    assert "neural_tts_server.py" in text and "--warm" in text
    assert text.index("neural_tts_server.py") < text.index("cli.py serve")
    assert text.index("exec python cli.py serve") > 0
    assert "fetch_neural_voices.py" in text
    check = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_compose_mounts_data_and_reads_env():
    text = (ROOT / "docker-compose.yml").read_text()
    assert "env_file" in text and ".env" in text
    assert "./data:/app/data" in text, "recordings and the voice library must survive a rebuild"
    assert "8000:8000" in text


# ---------------------------------------------------------------------------
# requirements — what a fresh install actually gets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("package", ["piper-tts", "onnxruntime", "soundfile", "fastapi", "uvicorn"])
def test_requirements_cover_a_working_deployment(package):
    text = (ROOT / "requirements.txt").read_text().lower()
    assert package in text, f"{package} is missing from requirements.txt"


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
