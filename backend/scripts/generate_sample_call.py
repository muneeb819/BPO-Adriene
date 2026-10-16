import pyttsx3

"""
Generate a sample mock call audio file using pyttsx3 (offline TTS).

This script creates sample_call.wav in the current directory. It uses the system's
default TTS voice. Install dependencies with:

    pip install pyttsx3

Run:
    python generate_sample_call.py

Then play sample_call.wav with your preferred audio player.

Note: On Linux you may need additional packages (e.g., espeak) depending on your platform.
"""

SAMPLE_TEXT = (
    "Hello, my name is Adriene. I'm calling about Medicare benefits. May I ask: do you currently have Medicare? "
    "(pause) Thank you. Are you between 45 and 80 years old? "
    "That's wonderful, I have a Benefits Specialist who is waiting. Let me transfer you without any delay — here we go. "
    "Goodbye and have a nice day."
)

OUTPUT_FILE = "sample_call.wav"

if __name__ == '__main__':
    engine = pyttsx3.init()
    # Optional voice rate/volume tuning
    rate = engine.getProperty('rate')
    engine.setProperty('rate', int(rate * 0.95))
    voices = engine.getProperty('voices')
    # Try to select a voice (if multiple voices available)
    if voices:
        engine.setProperty('voice', voices[0].id)
    engine.save_to_file(SAMPLE_TEXT, OUTPUT_FILE)
    engine.runAndWait()
    print(f'Sample audio saved to {OUTPUT_FILE}')
