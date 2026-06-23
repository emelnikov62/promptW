import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
SUPPORT_BOT_TOKEN = os.getenv("SUPPORT_BOT_TOKEN", "")
SUPPORT_AGENT_IDS = {int(x) for x in os.getenv("SUPPORT_AGENT_IDS", "").replace(" ", "").split(",") if x}
