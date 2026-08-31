import os
import requests
import json
import time

API_KEY = os.environ.get("MART_API_KEY", "")

URL = "https://api.apimart.ai/v1/chat/completions"