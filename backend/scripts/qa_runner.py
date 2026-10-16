#!/usr/bin/env python3
"""Simple QA runner for deterministic scenarios (mock-first).

This runner is intentionally self-contained and does not require running the backend server.
It simulates STT/LLM/TTS steps for deterministic scenario files under docs/scenarios/*.json
and produces a structured report (JSON) plus generated per-turn TTS WAV files in outputs/.

Usage:
  python backend/scripts/qa_runner.py --scenario ../../docs/scenarios/cooperative_caller.json

Requirements (install in backend virtualenv):
  pip install pyttsx3

Note: This runner is a lightweight QA utility aimed at quick local validation and audio preview.
It is NOT a replacement for the full integrated test runner that exercises the live WebSocket
mock-call flow. Use it to preview scenario behavior, generate audio samples, and produce
structured scenario reports.
"""

import argparse
import json
import os
import time
import random
from datetime import datetime
from pathlib import Path

try:
    import pyttsx3
except Exception:
    pyttsx3 = None

OUTPUT_DIR = Path(__file__).resolve().parents[1] / 'qa_outputs'
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

ENGINE = None
if pyttsx3:
    ENGINE = pyttsx3.init()
    # reduce rate slightly for clarity
    try:
        rate = ENGINE.getProperty('rate')
        ENGINE.setProperty('rate', int(rate * 0.95))
    except Exception:
        pass


def synth_tts(text: str, out_file: Path):
    if ENGINE is None:
        return None
    ENGINE.save_to_file(text, str(out_file))
    ENGINE.runAndWait()
    return str(out_file)


def fake_llm_response(user_text: str, context: dict) -> str:
    """Produce a deterministic, rule-based agent response for simple scenarios.
    This is deliberately conservative and meant for QA preview only.
    """
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


def run_scenario(scenario_path: Path):
    with scenario_path.open('r', encoding='utf-8') as f:
        scenario = json.load(f)

    report = {
        'scenario_id': scenario.get('id'),
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
        tts_file = OUTPUT_DIR / f"turn_{turn_index}_agent.wav"
        if pyttsx3:
            synth_tts(agent_text, tts_file)
            tts_first_frame = tts_start + 0.02
            tts_end = tts_first_frame + max(0.5, len(agent_text) * 0.02)
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
        found = any(m.lower() in e['user']['text'].lower() or m.lower() in (e['agent']['text'] or '').lower() for e in report['events'])
        checks.append({'assertion': f"must_contain:{m}", 'passed': bool(found)})
    # age_extraction
    expected_age = assertions.get('age_extraction')
    if expected_age:
        age_found = False
        for e in report['events']:
            # find digit in user text
            import re
            m = re.search(r"\b(\d{1,3})\b", e['user']['text'])
            if m and int(m.group(1)) == expected_age:
                age_found = True
                break
        checks.append({'assertion': f'age_extraction:{expected_age}', 'passed': age_found})
    report['checks'] = checks

    out_report = OUTPUT_DIR / f"report_{scenario.get('id')}.json"
    with out_report.open('w', encoding='utf-8') as f:
        json.dump(report, f, indent=2)

    print(f"Scenario run complete. Report written to: {out_report}")
    return out_report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run a deterministic scenario for QA preview')
    parser.add_argument('--scenario', required=True, help='Path to scenario JSON file (relative to repo root)')
    args = parser.parse_args()
    scen_path = Path(args.scenario).resolve()
    if not scen_path.exists():
        print('Scenario file not found:', scen_path)
        exit(1)
    run_scenario(scen_path)
