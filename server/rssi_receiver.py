"""RSSI受信(中継raspi -> 中央サーバ)。

中継raspi から送られてくるRSSIをUDPで受信し、Bufferへ積むだけ。
位置判定はしない(関心の分離)。
送信側は `relay_rssi_sender.py`(中継raspi上で実行)。
フォーマットは {"beacon": "phone1", "rssi": -67}(タイムスタンプは受信側で付与)。
"""
from __future__ import annotations

import asyncio
import json
import time

from position import RSSIBuffer, RSSISample


class RSSIReceiverProtocol(asyncio.DatagramProtocol):
    """UDPデータグラムを受け、JSONをパースしてBufferへ積む。

    フォーマット: {"beacon": "phone1", "rssi": -67}(relay_rssi_sender.py が送る)。
    """

    def __init__(self, buffer: RSSIBuffer) -> None:
        self.buffer = buffer

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            msg = json.loads(data.decode())
            self.buffer.add(
                RSSISample(
                    beacon_id=str(msg["beacon"]),
                    rssi=float(msg["rssi"]),
                    ts=time.time(),
                )
            )
        except Exception:
            # スケルトン: フォーマット確定後にバリデーション/ログを強化する。
            pass


async def start_rssi_receiver(buffer: RSSIBuffer, host: str, port: int) -> None:
    """UDP受信エンドポイントを起動(asyncioループに常駐)。"""
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(
        lambda: RSSIReceiverProtocol(buffer),
        local_addr=(host, port),
    )
