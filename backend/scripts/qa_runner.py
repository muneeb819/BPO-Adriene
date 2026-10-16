#!/usr/bin/env python3
"""QA runner: run one or all deterministic scenarios and produce consolidated CSV results.

Usage:
  # Run a single scenario
  python backend/scripts/qa_runner.py --scenario docs/scenarios/cooperative_caller.json

  # Run all scenarios under docs/scenarios/ and produce consolidated CSV
  python backend/scripts/qa_runner.py --all

Outputs:
  - Per-scenario JSON report written to backend/qa_outputs/report_{scenario_id}.json
  - Per-turn audio files written to backend/qa_outputs/{scenario_id}_turn_{n}_agent.wav (if pyttsx3 available)
  - Consolidated CSV: backend/qa_outputs/results_summary.csv when --all is used

This runner is intentionally self-contained and mock-first; it simulates STT/LLM/TTS steps
for deterministic scenario files and is suitable for local QA preview.
"""

import argparse
import json
import os
import time
import random
import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
import glob

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

BASE = Path(__file__).resolve().parents[1]
OUTPUT_DIR = BASE / 'qa_outputs'
SCENARIO_DIR = BASE.parent / 'docs' / 'scenarios'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# Per-file TTS budget; a stuck engine is abandoned instead of stalling the run.
TTS_TIMEOUT = float(os.getenv('QA_TTS_TIMEOUT', '30'))


def _iso(ts: float = None) -> str:
    """Format a UTC timestamp (or now) as ISO-8601 with a trailing 'Z'."""
    dt = datetime.fromtimestamp(ts, timezone.utc) if ts is not None else datetime.now(timezone.utc)
    return dt.replace(tzinfo=None).isoformat() + 'Z'


def synth_tts(text: str, out_file: Path):
    """Synthesize `text` into `out_file`, returning the path on success else None.

    A fresh engine is created per file: reusing a single pyttsx3 engine across
    save_to_file()/runAndWait() calls can deadlock on Windows (SAPI). The work
    also runs on a daemon thread with a timeout so a wedged engine can never
    hang the whole scenario run.
    """
    if pyttsx3 is None:
        return None

    def _speak():
        engine = pyttsx3.init()
        engine.save_to_file(text, str(out_file))
        engine.runAndWait()
        engine.stop()

    worker = threading.Thread(target=_speak, daemon=True)
    worker.start()
    worker.join(TTS_TIMEOUT)
    if worker.is_alive():
        print(f"[qa_runner] TTS timed out writing {out_file.name}; continuing without audio")
        return None
    return str(out_file) if out_file.exists() else None


def fake_llm_response(user_text: str, context: dict) -> str:
    """Deterministic, rule-based agent response for simple QA scenarios."""
    ut = user_text.lower()
    # caller is unsure -> must clarify safely, never guess (see scenario_confused_001)
    if any(k in ut for k in ('not sure', 'unsure', "don't know", 'do not know', 'might have', 'i think')):
        return "I can clarify — Medicare is federal health coverage for people over 65. Do you have a Medicare card?"
    if 'medicare' in ut:
        return "Thank you for confirming Medicare. Are you between 45 and 80 years old?"
    # simple age detection
    for token in ut.replace("'"," ").split():
        if token.isdigit() and 18 <= int(token) <= 120:
            age = int(token)
            if 45 <= age <= 80:
                return f"Thanks — based on your age {age}, you appear eligible. I will transfer you to a Benefits Specialist."
            else:
                return f"Thanks — at age {age} you may not be eligible for this campaign. I'll mark that in the notes."
    if 'hello' in ut or 'hi' in ut:
        return f"Hello {context.get('customer_name','there')}, this is an automated benefits outreach. Do you have Medicare?"
    return "Thanks for your reply. Can you please clarify?"


def run_scenario_dict(scenario: dict, output_dir: Path) -> dict:
    """Run a scenario provided as a dictionary and return the generated report dict."""
    report = {
        'scenario_id': scenario.get('id') or scenario.get('scenario_id') or scenario.get('name'),
        'name': scenario.get('name'),
        'started_at': _iso(),
        'events': [],
        'assertions': scenario.get('assertions', {}),
    }

    variables = scenario.get('variables', {})
    utterances = scenario.get('utterances', [])

    base_time = time.time()
    turn_index = 0
    for utt in utterances:
        # wait until relative time
        target = base_time + utt.get('time', 0.0)
        now = time.time()
        wait = target - now
        if wait > 0:
            time.sleep(wait)
        # simulate user speech
        user_start = time.time()
        # assume speech duration proportional to length
        dur = max(0.2, min(3.0, len(utt.get('text','')) * 0.03))
        time.sleep(dur)
        user_end = time.time()

        # STT (mock): immediate transcript = utterance text
        stt_first_token = user_end + random.uniform(0.02, 0.08)
        stt_complete = stt_first_token + random.uniform(0.02, 0.05)

        stt_text = utt.get('text')

        # LLM
        llm_start = stt_complete + random.uniform(0.01, 0.05)
        # simulate TTFT
        llm_first_token = llm_start + random.uniform(0.05, 0.2)
        # generate response
        agent_text = fake_llm_response(stt_text, variables)
        llm_complete = llm_first_token + max(0.01, len(agent_text) * 0.002)

        # TTS
        tts_start = llm_complete + random.uniform(0.01, 0.05)
        tts_file = output_dir / f"{report['scenario_id']}_turn_{turn_index}_agent.wav"
        if synth_tts(agent_text, tts_file):
            tts_first_frame = tts_start + 0.02
            tts_end = tts_first_frame + max(0.5, len(agent_text) * 0.02)
        else:
            tts_first_frame = tts_start
            tts_end = tts_start + 0.5

        event = {
            'turn': turn_index,
            'user': {
                'text': stt_text,
                'barge_in': bool(utt.get('barge_in', False)),
                'start': _iso(user_start),
                'end': _iso(user_end),
                'stt_first_token': _iso(stt_first_token),
                'stt_complete': _iso(stt_complete),
            },
            'agent': {
                'text': agent_text,
                'llm_first_token': _iso(llm_first_token),
                'llm_complete': _iso(llm_complete),
                'tts_first_frame': _iso(tts_first_frame),
                'tts_end': _iso(tts_end),
                'tts_audio': str(tts_file) if tts_file.exists() else None,
            },
            'metrics': {
                # derived per-turn latencies (seconds) — see docs/TESTING_QA.md §5
                'stt_latency': round(stt_first_token - user_end, 3),
                'ttft': round(llm_first_token - user_end, 3),
                'tts_latency': round(tts_first_frame - llm_complete, 3),
                'end_to_first_audio': round(tts_first_frame - user_end, 3),
                'total_turn_time': round(tts_end - user_start, 3),
            },
        }
        report['events'].append(event)
        turn_index += 1

    report['completed_at'] = _iso()
    # simple assertion checks
    checks = []
    assertions = scenario.get('assertions', {})
    # must_contain
    musts = assertions.get('must_contain', [])
    for m in musts:
        found = any(m.lower() in (e['user']['text'] or '').lower() or m.lower() in (e['agent']['text'] or '').lower() for e in report['events'])
        checks.append({'assertion': f"must_contain:{m}", 'passed': bool(found)})
    # age_extraction
    expected_age = assertions.get('age_extraction')
    if expected_age:
        age_found = False
        for e in report['events']:
            import re
            mm = re.search(r"\b(\d{1,3})\b", e['user']['text'])
            if mm and int(mm.group(1)) == expected_age:
                age_found = True
                break
        checks.append({'assertion': f'age_extraction:{expected_age}', 'passed': age_found})
    # transfer_requested: agent must have proposed a transfer to a human
    if assertions.get('transfer_requested'):
        proposed = any('transfer' in (e['agent']['text'] or '').lower() for e in report['events'])
        checks.append({'assertion': 'transfer_requested', 'passed': proposed})
    # clarification_requested: agent must have asked a clarifying question
    if assertions.get('clarification_requested'):
        asked = any(
            'clarify' in (e['agent']['text'] or '').lower() or (e['agent']['text'] or '').strip().endswith('?')
            for e in report['events']
        )
        checks.append({'assertion': 'clarification_requested', 'passed': asked})
    # barge_in_handled: every barge-in turn must still yield a coherent agent response
    if assertions.get('barge_in_handled'):
        barged = [e for e in report['events'] if e['user'].get('barge_in')]
        handled = bool(barged) and all((e['agent']['text'] or '').strip() for e in barged)
        checks.append({'assertion': 'barge_in_handled', 'passed': handled})

    report['checks'] = checks

    # write report
    out_report = output_dir / f"report_{report['scenario_id']}.json"
    with out_report.open('w', encoding='utf-8') as f:
        json.dump(report, f, indent=2)

    return report, out_report


def run_all_scenarios(scenario_dir: Path, output_dir: Path, csv_out: Path):
    scenario_files = sorted(glob.glob(str(scenario_dir / '*.json')))
    results = []
    for sfile in scenario_files:
        print(f"Running scenario: {sfile}")
        with open(sfile, 'r', encoding='utf-8') as f:
            scenario = json.load(f)
        report, rpt_path = run_scenario_dict(scenario, output_dir)
        # summarize
        total_checks = len(report.get('checks', []))
        passed_checks = sum(1 for c in report.get('checks', []) if c.get('passed'))
        overall_pass = total_checks == passed_checks if total_checks > 0 else True
        results.append({
            'scenario_id': report.get('scenario_id'),
            'name': report.get('name'),
            'started_at': report.get('started_at'),
            'completed_at': report.get('completed_at'),
            'total_turns': len(report.get('events', [])),
            'total_checks': total_checks,
            'passed_checks': passed_checks,
            'overall_pass': overall_pass,
            'report_path': str(rpt_path)
        })

    # write CSV
    fieldnames = ['scenario_id','name','started_at','completed_at','total_turns','total_checks','passed_checks','overall_pass','report_path']
    with open(csv_out, 'w', newline='', encoding='utf-8') as csvf:
        writer = csv.DictWriter(csvf, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    print(f"All scenarios run. Summary CSV: {csv_out}")
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run QA scenarios (single or all)')
    parser.add_argument('--scenario', help='Path to scenario JSON file')
    parser.add_argument('--all', action='store_true', help='Run all scenarios under docs/scenarios')
    args = parser.parse_args()

    if args.all:
        csv_out = OUTPUT_DIR / 'results_summary.csv'
        run_all_scenarios(SCENARIO_DIR, OUTPUT_DIR, csv_out)
    elif args.scenario:
        scen_path = Path(args.scenario)
        if not scen_path.exists():
            print('Scenario file not found:', scen_path)
            exit(1)
        with scen_path.open('r', encoding='utf-8') as f:
            scenario = json.load(f)
        report, rpt_path = run_scenario_dict(scenario, OUTPUT_DIR)
        print('Report written to:', rpt_path)
    else:
        parser.print_help()
