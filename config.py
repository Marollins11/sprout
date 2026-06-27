import os

FLASK_SECRET      = os.getenv("FLASK_SECRET",      "replace-this-with-a-long-random-string")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
GOOGLE_CREDS      = "credentials.json"
OUTLOOK_CLIENT_ID = os.getenv("OUTLOOK_CLIENT_ID")
OUTLOOK_SECRET    = os.getenv("OUTLOOK_SECRET")
OUTLOOK_TENANT    = os.getenv("OUTLOOK_TENANT",    "consumers")
CANVAS_TOKEN      = os.getenv("CANVAS_TOKEN")
CANVAS_URL        = os.getenv("CANVAS_URL",        "https://yourschool.instructure.com")
