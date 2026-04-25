"""
Local development aircraft simulator.

Writes a readsb-compatible aircraft.json to a file at POLL_INTERVAL_SEC
intervals. Use this to test the collector without a live readsb feed.

Usage:
    python sim.py                        # writes to /tmp/rsbs_sim.json
    python sim.py /tmp/my_aircraft.json  # custom path

Then run the collector pointed at the same file:
    RSBS_AIRCRAFT_JSON=/tmp/rsbs_sim.json RSBS_DB_PATH=./db/history.db python collector.py
"""

import json
import math
import random
import sys
import time

OUTPUT_PATH = sys.argv[1] if len(sys.argv) > 1 else "/tmp/rsbs_sim.json"
INTERVAL = 5  # seconds between writes

# Receiver location (example coordinates — override via RSBS_LAT/RSBS_LON in production)
RX_LAT = 52.24199
RX_LON = 21.02872

# Simulated aircraft: (icao_hex, callsign, registration, type, category)
AIRCRAFT_DEFS = [
    ("3c4b26", "DLH123",  "D-AIBF", "A320", "A3"),
    ("4b1816", "SWR451",  "HB-JCA", "B77W", "A5"),
    ("484b58", "LOT231",  "SP-LNB", "E170", "A2"),
    ("400f4d", "RYR7412", "EI-DPI", "B738", "A3"),
    ("3944ef", "WZZ3301", "HA-LYG", "A320", "A3"),
    ("4ca7e5", "EIN334",  "EI-DVM", "A320", "A3"),
    ("471f85", "AUA263",  "OE-LBH", "B763", "A4"),
    ("50174c", "BTI571",  "YL-BBX", "AT75", "A2"),
]

# Each aircraft orbits at a different radius and speed
def _make_state(seed: int):
    rng = random.Random(seed)
    return {
        "radius_nm": rng.uniform(30, 380),    # distance from receiver
        "bearing0":  rng.uniform(0, 360),     # starting bearing (degrees)
        "speed_dps": rng.uniform(0.05, 0.3),  # degrees per second (angular speed)
        "alt_ft":    rng.choice([3000, 8000, 15000, 25000, 33000, 37000, 40000]),
        "gs_kts":    rng.randint(180, 480),
        "squawk":    f"{rng.randint(1000, 7776):04d}",
        "rssi":      rng.uniform(-25.0, -5.0),
        "source":    rng.choice(["adsb_icao", "adsb_icao", "adsb_icao", "mlat"]),
    }


STATES = {defn[0]: _make_state(i) for i, defn in enumerate(AIRCRAFT_DEFS)}


def _bearing_to_latlon(lat0: float, lon0: float, bearing_deg: float, dist_nm: float):
    """Simple flat-earth approximation for small distances."""
    bearing_rad = math.radians(bearing_deg)
    # 1 nm ≈ 1/60 degree latitude
    dlat = math.cos(bearing_rad) * dist_nm / 60.0
    # longitude degrees per nm shrinks with latitude
    dlon = math.sin(bearing_rad) * dist_nm / (60.0 * math.cos(math.radians(lat0)))
    return lat0 + dlat, lon0 + dlon


def _build_aircraft_list(now: float) -> list:
    result = []
    for defn, state in zip(AIRCRAFT_DEFS, STATES.values()):
        icao, callsign, reg, atype, cat = defn
        bearing = (state["bearing0"] + now * state["speed_dps"]) % 360
        lat, lon = _bearing_to_latlon(RX_LAT, RX_LON, bearing, state["radius_nm"])
        track = (bearing + 90) % 360  # rough heading: tangent to orbit
        result.append({
            "hex":      icao,
            "type":     state["source"],
            "flight":   callsign,
            "r":        reg,
            "t":        atype,
            "category": cat,
            "lat":      round(lat, 5),
            "lon":      round(lon, 5),
            "alt_baro": state["alt_ft"],
            "alt_geom": state["alt_ft"] + random.randint(-200, 200),
            "gs":       state["gs_kts"] + random.randint(-10, 10),
            "track":    round(track, 1),
            "baro_rate": random.choice([0, 64, 128, -64, -128]),
            "squawk":   state["squawk"],
            "messages": random.randint(100, 9999),
            "rssi":     round(state["rssi"] + random.uniform(-1, 1), 1),
            "seen":     round(random.uniform(0.1, 2.0), 1),
            "seen_pos": round(random.uniform(0.1, 4.0), 1),
        })
    return result


def main():
    print(f"sim.py: writing aircraft.json to {OUTPUT_PATH} every {INTERVAL}s")
    print("Stop with Ctrl-C\n")
    while True:
        now = time.time()
        data = {
            "now":      round(now, 1),
            "messages": random.randint(50000, 200000),
            "aircraft": _build_aircraft_list(now),
        }
        tmp = OUTPUT_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        import os
        os.replace(tmp, OUTPUT_PATH)
        print(f"\r{time.strftime('%H:%M:%S')}  {len(data['aircraft'])} aircraft", end="", flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
