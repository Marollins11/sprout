import os

FLASK_SECRET      = os.getenv("FLASK_SECRET",      "replace-with-a-long-random-string")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY",    "YOUR_GEMINI_API_KEY")
GOOGLE_CREDS      = "credentials.json"
OUTLOOK_CLIENT_ID = os.getenv("OUTLOOK_CLIENT_ID", "YOUR_AZURE_CLIENT_ID")
OUTLOOK_SECRET    = os.getenv("OUTLOOK_SECRET",    "YOUR_AZURE_SECRET")
OUTLOOK_TENANT    = os.getenv("OUTLOOK_TENANT",    "consumers")
CANVAS_TOKEN      = os.getenv("CANVAS_TOKEN",      "YOUR_CANVAS_API_TOKEN")
CANVAS_URL        = os.getenv("CANVAS_URL",        "https://yourschool.instructure.com")
