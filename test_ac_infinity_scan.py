"""Tests for ac_infinity_scan's packet parsers.

Runs without a Bluetooth adapter — it feeds synthetic manufacturer payloads
straight into parse_advertisement / parse_notification.

    pip install bleak
    python3 test_ac_infinity_scan.py
"""

import io
import json
import struct
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import ac_infinity_scan as ac

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "  <- " + str(detail)))
    if not cond:
        fails.append(name)


def adv_payload(length=27, version=3, device=11, status=0x60, temp=2450, hum=5520,
                fan=7, port=2, vpd_status=0x40, vpd=105):
    """Build a synthetic AC Infinity advertisement payload."""
    d = bytearray(27)
    d[11] = version
    d[12] = device
    d[13] = status
    d[14:16] = struct.pack(">h", temp)
    d[16:18] = struct.pack(">h", hum)
    d[18] = fan
    d[19] = port
    d[20] = vpd_status
    d[21:23] = struct.pack(">h", vpd)
    return bytes(d[:length])


def note(temp=2300, hum=6000, vpd=90, fan_type=3, fan_state_bits=2, work=7,
         port=5, unit_byte=0x00):
    """Build a synthetic AC Infinity notification packet."""
    d = bytearray(18)
    d[0] = 30
    d[1] = 255
    d[6] = unit_byte
    d[7] = port
    d[8:10] = struct.pack(">h", temp)
    d[10:12] = struct.pack(">h", hum)
    d[12:14] = struct.pack(">h", vpd)
    d[14:16] = struct.pack(">h", fan_type)
    d[16] = fan_state_bits << 6
    d[17] = work
    return bytes(d)


# ---- bit helpers ----
check("get_bit MSB-index 0 set", ac.get_bit(0b10000000, 0) is False)
check("get_bit MSB-index 0 clear", ac.get_bit(0b00000000, 0) is True)
check("get_bits(0b11000000,0,2)==3", ac.get_bits(0b11000000, 0, 2) == 3)
check("get_bits(0b00110000,2,2)==3", ac.get_bits(0b00110000, 2, 2) == 3)

# ---- advertisement, full 27-byte payload ----
r = ac.parse_advertisement({ac.MANUFACTURER_ID: adv_payload()})
check("adv 27-byte returns dict", isinstance(r, dict), r)
check("adv model", r and r["model"] == "Controller 69 Pro", r and r["model"])
check("adv type_id", r and r["type_id"] == 11)
check("adv version", r and r["version"] == 3)
check("adv unit Celsius", r and r["unit"] == "C", r and r["unit"])
check("adv temperature", r and r["temperature"] == 24.5, r and r["temperature"])
check("adv humidity", r and r["humidity"] == 55.2, r and r["humidity"])
check("adv fan_speed", r and r["fan_speed"] == 7)
check("adv fan_state AUTO", r and r["fan_state"] == "AUTO", r and r["fan_state"])
check("adv port", r and r.get("port") == 2)
check("adv vpd", r and r.get("vpd") == 1.05, r and r.get("vpd"))
check("adv vpd_state", r and r.get("vpd_state") == 1)

rf = ac.parse_advertisement({ac.MANUFACTURER_ID: adv_payload(status=0x20)})
check("adv unit Fahrenheit", rf and rf["unit"] == "F", rf and rf["unit"])

rv = ac.parse_advertisement({ac.MANUFACTURER_ID: adv_payload(version=2)})
check("adv v2 has no vpd", rv is not None and "vpd" not in rv, rv)
check("adv v2 still has temp", rv and rv["temperature"] == 24.5)

ru = ac.parse_advertisement({ac.MANUFACTURER_ID: adv_payload(device=99)})
check("adv unknown model label", ru and ru["model"] == "Type 99", ru and ru["model"])

# ---- advertisement, bad input ----
check("adv missing company id -> None", ac.parse_advertisement({}) is None)
check("adv wrong company id -> None", ac.parse_advertisement({1234: adv_payload()}) is None)
check("adv wrong length -> None", ac.parse_advertisement({ac.MANUFACTURER_ID: bytes(10)}) is None)

# ---- advertisement, length boundaries ----
try:
    r17 = ac.parse_advertisement({ac.MANUFACTURER_ID: adv_payload(length=17)})
    check("adv 17-byte -> None (no IndexError)", r17 is None, r17)
except Exception as exc:
    check("adv 17-byte -> None (no IndexError)", False, f"{type(exc).__name__}: {exc}")

r19 = ac.parse_advertisement({ac.MANUFACTURER_ID: adv_payload(length=19)})
check("adv 19-byte parses base fields", r19 and r19["temperature"] == 24.5, r19)
check("adv 19-byte skips extended", r19 and "vpd" not in r19, r19)

r23 = ac.parse_advertisement({ac.MANUFACTURER_ID: adv_payload(length=23)})
check("adv 23-byte has vpd", r23 and r23.get("vpd") == 1.05, r23)

# ---- notification ----
n = ac.parse_notification(note())
check("note returns dict", isinstance(n, dict), n)
check("note unit C", n and n["unit"] == "C", n and n["unit"])
check("note temperature", n and n["temperature"] == 23.0)
check("note humidity", n and n["humidity"] == 60.0)
check("note vpd", n and n["vpd"] == 0.9)
check("note fan_type", n and n["fan_type"] == 3)
check("note fan_state AUTO", n and n["fan_state"] == "AUTO", n and n["fan_state"])
check("note work_type", n and n["work_type"] == 7, n and n["work_type"])
check("note port", n and n["port"] == 5, n and n["port"])

nf = ac.parse_notification(note(unit_byte=0x80))
check("note unit F", nf and nf["unit"] == "F", nf and nf["unit"])

check("note short -> None", ac.parse_notification(bytes(17)) is None)
check("note bad header -> None", ac.parse_notification(bytes([0, 0]) + bytes(16)) is None)

# ---- emit format ----
buf = io.StringIO()
old = sys.stdout
sys.stdout = buf
try:
    ac.emit({"temperature": 24.5}, "AA:BB", "ACI", -60)
finally:
    sys.stdout = old
payload = json.loads(buf.getvalue())
check("emit merges reading",
      payload == {"address": "AA:BB", "name": "ACI", "rssi": -60, "temperature": 24.5},
      payload)

# ---- CLI ----
script = str(HERE / "ac_infinity_scan.py")
p = subprocess.run([sys.executable, script, "--help"], capture_output=True, text=True)
check("cli --help exit 0", p.returncode == 0, p.stderr)
check("cli --help lists --connect", "--connect" in p.stdout)

p = subprocess.run([sys.executable, script, "--connect"], capture_output=True, text=True)
check("cli --connect without --address warns",
      "--connect requires --address" in p.stderr, p.stderr)
check("cli --connect without --address exits 2", p.returncode == 2, p.returncode)

# ---- exit codes, with the scanner stubbed so no hardware is needed ----
import asyncio
import contextlib


def run_main(argv):
    old = sys.argv
    sys.argv = ["ac_infinity_scan.py"] + argv
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            return asyncio.run(ac.main())
    finally:
        sys.argv = old


async def _no_reading(address, duration, once):
    return False


async def _got_reading(address, duration, once):
    return True


original_scan = ac.passive_scan
ac.passive_scan = _no_reading
check("--once with no reading exits 1", run_main(["--once"]) == 1)
ac.passive_scan = _got_reading
check("--once with reading exits 0", run_main(["--once"]) == 0)
ac.passive_scan = original_scan

print()
print("FAILURES:", len(fails), fails if fails else "")
sys.exit(1 if fails else 0)
