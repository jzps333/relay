import os
from dotenv import load_dotenv

load_dotenv()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
JEV_API_KEY = os.getenv("JEV_API_KEY", "")
JEV_BASE_URL = os.getenv("JEV_BASE_URL", "")  # empty = OpenRouter Decisions API
JEV_MODEL = os.getenv("JEV_MODEL", "typesafe/jev-1.14")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
RELAY_TOKEN = os.getenv("RELAY_TOKEN", "change-me")

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
JEV_MIN_CONFIDENCE = float(os.getenv("JEV_MIN_CONFIDENCE", "0.75"))
MAX_STEPS = int(os.getenv("MAX_STEPS", "25"))
DEFAULT_DEVICE = os.getenv("DEFAULT_DEVICE", "phone")
ADB_SERIAL = os.getenv("ADB_SERIAL", "")  # empty = first device
ROUTINES_PATH = os.getenv("ROUTINES_PATH", "routines.json")
REPLY_TIMEOUT = float(os.getenv("REPLY_TIMEOUT", "60"))  # seconds Relay waits for a spoken yes/no

# --- Unlocking the emulator by voice ---
# EMULATOR_PIN: the emulator's lock-screen PIN. Typed by code over ADB, never shown to the AI.
# RELAY_PASSPHRASE: the phrase you say to let Relay unlock it, e.g. "open sesame".
EMULATOR_PIN = os.getenv("EMULATOR_PIN", "")
RELAY_PASSPHRASE = os.getenv("RELAY_PASSPHRASE", "")

# Fast, cheap model for quick decisions (routing to skills, planning).
CLAUDE_FAST_MODEL = os.getenv("CLAUDE_FAST_MODEL", "claude-haiku-5-5")
# Screenshots are shrunk to this width before going to the AI (smaller = faster upload).
SCREENSHOT_WIDTH = int(os.getenv("SCREENSHOT_WIDTH", "540"))
