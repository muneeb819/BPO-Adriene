#!/usr/bin/env python3
"""QA runner: run one or all deterministic scenarios and produce consolidated CSV results.

Usage:
  # Run a single scenario
  python backend/scripts/qa_runner.py --scenario docs/scenarios/cooperative_caller.json

  # Run all scenarios under docs/scenarios/ and produce consolidated CSV
  python backend/scripts/qa_runner.py --all

Outputs:
  - Per-scenario JSON report written to backend/qa_outputs/report_{scenario_id}.json
  - Per-turn audio files written to backend/qa_outputs/turn_{n}_agent.wav (if pyttsx3 available)
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
from datetime import datetime
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

ENGINE = None
if pyttsx3:
    try:
        ENGINE = pyttsx3.init()
        # reduce rate slightly for clarity
        rate = ENGINE.getProperty('rate')
        ENGINE.setProperty('rate', int(rate * 0.95))
    except Exception:
        ENGINE = None


def synth_tts(text: str, out_file: Path):
    if ENGINE is None:
        return None
    ENGINE.save_to_file(text, str(out_file))
    ENGINE.runAndWait()
    return str(out_file)


def fake_llm_response(user_text: str, context: dict) -> str:
    """Deterministic, rule-based agent response for simple QA scenarios."""
    ut = user_text.lower()
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
        'started_at': datetime.utcnow().isoformat() + 'Z',
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
        if ENGINE:
            try:
                synth_tts(agent_text, tts_file)
                tts_first_frame = tts_start + 0.02
                tts_end = tts_first_frame + max(0.5, len(agent_text) * 0.02)
            except Exception:
                tts_first_frame = tts_start
                tts_end = tts_start + 0.5
        else:
            tts_first_frame = tts_start
            tts_end = tts_start + 0.5

        event = {
            'turn': turn_index,
            'user': {
                'text': stt_text,
                'start': datetime.utcfromtimestamp(user_start).isoformat() + 'Z',
                'end': datetime.utcfromtimestamp(user_end).isoformat() + 'Z',
                'stt_first_token': datetime.utcfromtimestamp(stt_first_token).isoformat() + 'Z',
                'stt_complete': datetime.utcfromtimestamp(stt_complete).isoformat() + 'Z',
            },
            'agent': {
                'text': agent_text,
                'llm_first_token': datetime.utcfromtimestamp(llm_first_token).isoformat() + 'Z',
                'llm_complete': datetime.utcfromtimestamp(llm_complete).isoformat() + 'Z',
                'tts_first_frame': datetime.utcfromtimestamp(tts_first_frame).isoformat() + 'Z',
                'tts_end': datetime.utcfromtimestamp(tts_end).isoformat() + 'Z',
                'tts_audio': str(tts_file) if tts_file.exists() else None,
            }
        }
        report['events'].append(event)
        turn_index += 1

    report['completed_at'] = datetime.utcnow().isoformat() + 'Z'
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
