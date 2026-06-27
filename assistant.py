# assistant.py  —  runs on Pi 1 only
# pyaudio, webrtcvad, and whisper are Pi-specific (installed in Part B).
# google-generativeai is installed on your computer in Step 4 and again on the Pi in Phase 7.
from google import genai
import wave, pyaudio, whisper, webrtcvad
import pyttsx3, schedule, time, threading, requests, collections
from datetime import datetime
from config import GEMINI_API_KEY

_client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM = """You are a voice-first personal desk assistant.
Respond with EXACTLY ONE structured reply or a plain conversational answer.
ADD TASK:       TASK|title|family|sub-project
MOVE STATUS:    MOVE_STATUS|partial title|new_status  (todo/in_progress/done)
REASSIGN:       MOVE_PROJECT|partial title|family|new sub-project
FLAG/UNFLAG:    FLAG|partial title|1 or 0
FILTER BOARD:   FILTER|family|sub-project  (use "all" to clear)
SET REMINDER:   REMINDER|HH:MM|task description
CANVAS SYNC:    CANVAS_SYNC
For anything else reply conversationally in 2-3 sentences.
Infer the family from context: work=professional, school=courses, personal=everything else.
Keep replies short — they are spoken aloud.
"""

gemini_model = genai.GenerativeModel(
    model_name="gemini-2.5-pro",
    system_instruction=SYSTEM
)

tts   = pyttsx3.init()
asr   = whisper.load_model("tiny")
FLASK = "http://localhost:5000"

WAKE_WORD     = "sprout"
SAMPLE_RATE   = 16000
CHUNK_MS      = 30        # webrtcvad works in 10/20/30ms frames
CHUNK_SAMPLES = int(SAMPLE_RATE * CHUNK_MS / 1000)
CHUNK_BYTES   = CHUNK_SAMPLES * 2  # 16-bit = 2 bytes per sample
VAD_MODE      = 3         # 0=least aggressive, 3=most aggressive


def speak(text):
    print(f"Assistant: {text}")
    tts.say(text)
    tts.runAndWait()


def record_until_silence(stream, vad, min_speech_chunks=8, silence_chunks=30):
    """Record audio using VAD — starts capturing on speech, stops after silence."""
    ring = collections.deque(maxlen=silence_chunks)
    frames = []
    triggered = False
    speech_count = 0

    while True:
        chunk = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
        is_speech = vad.is_speech(chunk, SAMPLE_RATE)
        ring.append(is_speech)

        if not triggered:
            if is_speech:
                speech_count += 1
            if speech_count >= min_speech_chunks:
                triggered = True
                frames.append(chunk)
        else:
            frames.append(chunk)
            if not any(ring):
                break

    return b"".join(frames)


def save_and_transcribe(audio_bytes):
    with wave.open("/tmp/speech.wav", "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_bytes)
    return asr.transcribe("/tmp/speech.wav")["text"].strip()


def wait_for_wake_word(stream, vad):
    """Listen in short bursts with VAD; return True when wake word detected."""
    ring = collections.deque(maxlen=10)
    frames = []

    while True:
        chunk = stream.read(CHUNK_SAMPLES, exception_on_overflow=False)
        is_speech = vad.is_speech(chunk, SAMPLE_RATE)
        ring.append(is_speech)

        if is_speech:
            frames.append(chunk)
        else:
            if frames and not any(ring):
                # burst of speech just ended — check for wake word
                audio = b"".join(frames)
                frames = []
                with wave.open("/tmp/wake.wav", "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(audio)
                result = asr.transcribe("/tmp/wake.wav")["text"].lower()
                if WAKE_WORD in result:
                    return True


def listen(stream, vad):
    audio = record_until_silence(stream, vad)
    return save_and_transcribe(audio)


def find_task(match):
    for t in requests.get(f"{FLASK}/api/tasks").json():
        if match.lower() in t["title"].lower():
            return t
    return None


def handle_reply(reply):
    if reply.startswith("TASK|"):
        _, title, family, project = reply.split("|", 3)
        requests.post(f"{FLASK}/api/tasks",
                      json={"title": title, "family": family, "project": project})
        return f"Added {title} to {project}."

    if reply.startswith("MOVE_STATUS|"):
        _, match, status = reply.split("|", 2)
        task = find_task(match)
        if not task:
            return "I could not find that task."
        requests.patch(f"{FLASK}/api/tasks/{task['id']}", json={"status": status})
        return f"Moved to {status}."

    if reply.startswith("MOVE_PROJECT|"):
        _, match, family, project = reply.split("|", 3)
        task = find_task(match)
        if not task:
            return "I could not find that task."
        requests.patch(f"{FLASK}/api/tasks/{task['id']}",
                       json={"project": project, "family": family})
        return f"Moved to {project}."

    if reply.startswith("FLAG|"):
        _, match, flagged = reply.split("|", 2)
        task = find_task(match)
        if not task:
            return "I could not find that task."
        requests.patch(f"{FLASK}/api/tasks/{task['id']}", json={"flagged": int(flagged)})
        return "Flagged." if flagged == "1" else "Flag removed."

    if reply.startswith("FILTER|"):
        _, family, sub = reply.split("|", 2)
        return "Showing all tasks." if family == "all" else f"Filtering to {sub}."

    if reply.startswith("REMINDER|"):
        _, t, task = reply.split("|", 2)
        requests.post(f"{FLASK}/api/reminders", json={"time": t, "task": task})
        return f"Reminder set for {t}."

    if reply.strip() == "CANVAS_SYNC":
        requests.post(f"{FLASK}/api/canvas_sync")
        return "Syncing Canvas now."

    return reply


def reminder_loop():
    import sqlite3

    def check():
        now = datetime.now().strftime("%H:%M")
        try:
            db = sqlite3.connect("tasks.db")
            for rid, task in db.execute(
                "SELECT id,task FROM reminders WHERE time=? AND fired=0", (now,)
            ).fetchall():
                speak(f"Reminder: {task}")
                db.execute("UPDATE reminders SET fired=1 WHERE id=?", (rid,))
                db.commit()
        except Exception:
            pass

    schedule.every(1).minutes.do(check)
    while True:
        schedule.run_pending()
        time.sleep(10)


def main():
    vad = webrtcvad.Vad(VAD_MODE)
    pa  = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16, channels=1, rate=SAMPLE_RATE,
        input=True, frames_per_buffer=CHUNK_SAMPLES
    )
    chat = gemini_model.start_chat(history=[])
    threading.Thread(target=reminder_loop, daemon=True).start()
    speak("Desk assistant ready. Say Sprout to wake me.")

    while True:
        wait_for_wake_word(stream, vad)
        speak("Yes?")
        text = listen(stream, vad)
        if not text:
            continue
        print(f"You: {text}")
        response = chat.send_message(text)
        speak(handle_reply(response.text))


if __name__ == "__main__":
    main()
