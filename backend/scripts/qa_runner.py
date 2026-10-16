#!/usr/bin/env python3
"""Run deterministic offline QA scenarios against the voice conversation engine.

From the repository root:
    python backend/scripts/qa_runner.py --all

Or from backend/:
    python scripts/qa_runner.py --all

The runner does not call the configured LLM, TTS providers, PBX, or any remote
service. Agent lines use the campaign's compliance-safe scripted fallbacks.
Optional WAV speech synthesis uses pyttsx3; if it or a local speech engine is
unavailable, the runner writes valid silent WAV placeholders and records that
fact in each report. Install pyttsx3 for spoken clips:
    pip install pyttsx3
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import struct
import sys
import time
import traceback
import wave
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Add the repository root so the script works both as a file and from backend/.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.campaign import DEFAULTS  # noqa: E402
from agent.config import Config  # noqa: E402
from agent.conversation import Conversation  # noqa: E402


@dataclass(frozen=True)
class Scenario:
    """A scripted customer conversation and its expected call outcome."""

    id: str
    name: str
    campaign_id: str
    customer_turns: tuple[str, ...]
    expected_disposition: str
    expected_answers: dict[str, str] = field(default_factory=dict)
    expected_age: int | None = None
    check_age: bool = False
    required_agent_phrases: tuple[str, ...] = ()


# The first customer line is a response to the greeting. Subsequent lines answer
# the campaign's verification questions in order; unclear replies intentionally
# exercise the conversation engine's clarification path.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        id="medicare-eligible-age-67",
        name="Medicare: eligible caller transfers",
        campaign_id="medicare",
        customer_turns=("I'm doing well, thank you.", "Yes, I currently have Medicare.", "I'm 67 years old."),
        expected_disposition="AI-TRANSFER",
        expected_answers={"medicare": "YES", "age": "PASS"},
        expected_age=67,
        check_age=True,
        required_agent_phrases=("Do you currently have Medicare", "How old are you", "licensed agent"),
    ),
    Scenario(
        id="medicare-no-coverage",
        name="Medicare: caller does not have coverage",
        campaign_id="medicare",
        customer_turns=("I'm well, thanks.", "No, I don't have Medicare."),
        expected_disposition="AI-NO-MEDICARE",
        expected_answers={"medicare": "NO"},
        required_agent_phrases=("thank you for your time",),
    ),
    Scenario(
        id="medicare-underage",
        name="Medicare: caller is below the age range",
        campaign_id="medicare",
        customer_turns=("I'm fine, thank you.", "Yes, I have Medicare.", "I'm 44."),
        expected_disposition="AI-NOT-ELIGIBLE",
        expected_answers={"medicare": "YES", "age": "FAIL"},
        expected_age=44,
        check_age=True,
        required_agent_phrases=("ages 45 to 90",),
    ),
    Scenario(
        id="medicare-age-boundary-45",
        name="Medicare: lower age boundary is eligible",
        campaign_id="medicare",
        customer_turns=("Doing well.", "Yes, I have Medicare.", "I'm 45."),
        expected_disposition="AI-TRANSFER",
        expected_answers={"medicare": "YES", "age": "PASS"},
        expected_age=45,
        check_age=True,
        required_agent_phrases=("licensed agent",),
    ),
    Scenario(
        id="medicare-age-boundary-90",
        name="Medicare: upper age boundary is eligible",
        campaign_id="medicare",
        customer_turns=("Doing well.", "Yes, I have Medicare.", "I'm 90."),
        expected_disposition="AI-TRANSFER",
        expected_answers={"medicare": "YES", "age": "PASS"},
        expected_age=90,
        check_age=True,
        required_agent_phrases=("licensed agent",),
    ),
    Scenario(
        id="medicare-clarify-and-recover",
        name="Medicare: unclear answer is clarified and recovered",
        campaign_id="medicare",
        customer_turns=("I'm fine.", "Maybe, I'm not sure.", "Yes, I have Medicare.", "I'm 67."),
        expected_disposition="AI-TRANSFER",
        expected_answers={"medicare": "YES", "age": "PASS"},
        expected_age=67,
        check_age=True,
        required_agent_phrases=("Just to confirm", "How old are you", "licensed agent"),
    ),
    Scenario(
        id="medicare-clarify-exhausted",
        name="Medicare: repeated uncertainty closes as unclear",
        campaign_id="medicare",
        customer_turns=("I'm fine.", "Maybe.", "I'm still not sure.", "I can't remember."),
        expected_disposition="AI-UNCLEAR",
        expected_answers={"medicare": "UNSURE"},
        required_agent_phrases=("Just to confirm",),
    ),
    Scenario(
        id="medicare-dnc",
        name="Medicare: do-not-call request is honored immediately",
        campaign_id="medicare",
        customer_turns=("Please stop calling and remove my number.",),
        expected_disposition="AI-DNC",
    ),
    Scenario(
        id="medicare-busy",
        name="Medicare: busy caller is allowed to end the call",
        campaign_id="medicare",
        customer_turns=("I'm busy right now; please call me back later.",),
        expected_disposition="AI-BUSY",
        required_agent_phrases=("No problem",),
    ),
    Scenario(
        id="medicare-refusal",
        name="Medicare: refusal to answer is respected",
        campaign_id="medicare",
        customer_turns=("I'm fine.", "I don't want to discuss that."),
        expected_disposition="AI-MEDICARE-REFUSED",
        expected_answers={"medicare": "REFUSE"},
        required_agent_phrases=("thank you for your time",),
    ),
    Scenario(
        id="final-expense-qualified",
        name="Final expense: interested caller in age range transfers",
        campaign_id="final_expense",
        customer_turns=("I'm doing well.", "Yes, I'm interested.", "I'm 67 years old."),
        expected_disposition="AI-TRANSFER",
        expected_answers={"interest": "YES", "age": "PASS"},
        expected_age=67,
        check_age=True,
        required_agent_phrases=("How old are you", "licensed agent"),
    ),
    Scenario(
        id="final-expense-not-interested",
        name="Final expense: caller not interested is closed",
        campaign_id="final_expense",
        customer_turns=("I'm okay.", "No thanks, I'm not interested."),
        expected_disposition="AI-NOT-INTERESTED",
        expected_answers={"interest": "NO"},
        required_agent_phrases=("thank you for your time",),
    ),
    Scenario(
        id="aca-uninsured-qualified",
        name="ACA: uninsured caller in age range transfers",
        campaign_id="aca",
        customer_turns=("I'm well, thanks.", "No, I don't have health insurance.", "I'm 35."),
        expected_disposition="AI-TRANSFER",
        expected_answers={"insured": "NO", "age": "PASS"},
        expected_age=35,
        check_age=True,
        required_agent_phrases=("health insurance", "licensed agent"),
    ),
    Scenario(
        id="aca-already-insured",
        name="ACA: caller with existing insurance is screened out",
        campaign_id="aca",
        customer_turns=("I'm fine.", "Yes, I have health insurance."),
        expected_disposition="AI-HAS-INSURANCE",
        expected_answers={"insured": "YES"},
        required_agent_phrases=("thank you",),
    ),
    Scenario(
        id="hospital-indemnity-qualified",
        name="Hospital indemnity: eligible caller without recent stay transfers",
        campaign_id="hi",
        customer_turns=("I'm doing well.", "I'm 60 years old.", "No, I haven't had a hospital stay."),
        expected_disposition="AI-TRANSFER",
        expected_answers={"age": "PASS", "hospital_stay": "NO"},
        expected_age=60,
        check_age=True,
        required_agent_phrases=("hospital stays", "licensed agent"),
    ),
    Scenario(
        id="hospital-indemnity-recent-stay",
        name="Hospital indemnity: recent hospital stay is screened out",
        campaign_id="hi",
        customer_turns=("I'm doing well.", "I'm 60.", "Yes, I was hospitalized last year."),
        expected_disposition="AI-RECENT-HOSPITAL",
        expected_answers={"age": "PASS", "hospital_stay": "YES"},
        expected_age=60,
        check_age=True,
        required_agent_phrases=("thank you for your time",),
    ),
)

SCENARIO_BY_ID = {scenario.id: scenario for scenario in SCENARIOS}
SUMMARY_FIELDS = (
    "scenario_id",
    "scenario_name",
    "campaign_id",
    "status",
    "expected_disposition",
    "actual_disposition",
    "expected_age",
    "actual_age",
    "customer_turns",
    "agent_turns",
    "audio_clips",
    "audio_mode",
    "report_file",
    "error",
)


class _OfflineLLM:
    """LLM double that prevents network access and forces scripted fallback lines."""

    def models_available(self) -> bool:
        return False

    def chat(self, *_args: Any, **_kwargs: Any) -> str:
        return ""

    def chat_json(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"intent": "UNCLEAR", "confidence": 0.0}


class _NoopMemory:
    """Keep QA runs from reading or writing the developer's call-memory file."""

    def few_shot(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        return []

    def record(self, *_args: Any, **_kwargs: Any) -> None:
        return None


@dataclass
class AudioResult:
    filename: str | None
    engine: str
    spoken: bool
    duration_seconds: float | None = None
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.filename,
            "engine": self.engine,
            "spoken": self.spoken,
            "duration_seconds": self.duration_seconds,
            "warning": self.warning,
        }


def _inspect_wav(path: Path) -> float:
    """Validate a WAV artifact and return its duration in seconds."""
    with wave.open(str(path), "rb") as audio:
        frames = audio.getnframes()
        rate = audio.getframerate()
        if frames <= 0 or rate <= 0:
            raise ValueError(f"empty or invalid WAV: {path.name}")
        if audio.getcomptype() != "NONE":
            raise ValueError(f"unsupported WAV compression in {path.name}")
        return frames / float(rate)


def _write_silent_wav(path: Path, text: str, sample_rate: int = 16000) -> float:
    """Write a valid PCM WAV placeholder when no offline speech engine is usable."""
    words = max(1, len((text or "").split()))
    duration = max(0.25, min(12.0, words / 2.7))
    frames = max(1, int(sample_rate * duration))
    duration = frames / float(sample_rate)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.stem}.tmp{path.suffix}")
    try:
        with wave.open(str(tmp_path), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            audio.writeframes(struct.pack("<h", 0) * frames)
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return duration


class Pyttsx3AudioSynthesizer:
    """Best-effort local speech synthesis with a readable WAV fallback."""

    def __init__(self, *, voice: str | None = None, rate: int = 170, sample_rate: int = 16000):
        self.engine: Any | None = None
        self.rate = rate
        self.sample_rate = sample_rate
        self.warnings: list[str] = []
        self.mode = "pyttsx3"
        try:
            import pyttsx3

            self.engine = pyttsx3.init()
            self.engine.setProperty("rate", rate)
            if voice:
                self.engine.setProperty("voice", voice)
        except Exception as exc:  # missing package or platform speech driver
            self.engine = None
            self.mode = "silent-fallback"
            self.warnings.append(self._warning(exc))

    @staticmethod
    def _warning(exc: BaseException) -> str:
        message = " ".join(str(exc).split())
        if len(message) > 300:
            message = message[:297] + "..."
        return f"Local speech synthesis unavailable: {type(exc).__name__}: {message}"

    def synthesize(self, text: str, path: Path) -> AudioResult:
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.engine is not None:
            tmp_path = path.with_name(f".{path.stem}.tmp{path.suffix}")
            try:
                self.engine.save_to_file(text, str(tmp_path))
                self.engine.runAndWait()
                duration = _inspect_wav(tmp_path)
                tmp_path.replace(path)
                return AudioResult(path.name, "pyttsx3", True, duration)
            except Exception as exc:  # fall back, but keep the scenario runnable
                try:
                    self.engine.stop()
                except Exception:
                    pass
                self.engine = None
                self.mode = "silent-fallback"
                warning = self._warning(exc)
                self.warnings.append(warning)
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                duration = _write_silent_wav(path, text, self.sample_rate)
                return AudioResult(path.name, "silent-fallback", False, duration, warning)

        duration = _write_silent_wav(path, text, self.sample_rate)
        warning = self.warnings[0] if self.warnings else "pyttsx3 is unavailable"
        return AudioResult(path.name, "silent-fallback", False, duration, warning)


class NoAudioSynthesizer:
    """No-op synthesizer used with --no-audio."""

    mode = "disabled"
    warnings: list[str] = []

    def synthesize(self, _text: str, _path: Path) -> AudioResult:
        return AudioResult(None, "disabled", False)


def _new_conversation(campaign_id: str) -> Conversation:
    """Build a fresh conversation without touching campaign or memory stores."""
    factory = DEFAULTS.get(campaign_id)
    if factory is None:
        raise ValueError(f"unknown campaign: {campaign_id}")

    # Conversation imports get_memory directly, so patch its constructor-time
    # lookup. The object is intentionally a no-op for the lifetime of this call.
    from unittest.mock import patch

    with patch("agent.conversation.get_memory", return_value=_NoopMemory()):
        return Conversation(Config(), _OfflineLLM(), campaign=factory())


def _check(name: str, passed: bool, expected: Any, actual: Any) -> dict[str, Any]:
    return {
        "name": name,
        "passed": bool(passed),
        "expected": expected,
        "actual": actual,
    }


def _safe_scenario_filename(scenario_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", scenario_id).strip("-_")
    if not safe:
        raise ValueError(f"scenario id is not a safe filename: {scenario_id!r}")
    return safe


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_old_scenario_audio(output_dir: Path, scenario_id: str) -> None:
    """Delete stale clips for this scenario, not other QA output files."""
    prefix = f"{_safe_scenario_filename(scenario_id)}_turn_"
    for old_file in output_dir.glob(f"{prefix}*_agent.wav"):
        try:
            old_file.unlink()
        except OSError:
            pass


def run_scenario(
    scenario: Scenario,
    output_dir: Path,
    synthesizer: Pyttsx3AudioSynthesizer | NoAudioSynthesizer,
    *,
    require_audio: bool = False,
) -> dict[str, Any]:
    """Execute one scenario, save all newly spoken agent clips, and return a report."""
    started = time.perf_counter()
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scenario_stem = _safe_scenario_filename(scenario.id)
    _remove_old_scenario_audio(output_dir, scenario_stem)

    report: dict[str, Any] = {
        "schema_version": 1,
        "scenario_id": scenario.id,
        "scenario_name": scenario.name,
        "campaign_id": scenario.campaign_id,
        "generated_at": generated_at,
        "status": "failed",
        "passed": False,
        "expected_disposition": scenario.expected_disposition,
        "actual_disposition": None,
        "ended": False,
        "expected_age": scenario.expected_age if scenario.check_age else None,
        "actual_age": None,
        "answers": {},
        "checks": [],
        "turns": [],
        "artifacts": [],
        "audio": {
            "requested": not isinstance(synthesizer, NoAudioSynthesizer),
            "required": require_audio,
            "engine": getattr(synthesizer, "mode", "unknown"),
            "spoken_clips": 0,
            "clip_count": 0,
            "warnings": [],
        },
        "errors": [],
    }

    try:
        conversation = _new_conversation(scenario.campaign_id)
        agent_turn_index = 0
        customer_turns_processed = 0
        audio_events: list[dict[str, Any]] = []

        def add_agent_utterance(text: str) -> None:
            nonlocal agent_turn_index
            event: dict[str, Any] = {
                "turn_index": len(report["turns"]),
                "speaker": "agent",
                "role": "ai",
                "text": text,
                "agent_turn_index": agent_turn_index,
            }
            filename = f"{scenario_stem}_turn_{agent_turn_index}_agent.wav"
            artifact_path = output_dir / filename
            audio = synthesizer.synthesize(text, artifact_path)
            event["audio"] = audio.as_dict()
            if audio.filename:
                artifact = {
                    "type": "audio/wav",
                    "kind": "agent_audio",
                    "filename": audio.filename,
                    "turn_index": agent_turn_index,
                    "spoken": audio.spoken,
                    "duration_seconds": audio.duration_seconds,
                }
                report["artifacts"].append(artifact)
                audio_events.append(artifact)
            report["turns"].append(event)
            agent_turn_index += 1

        greeting = conversation.start()
        if greeting:
            add_agent_utterance(greeting)

        for customer_text in scenario.customer_turns:
            report["turns"].append({
                "turn_index": len(report["turns"]),
                "speaker": "customer",
                "role": "customer",
                "text": customer_text,
            })
            customer_turns_processed += 1
            beat = conversation.hear(customer_text)
            if beat.ai_speech:
                add_agent_utterance(beat.ai_speech)
            if beat.end_call:
                break

        report["actual_disposition"] = conversation.ctx.disposition or None
        report["ended"] = bool(conversation.ended)
        report["actual_age"] = conversation.ctx.age
        report["answers"] = dict(conversation.ctx.answers)

        checks = [
            _check("call_ended", True, True, report["ended"]),
            _check(
                "expected_disposition",
                report["actual_disposition"] == scenario.expected_disposition,
                scenario.expected_disposition,
                report["actual_disposition"],
            ),
            _check(
                "customer_turns_consumed",
                customer_turns_processed == len(scenario.customer_turns),
                len(scenario.customer_turns),
                customer_turns_processed,
            ),
        ]
        for question_id, expected_verdict in scenario.expected_answers.items():
            actual_verdict = report["answers"].get(question_id)
            checks.append(_check(
                f"answer:{question_id}",
                actual_verdict == expected_verdict,
                expected_verdict,
                actual_verdict,
            ))
        if scenario.check_age:
            checks.append(_check(
                "age",
                report["actual_age"] == scenario.expected_age,
                scenario.expected_age,
                report["actual_age"],
            ))

        agent_text = "\n".join(
            turn["text"] for turn in report["turns"] if turn["speaker"] == "agent"
        ).casefold()
        missing_phrases = [
            phrase for phrase in scenario.required_agent_phrases
            if phrase.casefold() not in agent_text
        ]
        checks.append(_check(
            "required_agent_phrases",
            not missing_phrases,
            list(scenario.required_agent_phrases),
            {"missing": missing_phrases},
        ))

        if not isinstance(synthesizer, NoAudioSynthesizer):
            wav_errors: list[str] = []
            for artifact in audio_events:
                wav_path = output_dir / artifact["filename"]
                try:
                    _inspect_wav(wav_path)
                except (OSError, ValueError, wave.Error) as exc:
                    wav_errors.append(f"{artifact['filename']}: {exc}")
            checks.append(_check(
                "wav_artifacts_valid",
                not wav_errors and len(audio_events) == agent_turn_index,
                agent_turn_index,
                {"valid": len(audio_events) - len(wav_errors), "errors": wav_errors},
            ))
            if require_audio:
                silent_clips = [a["filename"] for a in audio_events if not a["spoken"]]
                checks.append(_check(
                    "spoken_audio_available",
                    not silent_clips and len(audio_events) == agent_turn_index,
                    "spoken WAV for each agent turn",
                    {"silent_or_missing": silent_clips, "agent_turns": agent_turn_index},
                ))

        report["checks"] = checks
        report["passed"] = all(check["passed"] for check in checks)
        report["status"] = "passed" if report["passed"] else "failed"
        report["audio"]["engine"] = getattr(synthesizer, "mode", "unknown")
        report["audio"]["clip_count"] = len(audio_events)
        report["audio"]["spoken_clips"] = sum(1 for item in audio_events if item["spoken"])
        report["audio"]["warnings"] = list(dict.fromkeys(getattr(synthesizer, "warnings", [])))
    except Exception as exc:  # one broken scenario must not prevent the suite running
        report["errors"].append({
            "type": type(exc).__name__,
            "message": str(exc),
        })
        report["traceback"] = traceback.format_exc()
        report["status"] = "failed"
        report["passed"] = False
        report["audio"]["engine"] = getattr(synthesizer, "mode", "unknown")
        report["audio"]["clip_count"] = len(report["artifacts"])
        report["audio"]["spoken_clips"] = sum(
            1 for artifact in report["artifacts"] if artifact.get("spoken")
        )
        report["audio"]["warnings"] = list(dict.fromkeys(getattr(synthesizer, "warnings", [])))

    report["duration_seconds"] = round(time.perf_counter() - started, 3)
    report["audio"]["engine"] = getattr(synthesizer, "mode", "unknown")
    return report


def _summary_row(report: dict[str, Any], report_file: str) -> dict[str, Any]:
    errors = report.get("errors", [])
    error = "; ".join(item.get("message", "") for item in errors)
    return {
        "scenario_id": report["scenario_id"],
        "scenario_name": report["scenario_name"],
        "campaign_id": report["campaign_id"],
        "status": report["status"],
        "expected_disposition": report["expected_disposition"],
        "actual_disposition": report.get("actual_disposition") or "",
        "expected_age": report.get("expected_age") if report.get("expected_age") is not None else "",
        "actual_age": report.get("actual_age") if report.get("actual_age") is not None else "",
        "customer_turns": sum(1 for turn in report.get("turns", []) if turn.get("speaker") == "customer"),
        "agent_turns": sum(1 for turn in report.get("turns", []) if turn.get("speaker") == "agent"),
        "audio_clips": report.get("audio", {}).get("clip_count", 0),
        "audio_mode": report.get("audio", {}).get("engine", "unknown"),
        "report_file": report_file,
        "error": error,
    }


def _write_summary_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def run_suite(
    scenarios: Iterable[Scenario],
    output_dir: Path,
    synthesizer: Pyttsx3AudioSynthesizer | NoAudioSynthesizer,
    *,
    require_audio: bool = False,
) -> list[dict[str, Any]]:
    """Run selected scenarios and write one JSON report per scenario + CSV."""
    output_dir.mkdir(parents=True, exist_ok=True)
    reports: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        stem = _safe_scenario_filename(scenario.id)
        report = run_scenario(scenario, output_dir, synthesizer, require_audio=require_audio)
        report_file = f"{stem}.json"
        _write_json(output_dir / report_file, report)
        reports.append(report)
        rows.append(_summary_row(report, report_file))
    _write_summary_csv(output_dir / "results_summary.csv", rows)
    return reports


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run offline QA scenarios for the BPO Adriene conversation engine.",
        epilog=(
            "Example: python backend/scripts/qa_runner.py --all\n"
            "Install pyttsx3 for spoken WAV output; without it, valid silent WAV placeholders are written."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--all", action="store_true", help="run the complete built-in QA scenario suite")
    selection.add_argument(
        "--scenario",
        action="append",
        metavar="ID",
        help="run one scenario by ID (repeat to run several; see --list)",
    )
    parser.add_argument("--list", action="store_true", help="list available scenarios and exit")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "qa_outputs",
        help="artifact directory (default: backend/qa_outputs)",
    )
    audio_group = parser.add_mutually_exclusive_group()
    audio_group.add_argument("--no-audio", action="store_true", help="skip WAV generation")
    audio_group.add_argument(
        "--require-audio",
        action="store_true",
        help="fail QA checks unless every agent turn has spoken (non-silent) audio",
    )
    parser.add_argument("--voice", help="optional local pyttsx3 voice ID")
    parser.add_argument("--rate", type=int, default=170, help="pyttsx3 speech rate (default: 170)")
    parser.add_argument(
        "--sample-rate",
        type=int,
        default=16000,
        help="sample rate for silent fallback WAVs (default: 16000 Hz)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list:
        for scenario in SCENARIOS:
            print(f"{scenario.id:36} {scenario.campaign_id:14} {scenario.name}")
        return 0
    if not args.all and not args.scenario:
        parser.error("choose --all or at least one --scenario (or use --list)")
    if args.rate <= 0:
        parser.error("--rate must be a positive integer")
    if args.sample_rate < 8000 or args.sample_rate > 96000:
        parser.error("--sample-rate must be between 8000 and 96000 Hz")

    if args.all:
        selected = list(SCENARIOS)
    else:
        unknown = [scenario_id for scenario_id in args.scenario if scenario_id not in SCENARIO_BY_ID]
        if unknown:
            parser.error(f"unknown scenario ID(s): {', '.join(unknown)}; use --list to see valid IDs")
        selected = [SCENARIO_BY_ID[scenario_id] for scenario_id in dict.fromkeys(args.scenario)]

    output_dir = args.output_dir.expanduser().resolve()
    synthesizer: Pyttsx3AudioSynthesizer | NoAudioSynthesizer
    if args.no_audio:
        synthesizer = NoAudioSynthesizer()
    else:
        synthesizer = Pyttsx3AudioSynthesizer(
            voice=args.voice,
            rate=args.rate,
            sample_rate=args.sample_rate,
        )

    print(f"Running {len(selected)} QA scenario(s)...")
    reports = run_suite(selected, output_dir, synthesizer, require_audio=args.require_audio)
    for report in reports:
        label = "PASS" if report["passed"] else "FAIL"
        print(
            f"[{label}] {report['scenario_id']}: "
            f"expected {report['expected_disposition']}, got {report.get('actual_disposition') or 'no disposition'}"
        )
        for error in report.get("errors", []):
            print(f"       {error['type']}: {error['message']}")

    passed = sum(1 for report in reports if report["passed"])
    failed = len(reports) - passed
    print(f"\nQA result: {passed}/{len(reports)} passed, {failed} failed")
    print(f"Reports and artifacts: {output_dir}")
    print(f"CSV summary: {output_dir / 'results_summary.csv'}")
    if getattr(synthesizer, "warnings", None):
        print("Audio warning: " + synthesizer.warnings[0])
        print("Silent WAV placeholders were written; install/configure pyttsx3 for spoken audio.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
