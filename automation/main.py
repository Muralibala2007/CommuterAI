"""Daily commute workflow: Google Routes API -> JSON report -> Telegram link."""
import datetime as dt, html, json, os, pathlib, sys, zoneinfo
import requests
from trains import next_trains

ROOT = pathlib.Path(__file__).resolve().parent.parent
IST = zoneinfo.ZoneInfo("Asia/Kolkata")
KEY = os.environ["GOOGLE_MAPS_API_KEY"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]
PAGES_URL = os.environ.get("PAGES_URL", "").rstrip("/")
FORCE = os.environ.get("FORCE") == "true"

MODES = {  # key -> (Routes API travelMode, label)
    "transit": ("TRANSIT", "Bus / Train"),
    "drive": ("DRIVE", "Car"),
    "two_wheeler": ("TWO_WHEELER", "Bike"),
}
FIELDS = ("routes.duration,routes.staticDuration,routes.distanceMeters,"
          "routes.legs.endLocation,routes.legs.steps.travelMode,"
          "routes.legs.steps.staticDuration,routes.legs.steps.transitDetails")
VEHICLES = {"BUS": "Bus", "HEAVY_RAIL": "Train", "COMMUTER_TRAIN": "Train", "TRAIN": "Train",
            "RAIL": "Train", "SUBWAY": "Metro", "METRO_RAIL": "Metro", "MONORAIL": "Monorail"}


def secs(v):
    return int(str(v).rstrip("s") or 0)


def routes_api(origin, dest, mode, depart):
    body = {"origin": {"address": origin}, "destination": {"address": dest}, "travelMode": mode,
            "departureTime": depart.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if mode == "TRANSIT":
        body["computeAlternativeRoutes"] = True
    else:
        body["routingPreference"] = "TRAFFIC_AWARE"
    r = requests.post("https://routes.googleapis.com/directions/v2:computeRoutes", json=body,
                      headers={"X-Goog-Api-Key": KEY, "X-Goog-FieldMask": FIELDS}, timeout=30)
    if r.status_code != 200:
        print(f"Routes API {mode} failed: {r.status_code} {r.text[:200]}")
        return []
    return r.json().get("routes", [])


def parse_steps(route):
    out = []
    for leg in route.get("legs", []):
        for s in leg.get("steps", []):
            if s.get("travelMode") == "TRANSIT" and "transitDetails" in s:
                td = s["transitDetails"]
                line = td.get("transitLine", {})
                kind = VEHICLES.get(line.get("vehicle", {}).get("type", ""), "Transit")
                name = line.get("nameShort") or line.get("name") or ""
                stop = td.get("stopDetails", {})
                out.append({"type": kind.lower(), "text": f"{kind} {name} from {stop.get('departureStop', {}).get('name', '?')} "
                            f"to {stop.get('arrivalStop', {}).get('name', '?')}"
                            + (f" ({td['stopCount']} stops)" if td.get("stopCount") else "")
                            + (f" toward {td['headsign']}" if td.get("headsign") else ""),
                            "depart": stop.get("departureTime", "")})
            elif s.get("travelMode") == "WALK" and secs(s.get("staticDuration", 0)) >= 120:
                out.append({"type": "walk", "text": f"Walk {round(secs(s['staticDuration']) / 60)} min", "depart": ""})
    return out


def build_options(route_cfg, depart):
    options = []
    for key in route_cfg.get("modes", list(MODES)):
        if key not in MODES:
            continue
        api_mode, label = MODES[key]
        for i, r in enumerate(routes_api(route_cfg["origin"], route_cfg["destination"], api_mode, depart)[:3]):
            mins, static = secs(r["duration"]) / 60, secs(r.get("staticDuration", r["duration"])) / 60
            options.append({"mode": key, "label": label + (f" (option {i + 1})" if key == "transit" else ""),
                            "minutes": round(mins), "no_traffic_minutes": round(static),
                            "delay_minutes": max(0, round(mins - static)) if key != "transit" else 0,
                            "km": round(r.get("distanceMeters", 0) / 1000, 1),
                            "steps": parse_steps(r) if key == "transit" else [],
                            "_end": (r.get("legs") or [{}])[-1].get("endLocation", {}).get("latLng")})
    options.sort(key=lambda o: o["minutes"])
    return options


def weather(latlng):
    try:
        j = requests.get("https://api.open-meteo.com/v1/forecast", timeout=15, params={
            "latitude": latlng["latitude"], "longitude": latlng["longitude"],
            "current": "temperature_2m,precipitation"}).json()["current"]
        return {"temp_c": j["temperature_2m"], "rain_mm": j["precipitation"]}
    except Exception:
        return None


def telegram(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", timeout=20,
                      json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"})
    r.raise_for_status()


def main():
    now = dt.datetime.now(IST)
    today = now.date().isoformat()
    cfg = json.loads((ROOT / "config/routes.json").read_text())
    state_f = ROOT / "state/sent.json"
    state = json.loads(state_f.read_text()) if state_f.exists() else {}
    sent = state.setdefault(today, [])
    report_f = ROOT / f"docs/data/reports/{today}.json"
    report = json.loads(report_f.read_text()) if report_f.exists() else {"date": today, "routes": []}
    lines = []

    for rc in cfg["routes"]:
        h, m = map(int, rc["depart_time"].split(":"))
        depart = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if not FORCE:
            if rc["id"] in sent or now.weekday() not in rc.get("days", range(7)):
                continue
            if now < depart - dt.timedelta(minutes=45):  # send ~45 min before leaving
                continue
        if depart < now + dt.timedelta(minutes=1):
            depart = now + dt.timedelta(minutes=2)  # Routes API needs a future time
        options = build_options(rc, depart)
        if not options:
            continue
        end = next((o["_end"] for o in options if o["_end"]), None)
        for o in options:
            o.pop("_end")
        trains = next_trains(rc["train"]["from"], rc["train"]["to"], now.hour * 60 + now.minute) if rc.get("train") else []
        report["routes"] = [r for r in report["routes"] if r["id"] != rc["id"]] + [{
            "id": rc["id"], "name": rc["name"], "origin": rc["origin"], "destination": rc["destination"],
            "depart_time": rc["depart_time"], "options": options, "fastest": options[0]["label"],
            "trains": trains, "train_pair": rc.get("train"), "weather": weather(end) if end else None}]
        best = options[0]
        lines.append(f"<b>{html.escape(rc['name'])}</b>\nFastest: {best['label']}, <b>{best['minutes']} min</b>"
                     + (f" (+{best['delay_minutes']} min traffic)" if best["delay_minutes"] else "")
                     + (f"\nNext locals: {', '.join(trains)}" if trains else ""))
        sent.append(rc["id"])

    if not lines:
        print("Nothing to send.")
        return
    report["generated_at"] = now.isoformat(timespec="minutes")
    report_f.write_text(json.dumps(report, indent=1))
    link = f"{PAGES_URL}/report.html?d={today}"
    telegram("Good morning. Your commute today:\n\n" + "\n\n".join(lines) + f"\n\n<a href=\"{link}\">Open full route details</a>")
    state_f.write_text(json.dumps({k: v for k, v in sorted(state.items())[-14:]}, indent=1))
    for old in sorted((ROOT / "docs/data/reports").glob("*.json"))[:-14]:
        old.unlink()  # keep two weeks of reports
    print("Sent", len(lines), "route(s).")


if __name__ == "__main__":
    sys.exit(main())
