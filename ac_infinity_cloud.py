"""Poll AC Infinity UIS Cloud for live controller readings.

The cloud route exists for controllers connected to Wi-Fi in the AC Infinity
app. It needs no Bluetooth adapter and no proximity, so it works from anywhere.
It only works on Wi-Fi-capable controllers:

    Controller 69 WiFi, 69 Pro, 69 Pro+, AI+

Bluetooth-only controllers (Controller 67, base Controller 69) never reach the
UIS Cloud and cannot be read this way. Use ac_infinity_scan.py for those.

The API is undocumented and reverse engineered. It has no OAuth: the account
e-mail and password are sent on every login. Use a throwaway password if that
is a concern, and never commit credentials.

Environment:
    ACINF_EMAIL       account e-mail
    ACINF_PASSWORD    account password (truncated to 25 chars, as the app does)

Usage:
    python3 ac_infinity_cloud.py                     # one reading, then exit
    python3 ac_infinity_cloud.py --interval 60       # poll every 60s, forever
    python3 ac_infinity_cloud.py --interval 60 --duration 600
    python3 ac_infinity_cloud.py --device "Tent"     # filter by name, devId or MAC
    python3 ac_infinity_cloud.py --raw               # dump the raw payload, then exit

Output is one JSON object per controller per poll on stdout. The keys match
ac_infinity_scan.py, so either route can feed the same downstream code.

Exit codes:
    0  reading captured
    1  logged in, but no matching controller was returned
    2  usage or configuration error (missing env vars)
    3  login rejected (bad e-mail or password)
    4  network or API failure
"""

import argparse
import json
import os
import sys
import time

import requests

HOST = "https://www.acinfinityserver.com"
API_URL_LOGIN = "/api/user/appUserLogin"
API_URL_GET_DEVICE_INFO_LIST_ALL = "/api/user/devInfoListAll"

USER_AGENT = "okhttp/4.12.0"
MAX_PASSWORD_LENGTH = 25
DEFAULT_TIMEOUT = 15

# devType values from the UIS Cloud. A few overlap with the BLE type ids.
MODEL_NAMES = {
    1: "Controller 67",
    6: "AirTap T4",
    7: "Controller 69",
    9: "Controller 69 Pro",
    11: "Controller 69 Pro",
    12: "Controller 69 Pro+",
    18: "Controller 69 Pro+",
    20: "Controller AI+",
    21: "UIS Outlet AI",
    22: "UIS Outlet AI+",
}


class ACInfinityError(Exception):
    """Base for cloud failures that map onto a CLI exit code."""

    exit_code = 4


class ConfigError(ACInfinityError):
    exit_code = 2


class AuthError(ACInfinityError):
    exit_code = 3


class ApiError(ACInfinityError):
    exit_code = 4


def model_name(dev_type):
    if dev_type is None:
        return "Unknown"
    return MODEL_NAMES.get(dev_type, f"Type {dev_type}")


def scaled(value):
    """The cloud reports temperature, humidity and VPD in hundredths."""
    if value is None:
        return None
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def parse_controller(controller):
    """Turn one controller object from devInfoListAll into a reading.

    Returns None for anything that is not a dict. Missing fields become None
    rather than raising, because the API adds and drops keys between firmwares.
    """
    if not isinstance(controller, dict):
        return None

    info = controller.get("deviceInfo") or {}
    dev_type = controller.get("devType")
    ports = info.get("ports") or []

    payload = {
        "address": controller.get("devMacAddr"),
        "name": controller.get("devName"),
        "rssi": None,
        "source": "cloud",
        "model": model_name(dev_type),
        "type_id": dev_type,
        "version": controller.get("devVersion"),
        "unit": "C",
        "temperature": scaled(info.get("temperature")),
        "humidity": scaled(info.get("humidity")),
        "vpd": scaled(info.get("vpdnums")),
        "online": bool(controller.get("online") or info.get("online")),
    }

    port_readings = []
    for port in ports:
        if not isinstance(port, dict):
            continue
        on = bool(port.get("loadState"))
        port_readings.append({
            "port": port.get("port"),
            "name": port.get("portName"),
            "state": "ON" if on else "OFF",
            "speed": port.get("speak"),
            "remaining": port.get("remainTime"),
        })

    if port_readings:
        payload["ports"] = port_readings
        active = [p["speed"] or 0 for p in port_readings if p["state"] == "ON"]
        payload["fan_speed"] = max(active) if active else 0
        payload["fan_state"] = "ON" if active else "OFF"

    return payload


def select(devices, needle):
    """Filter controllers by devId, name substring or MAC address."""
    needle = str(needle).lower() if needle else None
    matches = []
    for device in devices:
        if not isinstance(device, dict):
            continue
        if needle is None:
            matches.append(device)
            continue
        fields = (
            str(device.get("devId") or ""),
            str(device.get("devName") or ""),
            str(device.get("devMacAddr") or ""),
        )
        if any(needle in field.lower() for field in fields):
            matches.append(device)
    return matches


def emit(payload):
    print(json.dumps(payload, sort_keys=True), flush=True)


class CloudClient:
    """Thin wrapper over the two endpoints needed for read-only polling."""

    def __init__(self, email, password, host=HOST, session=None, timeout=DEFAULT_TIMEOUT):
        self.email = email
        # The API rejects passwords longer than 25 characters; the mobile app
        # truncates them, so anything else would fail to log in.
        self.password = (password or "")[:MAX_PASSWORD_LENGTH]
        self.host = host.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.user_id = None

    def _headers(self, authenticated):
        headers = {"User-Agent": USER_AGENT}
        if authenticated:
            headers["token"] = self.user_id
        return headers

    def _post(self, path, data, headers):
        url = self.host + path
        try:
            response = self.session.post(url, data=data, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ApiError(f"could not reach {url}: {exc}") from exc

        if response.status_code != 200:
            raise ApiError(f"{path} returned HTTP {response.status_code}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ApiError(f"{path} returned a non-JSON response") from exc

        code = body.get("code")
        if code != 200:
            message = body.get("msg") or f"code {code}"
            if path == API_URL_LOGIN:
                raise AuthError(f"login rejected: {message}")
            raise ApiError(f"{path} failed: {message}")
        return body

    def login(self):
        body = self._post(
            API_URL_LOGIN,
            {"appEmail": self.email, "appPasswordl": self.password},
            self._headers(authenticated=False),
        )
        self.user_id = (body.get("data") or {}).get("appId")
        if not self.user_id:
            raise AuthError("login succeeded but no appId was returned")
        return self.user_id

    def devices(self):
        if not self.user_id:
            raise ApiError("login() must be called before devices()")
        body = self._post(
            API_URL_GET_DEVICE_INFO_LIST_ALL,
            {"userId": self.user_id},
            self._headers(authenticated=True),
        )
        return body.get("data") or []


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--interval", type=float, help="seconds between polls (omit for a single reading)")
    parser.add_argument("--duration", type=float, help="stop after this many seconds (needs --interval)")
    parser.add_argument("--device", help="only this controller: devId, name substring, or MAC")
    parser.add_argument("--raw", action="store_true", help="print the raw API payload and exit")
    args = parser.parse_args(argv)

    email = os.environ.get("ACINF_EMAIL")
    password = os.environ.get("ACINF_PASSWORD")
    if not email or not password:
        print("set ACINF_EMAIL and ACINF_PASSWORD in the environment", file=sys.stderr)
        return ConfigError.exit_code

    client = CloudClient(email, password)
    try:
        client.login()
    except ACInfinityError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code

    deadline = None if args.duration is None else time.monotonic() + args.duration

    while True:
        if deadline is not None and time.monotonic() >= deadline:
            break

        try:
            devices = client.devices()
        except ACInfinityError as exc:
            print(str(exc), file=sys.stderr)
            return exc.exit_code

        if args.raw:
            print(json.dumps(devices, indent=2, sort_keys=True))
            return 0

        selected = select(devices, args.device)
        for controller in selected:
            payload = parse_controller(controller)
            if payload is not None:
                emit(payload)

        if not selected:
            if args.device:
                print(f"no controller matched {args.device!r}", file=sys.stderr)
            else:
                print("account returned no controllers", file=sys.stderr)

        if args.interval is None:
            return 0 if selected else 1

        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(args.interval, remaining))
        else:
            time.sleep(args.interval)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
