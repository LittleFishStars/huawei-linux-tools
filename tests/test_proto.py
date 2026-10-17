# SPDX-License-Identifier: GPL-2.0-or-later
"""假设备驱动的协议时序单测（不需要硬件）。

覆盖五个在真机上必须成立的行为：

1. ACK 与响应落在同一个 IRQ 窗口时，响应不能丢
2. ACK 与响应分两个窗口时，也不能丢
3. 写一帧是**两个 CS 周期**（头 4 字节、载荷 N 字节各一次）
4. 总线空闲（MISO 全 0xff）要退避重试，不能立刻失败
5. 头校验坏（噪声）要跳过继续找，不能当成包

用 `python3 tests/test_proto.py` 直接跑，退出码 0 表示全过。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from gdix51c0 import proto, wire  # noqa: E402


class FakeDev:
    """把若干个包拼成一条连续 MISO 流，按请求长度切。

    判定"这是读还是写"靠"tx 是不是全 0xff 且长度等于请求长度"——这正是
    PacketIo.read() 的调用形态。
    """

    def __init__(self, packets: list[bytes]):
        self.buf = bytearray()
        for p in packets:
            self.buf += p
        self.tx_log: list[bytes] = []

    def xfer(self, tx: bytes, rx_len: int | None = None, speed_hz: int = 0) -> bytes:
        n = max(len(tx), rx_len or 0)
        if tx == b"\xff" * len(tx) and len(tx) == n:
            data = bytes(self.buf[:n])
            del self.buf[:n]
        else:
            self.tx_log.append(bytes(tx))
            data = b"\x00" * n
        return (data + b"\xff" * n)[:n]


def pkt(outer: int, payload: bytes) -> bytes:
    return wire.encode_outer_header(outer, len(payload)) + payload


def _ack() -> bytes:
    return pkt(wire.OUTER_WRITE, wire.encode_command(wire.CMD_ACK))


def _chipid() -> bytes:
    return pkt(
        wire.OUTER_WRITE,
        wire.encode_command(wire.CMD_READ_SENSOR_REG, b"\xa2\x04\x25\x00"),
    )


CMD = proto.cmd_register_read(0, 4)


def test_same_irq_window() -> None:
    """ACK 与响应同窗口连着到达时，响应必须能拿到。"""
    io = proto.PacketIo(FakeDev([_ack(), _chipid()]))
    out = io.send_ack_then_response(CMD, timeout=0.05)
    assert out.cmd == wire.CMD_READ_SENSOR_REG, hex(out.cmd)
    assert out.data == b"\xa2\x04\x25\x00", out.data.hex()


def test_split_window() -> None:
    """ACK 先到、响应后到时也必须能拿到。"""
    dev = FakeDev([_ack()])
    io = proto.PacketIo(dev)
    io.send_and_ack(CMD, timeout=0.05)
    dev.buf += _chipid()
    pkt_obj = io.next_packet(0.05)
    assert pkt_obj is not None
    assert pkt_obj.command().data == b"\xa2\x04\x25\x00"


def test_write_is_two_cs_cycles() -> None:
    """写一帧要拆成头与载荷两次传输。"""
    dev = FakeDev([_ack()])
    io = proto.PacketIo(dev)
    io.send_no_ack(proto.PREFIX01)
    assert [len(x) for x in dev.tx_log] == [4, 8], dev.tx_log


def test_idle_bus_retries() -> None:
    """全 0xff 是总线空闲，不是错误。"""
    dev = FakeDev([b"\xff" * 4, b"\xff" * 4, _ack()])
    io = proto.PacketIo(dev)
    io.send_and_ack(CMD, timeout=0.5)


def test_bad_header_is_skipped() -> None:
    """头校验坏当作噪声跳过。"""
    bad = b"\xa0\x04\x00\x00"  # 校验和应为 0xa4，这里是 0x00
    dev = FakeDev([bad, _ack()])
    io = proto.PacketIo(dev)
    io.send_and_ack(CMD, timeout=0.5)


def test_stash_is_drained() -> None:
    """停滞区里的包在 drain 后不应残留。"""
    io = proto.PacketIo(FakeDev([_ack(), _chipid()]))
    io.send_ack_then_response(CMD, timeout=0.05)
    io.drain(0.005)
    assert io._stash == [], io._stash


TESTS = [
    ("同 IRQ 窗口的 ACK+响应", test_same_irq_window),
    ("分离窗口的 ACK/响应", test_split_window),
    ("写帧分两个 CS 周期", test_write_is_two_cs_cycles),
    ("总线空闲退避重试", test_idle_bus_retries),
    ("坏头当噪声跳过", test_bad_header_is_skipped),
    ("drain 清空停滞区", test_stash_is_drained),
]


def main() -> int:
    failed = 0
    for name, fn in TESTS:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL {name}: {exc}")
    if failed:
        print(f"\n{failed} 项失败", file=sys.stderr)
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
