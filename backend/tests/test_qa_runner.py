"""Tests for the offline QA runner and its report/audio artifacts."""
from __future__ import annotations

import csv
import json
import wave
from pathlib import Path

from backend.scripts import qa_runner


class SilentTestSynthesizer:
    """Small deterministic WAV writer so tests do not need a system TTS engine."""

    mode = "silent-fallback"
    warnings = ["test audio uses silence"]

    def synthesize(self, text: str, path: Path) -> qa_runner.AudioResult:
        duration = qa_runner._write_silent_wav(path, text)
        return qa_runner.AudioResult(path.name, self.mode, False, duration, self.warnings[0])


def test_all_scenarios_run_offline_and_write_reports(tmp_path, capsys):
    exit_code = qa_runner.main([
        "--all",
        "--no-audio",
        "--output-dir",
        str(tmp_path),
    ])

    assert exit_code == 0
    assert "16/16 passed" in capsys.readouterr().out
    assert len(list(tmp_path.glob("*.json"))) == len(qa_runner.SCENARIOS)
    assert (tmp_path / "results_summary.csv").is_file()

    expected_ids = {scenario.id for scenario in qa_runner.SCENARIOS}
    actual_ids = {
        json.loads(path.read_text(encoding="utf-8"))["scenario_id"]
        for path in tmp_path.glob("*.json")
    }
    assert actual_ids == expected_ids

    with (tmp_path / "results_summary.csv").open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == len(qa_runner.SCENARIOS)
    assert {row["status"] for row in rows} == {"passed"}
    assert {row["audio_mode"] for row in rows} == {"disabled"}
    assert not list(tmp_path.glob("*.wav"))


def test_audio_turns_are_wav_artifacts_and_require_audio_fails_on_silence(tmp_path):
    scenario = qa_runner.SCENARIO_BY_ID["medicare-eligible-age-67"]
    report = qa_runner.run_scenario(
        scenario,
        tmp_path,
        SilentTestSynthesizer(),
        require_audio=True,
    )

    assert report["status"] == "failed"
    assert report["audio"]["clip_count"] == 4
    assert report["audio"]["spoken_clips"] == 0
    assert any(check["name"] == "spoken_audio_available" and not check["passed"]
               for check in report["checks"])

    filenames = [artifact["filename"] for artifact in report["artifacts"]]
    assert filenames == [
        f"{scenario.id}_turn_{index}_agent.wav" for index in range(4)
    ]
    for filename in filenames:
        with wave.open(str(tmp_path / filename), "rb") as audio:
            assert audio.getnchannels() == 1
            assert audio.getsampwidth() == 2
            assert audio.getframerate() == 16000
            assert audio.getnframes() > 0


def test_silent_wav_fallback_is_valid_and_has_a_reasonable_duration(tmp_path):
    path = tmp_path / "silent.wav"
    duration = qa_runner._write_silent_wav(path, "A short line for QA.")

    assert path.is_file()
    assert duration > 0
    assert qa_runner._inspect_wav(path) == duration


def test_unknown_scenario_id_is_rejected_by_cli(tmp_path):
    try:
        qa_runner.main(["--scenario", "not-a-real-scenario", "--no-audio", "--output-dir", str(tmp_path)])
    except SystemExit as exc:
        assert exc.code == 2
    else:  # pragma: no cover - protects the CLI contract
        raise AssertionError("unknown scenario should be a command-line error")
