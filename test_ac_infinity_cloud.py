"""Tests for ac_infinity_cloud's parsing, auth and CLI.

Runs entirely offline: the HTTP session is faked, so no account and no
network access are needed.

    python3 test_ac_infinity_cloud.py
"""

import contextlib
import io
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import requests

import ac_infinity_cloud as cloud

fails = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name + ("" if cond else "  <- " + str(detail)))
    if not cond:
        fails.append(name)


# --------------------------------------------------------------------------
# Fixtures shaped like the real /api/user/devInfoListAll response.
# --------------------------------------------------------------------------

def controller(**overrides):
    base = {
        "devId": "123456",
        "devName": "4x4 Tent",
        "devType": 11,
        "devMacAddr": "AA:BB:CC:DD:EE:FF",
        "devVersion": 7,
        "online": 1,
        "deviceInfo": {
            "temperature": 2417,
            "temperatureF": 7551,
            "humidity": 7200,
            "vpdnums": 83,
            "unit": 0,
            "online": 1,
            "ports": [
                {"port": 1, "portName": "Inline Fan", "speak": 7, "loadState": 1, "remainTime": None},
                {"port": 2, "portName": "Clip Fan", "speak": 0, "loadState": 0, "remainTime": None},
            ],
        },
    }
    base.update(overrides)
    return base


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no json body")
        return self._body


class FakeSession:
    """Maps API path -> response. Records every call for assertions."""

    def __init__(self, responses=None, raise_exc=None):
        self.responses = responses or {}
        self.raise_exc = raise_exc
        self.calls = []

    def post(self, url, data=None, headers=None, timeout=None):
        path = "/" + url.split("/", 3)[3]
        self.calls.append({"path": path, "data": data, "headers": headers, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        if path not in self.responses:
            raise AssertionError(f"unexpected POST {path}")
        return self.responses[path]


def login_body(app_id="user-42"):
    return {"code": 200, "msg": "success", "data": {"appId": app_id}}


def devices_body(devices):
    return {"code": 200, "msg": "success", "data": devices}


def make_client(password="hunter2", responses=None, raise_exc=None):
    session = FakeSession(responses, raise_exc=raise_exc)
    return cloud.CloudClient("grower@example.com", password, session=session), session


# --------------------------------------------------------------------------
# scaled()
# --------------------------------------------------------------------------

check("scaled(2417) == 24.17", cloud.scaled(2417) == 24.17, cloud.scaled(2417))
check("scaled(83) == 0.83", cloud.scaled(83) == 0.83, cloud.scaled(83))
check("scaled(None) is None", cloud.scaled(None) is None)
check("scaled('bad') is None", cloud.scaled("bad") is None)
check("scaled(0) == 0.0", cloud.scaled(0) == 0.0)

# --------------------------------------------------------------------------
# model_name()
# --------------------------------------------------------------------------

check("model 11 is 69 Pro", cloud.model_name(11) == "Controller 69 Pro")
check("model 18 is 69 Pro+", cloud.model_name(18) == "Controller 69 Pro+")
check("model 20 is AI+", cloud.model_name(20) == "Controller AI+")
check("model 7 is base 69", cloud.model_name(7) == "Controller 69")
check("model unknown -> Type N", cloud.model_name(99) == "Type 99")
check("model None -> Unknown", cloud.model_name(None) == "Unknown")

# --------------------------------------------------------------------------
# parse_controller()
# --------------------------------------------------------------------------

r = cloud.parse_controller(controller())
check("parse returns dict", isinstance(r, dict), r)
check("parse temperature", r["temperature"] == 24.17, r["temperature"])
check("parse humidity", r["humidity"] == 72.0, r["humidity"])
check("parse vpd", r["vpd"] == 0.83, r["vpd"])
check("parse unit is C", r["unit"] == "C", r["unit"])
check("parse address", r["address"] == "AA:BB:CC:DD:EE:FF", r["address"])
check("parse name", r["name"] == "4x4 Tent", r["name"])
check("parse rssi is None", r["rssi"] is None, r["rssi"])
check("parse source is cloud", r["source"] == "cloud", r["source"])
check("parse model", r["model"] == "Controller 69 Pro", r["model"])
check("parse type_id", r["type_id"] == 11, r["type_id"])
check("parse online", r["online"] is True, r["online"])
check("parse fan_speed is max active port", r["fan_speed"] == 7, r.get("fan_speed"))
check("parse fan_state ON", r["fan_state"] == "ON", r.get("fan_state"))
check("parse ports list", len(r["ports"]) == 2, r.get("ports"))
check("parse port name", r["ports"][0]["name"] == "Inline Fan", r["ports"][0])
check("parse port off", r["ports"][1]["state"] == "OFF", r["ports"][1])

# All ports off -> fan off at speed 0.
r_off = cloud.parse_controller(controller(deviceInfo={
    "temperature": 2000, "humidity": 5000, "vpdnums": 100,
    "ports": [{"port": 1, "portName": "Fan", "speak": 5, "loadState": 0}],
}))
check("all ports off -> fan_state OFF", r_off["fan_state"] == "OFF", r_off.get("fan_state"))
check("all ports off -> fan_speed 0", r_off["fan_speed"] == 0, r_off.get("fan_speed"))

# No ports at all -> no fan keys invented.
r_noports = cloud.parse_controller(controller(deviceInfo={"temperature": 2000, "humidity": 5000}))
check("no ports -> no fan_speed key", "fan_speed" not in r_noports, r_noports)
check("no ports -> no fan_state key", "fan_state" not in r_noports, r_noports)

# Offline controller.
r_offline = cloud.parse_controller(controller(online=0, deviceInfo={"online": 0}))
check("offline -> online False", r_offline["online"] is False, r_offline["online"])
check("offline -> missing readings None", r_offline["temperature"] is None, r_offline["temperature"])

# Missing / malformed payloads must not raise.
r_missing = cloud.parse_controller({"devId": "1", "devName": "Bare"})
check("missing deviceInfo -> dict", isinstance(r_missing, dict), r_missing)
check("missing deviceInfo -> temperature None", r_missing["temperature"] is None)
check("missing deviceInfo -> model Unknown", r_missing["model"] == "Unknown")
check("non-dict -> None", cloud.parse_controller(None) is None)
check("non-dict string -> None", cloud.parse_controller("nope") is None)
check("empty dict -> dict", isinstance(cloud.parse_controller({}), dict))

# --------------------------------------------------------------------------
# select()
# --------------------------------------------------------------------------

devices = [
    controller(devId="111", devName="4x4 Tent", devMacAddr="AA:BB:CC:DD:EE:FF"),
    controller(devId="222", devName="Veg Closet", devMacAddr="11:22:33:44:55:66"),
]
check("select no filter returns all", len(cloud.select(devices, None)) == 2)
check("select by devId", [d["devId"] for d in cloud.select(devices, "222")] == ["222"])
check("select by name substring", [d["devId"] for d in cloud.select(devices, "veg")] == ["222"])
check("select by MAC", [d["devId"] for d in cloud.select(devices, "aa:bb")] == ["111"])
check("select no match returns empty", cloud.select(devices, "basement") == [])
check("select skips non-dicts", cloud.select([None, "x", devices[0]], None) == [devices[0]])

# --------------------------------------------------------------------------
# CloudClient.login()
# --------------------------------------------------------------------------

client, session = make_client(password="hunter2", responses={cloud.API_URL_LOGIN: FakeResponse(body=login_body())})
user_id = client.login()
check("login returns appId", user_id == "user-42", user_id)
check("login stores user_id", client.user_id == "user-42")
call = session.calls[0]
check("login posts to login path", call["path"] == cloud.API_URL_LOGIN, call["path"])
check("login sends appEmail", call["data"]["appEmail"] == "grower@example.com", call["data"])
check("login sends appPasswordl (API typo)", call["data"]["appPasswordl"] == "hunter2", call["data"])
check("login sends no token header", "token" not in call["headers"], call["headers"])
check("login sets okhttp user agent", call["headers"]["User-Agent"] == "okhttp/4.12.0", call["headers"])

# Password truncation.
long_pw = "abcdefghijklmnopqrstuvwxyz0123456789"
client_long, _ = make_client(password=long_pw, responses={cloud.API_URL_LOGIN: FakeResponse(body=login_body())})
client_long.login()
check("password truncated to 25 chars", len(client_long.password) == 25, len(client_long.password))
check("password truncated correctly", client_long.password == long_pw[:25], client_long.password)

# Rejected login.
client_bad, _ = make_client(responses={cloud.API_URL_LOGIN: FakeResponse(body={"code": 400, "msg": "User Does Not Exist"})})
try:
    client_bad.login()
    check("bad login raises AuthError", False, "no exception")
except cloud.AuthError as exc:
    check("bad login raises AuthError", "User Does Not Exist" in str(exc), str(exc))

# Login OK but no appId.
client_noid, _ = make_client(responses={cloud.API_URL_LOGIN: FakeResponse(body={"code": 200, "data": {}})})
try:
    client_noid.login()
    check("login without appId raises AuthError", False, "no exception")
except cloud.AuthError:
    check("login without appId raises AuthError", True)

# --------------------------------------------------------------------------
# CloudClient.devices()
# --------------------------------------------------------------------------

client, session = make_client(responses={
    cloud.API_URL_LOGIN: FakeResponse(body=login_body()),
    cloud.API_URL_GET_DEVICE_INFO_LIST_ALL: FakeResponse(body=devices_body(devices)),
})
client.login()
result = client.devices()
check("devices returns list", len(result) == 2, result)
device_call = session.calls[1]
check("devices sends userId", device_call["data"]["userId"] == "user-42", device_call["data"])
check("devices sends token header", device_call["headers"]["token"] == "user-42", device_call["headers"])

# devices() before login().
client_unlogged, _ = make_client()
try:
    client_unlogged.devices()
    check("devices before login raises", False, "no exception")
except cloud.ApiError:
    check("devices before login raises", True)

# Empty account -> empty list, not a crash.
client_empty, _ = make_client(responses={
    cloud.API_URL_LOGIN: FakeResponse(body=login_body()),
    cloud.API_URL_GET_DEVICE_INFO_LIST_ALL: FakeResponse(body={"code": 200, "data": None}),
})
client_empty.login()
check("null data -> empty list", client_empty.devices() == [])

# --------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------

client_500, _ = make_client(responses={cloud.API_URL_LOGIN: FakeResponse(status_code=500, body=login_body())})
try:
    client_500.login()
    check("HTTP 500 raises ApiError", False, "no exception")
except cloud.ApiError as exc:
    check("HTTP 500 raises ApiError", "500" in str(exc), str(exc))

client_html, _ = make_client(responses={cloud.API_URL_LOGIN: FakeResponse(body=None)})
try:
    client_html.login()
    check("non-JSON raises ApiError", False, "no exception")
except cloud.ApiError as exc:
    check("non-JSON raises ApiError", "non-JSON" in str(exc), str(exc))

client_net, _ = make_client(raise_exc=requests.ConnectionError("no route to host"))
try:
    client_net.login()
    check("network error raises ApiError", False, "no exception")
except cloud.ApiError as exc:
    check("network error raises ApiError", "no route to host" in str(exc), str(exc))

client_devfail, _ = make_client(responses={
    cloud.API_URL_LOGIN: FakeResponse(body=login_body()),
    cloud.API_URL_GET_DEVICE_INFO_LIST_ALL: FakeResponse(body={"code": 500, "msg": "server exploded"}),
})
client_devfail.login()
try:
    client_devfail.devices()
    check("device list failure raises ApiError", False, "no exception")
except cloud.ApiError as exc:
    check("device list failure raises ApiError", "server exploded" in str(exc), str(exc))

# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

ENV_KEYS = ("ACINF_EMAIL", "ACINF_PASSWORD")


def run_main(argv, responses=None, raise_exc=None, env=None):
    """Run main() with a fake client and captured stdio."""
    saved_env = {k: os.environ.get(k) for k in ENV_KEYS}
    saved_ctor = cloud.CloudClient

    def factory(email, password, **kwargs):
        return saved_ctor(email, password, session=FakeSession(responses, raise_exc=raise_exc))

    os.environ.pop("ACINF_EMAIL", None)
    os.environ.pop("ACINF_PASSWORD", None)
    if env:
        os.environ.update(env)
    cloud.CloudClient = factory
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cloud.main(argv)
    finally:
        cloud.CloudClient = saved_ctor
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return code, out.getvalue(), err.getvalue()


GOOD_ENV = {"ACINF_EMAIL": "grower@example.com", "ACINF_PASSWORD": "hunter2"}
GOOD_RESPONSES = {
    cloud.API_URL_LOGIN: FakeResponse(body=login_body()),
    cloud.API_URL_GET_DEVICE_INFO_LIST_ALL: FakeResponse(body=devices_body([controller()])),
}

code, out, err = run_main([], responses=GOOD_RESPONSES)
check("cli missing env exits 2", code == 2, code)
check("cli missing env explains", "ACINF_EMAIL" in err, err)
check("cli missing env prints nothing", out == "", out)

code, out, err = run_main([], responses=GOOD_RESPONSES, env={"ACINF_EMAIL": "x"})
check("cli partial env exits 2", code == 2, code)

code, out, err = run_main([], responses=GOOD_RESPONSES, env=GOOD_ENV)
check("cli single poll exits 0", code == 0, (code, err))
lines = [json.loads(line) for line in out.splitlines() if line.strip()]
check("cli prints one JSON line", len(lines) == 1, out)
check("cli JSON has temperature", lines and lines[0]["temperature"] == 24.17, lines)
check("cli JSON is sorted keys", out.strip().startswith('{"address"'), out)

code, out, err = run_main([], responses=GOOD_RESPONSES, env=GOOD_ENV)
check("cli output is valid one-object-per-line JSON", all(json.loads(l) for l in out.splitlines()))

# Filter that matches nothing.
code, out, err = run_main(["--device", "basement"], responses=GOOD_RESPONSES, env=GOOD_ENV)
check("cli no match exits 1", code == 1, code)
check("cli no match explains", "no controller matched" in err, err)
check("cli no match prints nothing", out == "", out)

# Filter that matches.
code, out, err = run_main(["--device", "tent"], responses=GOOD_RESPONSES, env=GOOD_ENV)
check("cli --device match exits 0", code == 0, (code, err))

# Bad credentials.
bad_login = dict(GOOD_RESPONSES)
bad_login[cloud.API_URL_LOGIN] = FakeResponse(body={"code": 400, "msg": "User Does Not Exist"})
code, out, err = run_main([], responses=bad_login, env=GOOD_ENV)
check("cli bad auth exits 3", code == 3, code)
check("cli bad auth explains", "login rejected" in err, err)

# Network failure.
code, out, err = run_main([], raise_exc=requests.ConnectionError("offline"), env=GOOD_ENV)
check("cli network error exits 4", code == 4, code)

# --raw dumps the payload.
code, out, err = run_main(["--raw"], responses=GOOD_RESPONSES, env=GOOD_ENV)
check("cli --raw exits 0", code == 0, (code, err))
raw = json.loads(out)
check("cli --raw returns device array", isinstance(raw, list) and len(raw) == 1, raw)
check("cli --raw keeps API keys", "devId" in raw[0], raw[0])

# Empty account.
empty_responses = dict(GOOD_RESPONSES)
empty_responses[cloud.API_URL_GET_DEVICE_INFO_LIST_ALL] = FakeResponse(body=devices_body([]))
code, out, err = run_main([], responses=empty_responses, env=GOOD_ENV)
check("cli empty account exits 1", code == 1, code)
check("cli empty account explains", "no controllers" in err, err)

# --interval with --duration 0 must not hang or poll.
code, out, err = run_main(["--interval", "1", "--duration", "0"], responses=GOOD_RESPONSES, env=GOOD_ENV)
check("cli --duration 0 exits 0", code == 0, code)
check("cli --duration 0 prints nothing", out == "", out)

print()
print("FAILURES:", len(fails), fails if fails else "")
sys.exit(1 if fails else 0)
