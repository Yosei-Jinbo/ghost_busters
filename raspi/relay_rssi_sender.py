"""中継raspi -> 中央サーバ: 生RSSI送信(採用案)。

中継raspi(ユーザが携帯)がBLEをスキャンし、検出した各ビーコンの生RSSIを
UDPで中央サーバ(ゲームPC)へ送るだけのスタンドアロンツール。位置推定はしない
(PC側の `PositionEstimator` が kNN で行う)。

送信フォーマットは `rssi_receiver.py` が受ける形に合わせる:
    {"beacon": "phone1", "rssi": -67}
タイムスタンプは受信側(`rssi_receiver`)が付けるので送らない。

BLEスキャンのUUID照合ロジック(TARGETS / iBeaconパース / find_target)は
`collect_phone_uuid_fingerprint.py` の原型をそのまま import して再利用する。
これでビーコン定義を一箇所に保ち、収集スクリプトと挙動を揃える。

使い方(中継raspi上):
    python relay_rssi_sender.py --server-ip 192.168.0.10
"""
import argparse
import asyncio
import json
import socket
import time

from bleak import BleakScanner

# スキャン/UUID照合は収集スクリプトの原型を再利用する(単一の真実)。
from collect_phone_uuid_fingerprint import BEACONS, find_target


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Relay raw BLE RSSI to the central game server over UDP."
    )
    parser.add_argument(
        "--server-ip", required=True,
        help="Central server (game PC) IP, e.g., 192.168.0.10",
    )
    parser.add_argument(
        "--server-port", type=int, default=9000,
        help="Server UDP port (= config.RSSI_LISTEN_PORT). Default: 9000",
    )
    parser.add_argument(
        "--interval", type=float, default=0.2,
        help="Send interval in seconds (sampling cadence). Default: 0.2",
    )
    parser.add_argument(
        "--stale-sec", type=float, default=6.0,
        help="Drop a beacon if its last sighting is older than this. Default: 6.0",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    # ビーコンごとの最新観測(RSSIと観測時刻)。callbackが書き、送信ループが読む。
    latest: dict = {}

    def callback(device, adv_data) -> None:
        beacon_id, _target_key, _cand = find_target(device, adv_data)
        if beacon_id is None:
            return
        latest[beacon_id] = {
            "rssi": adv_data.rssi,
            "timestamp": time.time(),
        }

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server = (args.server_ip, args.server_port)

    print("Start raw RSSI relay (中継raspi -> central server)")
    print(f"server   : {server[0]}:{server[1]}")
    print(f"interval : {args.interval} s")
    print(f"targets  : {', '.join(BEACONS)}")

    # collect_phone_uuid_fingerprint.py と同じスキャナ初期化(原型を踏襲)。
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
        next_sample = time.monotonic()
        while True:
            now_mono = time.monotonic()
            if now_mono < next_sample:
                await asyncio.sleep(min(0.01, next_sample - now_mono))
                continue

            now_wall = time.time()
            sent = 0
            for beacon in BEACONS:
                item = latest.get(beacon)
                if item is None:
                    continue
                # 古すぎる観測は送らない(欠測はPC側がそのまま欠測として扱う)。
                if now_wall - item["timestamp"] > args.stale_sec:
                    continue

                payload = {"beacon": beacon, "rssi": item["rssi"]}
                sock.sendto(json.dumps(payload).encode("utf-8"), server)
                sent += 1

            print(
                f"\rsent {sent}/{len(BEACONS)} beacons -> {server[0]}:{server[1]}",
                end="",
                flush=True,
            )
            next_sample += args.interval

    finally:
        await scanner.stop()
        sock.close()
        print()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped")
