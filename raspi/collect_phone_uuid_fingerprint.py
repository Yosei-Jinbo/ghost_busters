import argparse
import asyncio
import csv
import os
import time
import uuid
from collections import defaultdict
from bleak import BleakScanner


# ============================================================
# スマホ側ビーコンのUUIDを設定
#
# 使い方1：スマホごとにUUIDを変える場合
#   "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx": "phone1"
#
# 使い方2：iBeaconで同じUUID + major/minorで区別する場合
#   "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:1:1": "phone1"
#   "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx:1:2": "phone2"
# ============================================================
TARGETS = {
    "48534442-4C45-4144-80C0-1800FFFFFFFA": "phone1",
    "48534442-4C45-4144-80C0-1800FFFFFFFB": "phone2",
    "48534442-4C45-4144-80C0-1800FFFFFFFC": "phone3",
    "48534442-4C45-4144-80C0-1800FFFFFFFD": "phone4",
    "2F234454-CF6D-4A0F-ADF2-F4911BA9FFA5": "phone5",
    "2F234454-CF6D-4A0F-ADF2-F4911BA9FFA6": "phone6",
    "48534442-4C45-4144-80C0-1800FFFFFFFE": "phone7",
    "48534442-4C45-4144-80C0-1800FFFFFFFF": "phone8",
}


def canonical_uuid(s: str) -> str:
    return str(uuid.UUID(s.strip()))


def normalize_target_key(key: str) -> str:
    parts = key.strip().lower().split(":")

    if len(parts) == 1:
        return canonical_uuid(parts[0])

    if len(parts) == 2:
        return f"{canonical_uuid(parts[0])}:{int(parts[1])}"

    if len(parts) == 3:
        return f"{canonical_uuid(parts[0])}:{int(parts[1])}:{int(parts[2])}"

    raise ValueError(f"Invalid target key: {key}")


TARGETS = {normalize_target_key(k): v for k, v in TARGETS.items()}
BEACONS = list(TARGETS.values())


def parse_ibeacon(manufacturer_data):
    """
    Apple iBeacon:
      Company ID: 0x004C
      Data: 0x02 0x15 UUID(16B) major(2B) minor(2B) tx_power(1B)
    """
    results = []

    for company_id, data in (manufacturer_data or {}).items():
        if company_id != 0x004C:
            continue

        if len(data) < 23:
            continue

        if data[0] != 0x02 or data[1] != 0x15:
            continue

        beacon_uuid = str(uuid.UUID(bytes=bytes(data[2:18])))
        major = int.from_bytes(data[18:20], byteorder="big")
        minor = int.from_bytes(data[20:22], byteorder="big")
        tx_power = int.from_bytes(data[22:23], byteorder="big", signed=True)

        results.append({
            "source": "ibeacon",
            "uuid": beacon_uuid,
            "major": major,
            "minor": minor,
            "tx_power": tx_power,
        })

    return results


def extract_uuid_candidates(device, adv_data):
    """
    BLE広告から候補UUIDを抽出する．
    - Service UUID
    - iBeacon UUID
    """
    candidates = []

    for su in getattr(adv_data, "service_uuids", []) or []:
        try:
            candidates.append({
                "source": "service",
                "uuid": canonical_uuid(su),
                "major": None,
                "minor": None,
                "tx_power": None,
            })
        except ValueError:
            continue

    candidates.extend(parse_ibeacon(getattr(adv_data, "manufacturer_data", {}) or {}))

    return candidates


def find_target(device, adv_data):
    """
    TARGETSと照合する．
    優先順位:
      UUID:major:minor
      UUID:major
      UUID
    """
    candidates = extract_uuid_candidates(device, adv_data)

    for c in candidates:
        u = c["uuid"]
        major = c["major"]
        minor = c["minor"]

        keys = []

        if major is not None and minor is not None:
            keys.append(f"{u}:{major}:{minor}")

        if major is not None:
            keys.append(f"{u}:{major}")

        keys.append(u)

        for key in keys:
            if key in TARGETS:
                return TARGETS[key], key, c

    return None, None, None


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect BLE RSSI fingerprint data from smartphone UUID beacons."
    )

    parser.add_argument("--label", required=True, help="Grid label, e.g., G1, G2")
    parser.add_argument("--duration", type=float, required=True, help="Duration in seconds")
    parser.add_argument("--out", default="phone_fingerprint.csv", help="Output CSV file")
    parser.add_argument("--sample-interval", type=float, default=0.2, help="CSV sampling interval")
    parser.add_argument("--stale-sec", type=float, default=6.0, help="Seconds before a beacon is treated as missing")
    parser.add_argument("--missing-rssi", type=float, default=-100.0, help="Missing RSSI value")
    parser.add_argument("--receiver-id", default="raspi2", help="Receiver ID")
    parser.add_argument("--warmup", type=float, default=2.0, help="Warmup time before recording")
    parser.add_argument("--debug-all", action="store_true", help="Print all detected UUID candidates for debugging")
    parser.add_argument("--append", action="store_true", help="Append without deleting existing rows for the same label")

    return parser.parse_args()


def build_csv_header():
    header = [
        "wall_time",
        "elapsed_s",
        "label",
        "receiver_id",
        "n_valid",
    ]

    for beacon in BEACONS:
        header += [
            f"{beacon}_rssi",
            f"{beacon}_age_s",
            f"{beacon}_valid",
            f"{beacon}_seq",
            f"{beacon}_uuid",
            f"{beacon}_source",
            f"{beacon}_major",
            f"{beacon}_minor",
            f"{beacon}_addr",
        ]

    return header


def remove_existing_label_rows(csv_path, label, header):
    """
    既存CSV内に同じlabelの行がある場合，その行を全て削除する．
    これにより，同じG1/G2などを再測定したときはデフォルトで上書き扱いになる．
    他のlabelの行は残す．
    """
    if not os.path.exists(csv_path):
        return 0

    tmp_path = f"{csv_path}.tmp"
    removed = 0

    with open(csv_path, "r", newline="") as src:
        reader = csv.reader(src)
        old_header = next(reader, None)

        # 空ファイルなら，いったんヘッダだけ作り直す
        if not old_header:
            with open(tmp_path, "w", newline="") as dst:
                writer = csv.writer(dst)
                writer.writerow(header)
            os.replace(tmp_path, csv_path)
            return 0

        # label列が見つからない古い/壊れたCSVなら，安全側でヘッダだけ作り直す
        if "label" not in old_header:
            with open(tmp_path, "w", newline="") as dst:
                writer = csv.writer(dst)
                writer.writerow(header)
            os.replace(tmp_path, csv_path)
            return 0

        label_idx = old_header.index("label")

        with open(tmp_path, "w", newline="") as dst:
            writer = csv.writer(dst)

            # 新しいコード側のヘッダを使う．
            # TARGETSを変えて列が増減した場合でも，今後の追記列と揃いやすい．
            writer.writerow(header)

            for row in reader:
                if len(row) > label_idx and row[label_idx] == label:
                    removed += 1
                    continue
                writer.writerow(row)

    os.replace(tmp_path, csv_path)
    return removed


async def main():
    args = parse_args()

    latest = {}
    seq = defaultdict(int)
    debug_last = defaultdict(float)

    def callback(device, adv_data):
        now = time.time()

        if args.debug_all:
            addr = device.address.upper()
            if now - debug_last[addr] > 1.0:
                name = device.name or adv_data.local_name or "unknown"
                cands = extract_uuid_candidates(device, adv_data)
                if cands:
                    print(f"[DEBUG] addr={addr} name={name} rssi={adv_data.rssi} candidates={cands}")
                debug_last[addr] = now

        beacon_id, target_key, cand = find_target(device, adv_data)
        if beacon_id is None:
            return

        seq[beacon_id] += 1

        latest[beacon_id] = {
            "beacon_id": beacon_id,
            "target_key": target_key,
            "uuid": cand["uuid"],
            "source": cand["source"],
            "major": cand["major"],
            "minor": cand["minor"],
            "addr": device.address.upper(),
            "name": device.name or adv_data.local_name or "unknown",
            "rssi": adv_data.rssi,
            "timestamp": now,
            "seq": seq[beacon_id],
        }

    print("Start smartphone UUID beacon fingerprint collection")
    print(f"label          : {args.label}")
    print(f"duration       : {args.duration} s")
    print(f"sample interval: {args.sample_interval} s")
    print(f"output         : {args.out}")
    print("targets:")
    for key, name in TARGETS.items():
        print(f"  {name}: {key}")

    try:
        scanner = BleakScanner(
            callback,
            scanning_mode="active",
            bluez={
                "filters": {
                    "Transport": "le",
                    "DuplicateData": True,
                }
            },
        )
    except TypeError:
        scanner = BleakScanner(callback)

    await scanner.start()

    try:
        header = build_csv_header()

        if args.append:
            print("append mode     : enabled")
        else:
            removed = remove_existing_label_rows(args.out, args.label, header)
            print(f"overwrite mode  : removed {removed} existing rows for label={args.label}")

        if args.warmup > 0:
            print(f"warming up for {args.warmup} s ...")
            await asyncio.sleep(args.warmup)

        file_exists = os.path.exists(args.out)
        file_is_empty = (not file_exists) or os.path.getsize(args.out) == 0

        start_mono = time.monotonic()
        next_sample = start_mono

        with open(args.out, "a", newline="") as f:
            writer = csv.writer(f)

            if file_is_empty:
                writer.writerow(header)

            print("collecting...")
            while True:
                now_mono = time.monotonic()
                elapsed = now_mono - start_mono

                if elapsed >= args.duration:
                    break

                if now_mono < next_sample:
                    await asyncio.sleep(min(0.01, next_sample - now_mono))
                    continue

                now_wall = time.time()

                row = [
                    now_wall,
                    round(elapsed, 3),
                    args.label,
                    args.receiver_id,
                ]

                values = []
                n_valid = 0

                for beacon in BEACONS:
                    if beacon in latest:
                        item = latest[beacon]
                        age = now_wall - item["timestamp"]

                        if age <= args.stale_sec:
                            valid = 1
                            n_valid += 1
                            rssi = item["rssi"]
                            seq_val = item["seq"]
                            uuid_val = item["uuid"]
                            source_val = item["source"]
                            major_val = "" if item["major"] is None else item["major"]
                            minor_val = "" if item["minor"] is None else item["minor"]
                            addr_val = item["addr"]
                        else:
                            valid = 0
                            rssi = args.missing_rssi
                            seq_val = ""
                            uuid_val = ""
                            source_val = ""
                            major_val = ""
                            minor_val = ""
                            addr_val = ""
                    else:
                        age = ""
                        valid = 0
                        rssi = args.missing_rssi
                        seq_val = ""
                        uuid_val = ""
                        source_val = ""
                        major_val = ""
                        minor_val = ""
                        addr_val = ""

                    values += [
                        rssi,
                        round(age, 3) if isinstance(age, float) else age,
                        valid,
                        seq_val,
                        uuid_val,
                        source_val,
                        major_val,
                        minor_val,
                        addr_val,
                    ]

                row.append(n_valid)
                row += values

                writer.writerow(row)
                f.flush()

                print(
                    f"\rlabel={args.label} elapsed={elapsed:6.2f}s "
                    f"valid={n_valid}/{len(BEACONS)}",
                    end="",
                    flush=True,
                )

                next_sample += args.sample_interval

        print()
        print(f"done. saved to {args.out}")

    finally:
        await scanner.stop()


if __name__ == "__main__":
    asyncio.run(main())
