import os
from dotenv import load_dotenv

load_dotenv()

MARZBAN_URL = os.getenv("MARZBAN_URL", "http://127.0.0.1:8000")
LISTEN_HOST = os.getenv("LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8001"))
DATA_DIR = os.getenv("DATA_DIR", "./data")
SECRET_KEY = os.getenv("SECRET_KEY", "changeme")
