import os

API_KEY    = os.environ.get("BFX_API_KEY", "")
API_SECRET = os.environ.get("BFX_API_SECRET", "")
BASE_URL   = os.environ.get("BFX_BASE_URL", "https://api.bitfinex.com")
SYMBOLS    = os.environ.get("BFX_SYMBOLS", "UST").split(",")
