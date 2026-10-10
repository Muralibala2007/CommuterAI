"""Best-effort Mumbai local timetable lookup from trainhelp.in.

The page layout is not an official API and may change, so this never raises:
on any failure it returns [] and the report still has Google Maps train legs.
"""
import re
import requests
from bs4 import BeautifulSoup

URL = "https://www.trainhelp.in/mumbai-local-train-time-table/"
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]([0-5]\d)\b")


def next_trains(from_stn, to_stn, now_minutes, limit=3):
    try:
        html = requests.get(URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text
        soup = BeautifulSoup(html, "html.parser")
        found = set()
        for row in soup.select("tr"):
            text = row.get_text(" ", strip=True)
            low = text.lower()
            if from_stn.lower() in low and to_stn.lower() in low:
                for h, m in TIME_RE.findall(text):
                    found.add(int(h) * 60 + int(m))
        upcoming = sorted(t for t in found if t >= now_minutes)[:limit]
        return [f"{t // 60:02d}:{t % 60:02d}" for t in upcoming]
    except Exception as exc:  # network, parsing, anything
        print("trainhelp lookup failed:", exc)
        return []
