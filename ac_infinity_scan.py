"""Scan for AC Infinity UIS controllers (Controller 69 / 69 Pro, etc.) over Bluetooth
and extract live sensor data.

The controller advertises temperature / humidity / VPD / fan speed in manufacturer
specific data (company ID 0x0902) roughly once per second, so no pairing or GATT
connection is required for the default passive scan. An optional connected mode
subscribes to the controller's notification characteristic and parses the live push
stream (which also includes fan type and operating mode).

Requirements:
    pip install bleak

Usage:
    python3 ac_infinity_scan.py                       # passive scan for 15 seconds
    python3 ac_infinity_scan.py --duration 60
    python3 ac_infinity_scan.py --once                # exit after the first reading
    python3 ac_infinity_scan.py --address AA:BB:CC:DD:EE:FF
    python3 ac_infinity_scan.py --connect --duration 10   # subscribe to live stream
"""

import argparse
import asyncio
import json
import struct
import sys

from bleak import BleakClient, BleakScanner

MANUFACTURER_ID = 2306
SERVICE_UUID = "70D51000-2C7F-4E75-AE8A-D758951CE4E0"
WRITE_UUID = "70D51001-2C7F-4E75-AE8A-D758951CE4E0"
NOTIFY_UUID = "70D51002-2C7F-4E75-AE8A-D758951CE4E0"

MODEL_NAMES = {
    1: "Controller 67",
    6: "AirTap T4",
    7: "Controller 69",
    9: "Controller 69 Pro",
    11: "Controller 69 Pro",
    12: "Controller 69 Pro+",
}

FAN_STATES = {
    0: "OFF",
    1: "ON",
    2: "AUTO",
    3: "UNKNOWN",
}


def get_bit(value, index):
    return (value >> (7 - index)) & 1 == 0


def get_bits(value, index, width):
    return (value >> (8 - index - width)) & (255 >> (8 - width))


def get_short(data, offset):
    return struct.unpack(">h", bytes(data[offset:offset + 2]))[0]


def parse_advertisement(manufacturer_data):
    data = manufacturer_data.get(MANUFACTURER_ID)
    # The base reading reads up to index 18, so anything shorter would raise.
    # (The original guard allowed 17, which crashed on data[18].)
    if data is None or len(data) < 19:
        return None
    version, device_type = data[11], data[12]
    reading = {
        "model": MODEL_NAMES.get(device_type, f"Type {device_type}"),
        "type_id": device_type,
        "version": version,
        "unit": "C" if True ^ get_bit(data[13], 1) else "F",  # bit 1 set => Celsius
        "temperature": get_short(data, 14) / 100,
        "humidity": get_short(data, 16) / 100,
        "fan_speed": data[18],
        "fan_state": FAN_STATES.get(get_bits(data[13], 2, 2)),
        "temp_state": get_bits(data[13], 4, 2),
        "hum_state": get_bits(data[13], 6, 2),
    }
    # The extended block reads up to index 22, so it needs a 23-byte payload.
    if version >= 3 and device_type in (7, 9, 11, 12) and len(data) >= 23:
        reading["port"] = data[19]
        reading["vpd"] = get_short(data, 21) / 100
        reading["vpd_state"] = get_bits(data[20], 0, 2)
    return reading


def parse_notification(data):
    if len(data) < 18 or data[0] != 30 or data[1] != 255:
        return None
    return {
        "unit": "C" if get_bit(data[6], 0) else "F",
        "temperature": get_short(data, 8) / 100,
        "humidity": get_short(data, 10) / 100,
        "vpd": get_short(data, 12) / 100,
        "fan_type": get_short(data, 14),
        "fan_state": FAN_STATES.get(get_bits(data[16], 0, 2)),
        "work_type": get_bits(data[17], 4, 4),
        "port": get_bits(data[7], 4, 4),
    }


def emit(reading, address, name, rssi):
    payload = {"address": address, "name": name, "rssi": rssi, **reading}
    print(json.dumps(payload, sort_keys=True), flush=True)


async def passive_scan(address, duration, once):
    stop = asyncio.Event()
    seen = []

    def callback(device, advertisement_data):
        if address and device.address.lower() != address.lower():
            return
        reading = parse_advertisement(advertisement_data.manufacturer_data)
        if reading is None:
            return
        emit(reading, device.address, device.name, advertisement_data.rssi)
        seen.append(True)
        if once:
            stop.set()

    scanner = BleakScanner(callback)
    await scanner.start()
    try:
        if once:
            try:
                await asyncio.wait_for(stop.wait(), timeout=duration)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(duration)
    finally:
        await scanner.stop()
    return bool(seen)


async def connected_stream(address, duration):
    async with BleakClient(address) as client:
        await client.start_notify(NOTIFY_UUID, _on_notify(address))
        await asyncio.sleep(duration)
        await client.stop_notify(NOTIFY_UUID)


def _on_notify(address):
    def handler(_sender, data):
        reading = parse_notification(data)
        if reading:
            emit(reading, address, "ACI-E", None)

    return handler


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=15.0, help="scan window in seconds")
    parser.add_argument("--address", help="filter to a specific BLE MAC address")
    parser.add_argument("--once", action="store_true", help="exit after the first reading")
    parser.add_argument("--connect", action="store_true", help="also subscribe to the live notification stream")
    args = parser.parse_args()

    if args.connect and not args.address:
        print("--connect requires --address", file=sys.stderr)
        return 2

    found = await passive_scan(args.address, args.duration, args.once)

    if args.once and not found:
        print(f"no AC Infinity controller seen in {args.duration:g}s", file=sys.stderr)
        return 1

    if args.connect:
        await connected_stream(args.address, args.duration)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
