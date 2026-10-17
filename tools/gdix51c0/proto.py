# SPDX-License-Identifier: GPL-2.0-or-later
"""SPI 包搬运与命令语义。

这一层处理三件在字节层面必须精确的事：

1. **头与载荷分两个 CS 周期发**。官方驱动是一次 ``write(hdr, 4)`` 再
   ``write(payload, len)``，不是一个连续缓冲。合并发送时 MCU 会在两段之间
   丢字节。
2. **读永远是全双工 ``0xff`` 填充**。用普通 ``read()``（半双工）会拿到错位
   的图像数据。
3. **读长度按头里的 len 精确切**。多读一个字节就会把下一个包的头吃掉。

读头对全 0 / 全 0xff 的响应视为总线空闲并退避重试，而不是当成错误。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import wire
from .transport import KernelDevice

#: 一次 SPI 读的最大长度（上游按 2048 分段搬运）
CHUNK = 2048
#: 单个包的最大载荷（超过就当作头噪声）
MAX_PAYLOAD = 8192
#: 偶发失败时整包重投次数
CMD_ATTEMPTS = 2
#: 写之前复现 Windows 的 Sleep(1)
PRE_SEND_DELAY = 0.001
#: ACK 之后多读一次的宽限：同 IRQ 窗口里的响应应该已经到了
SAME_IRQ_GRACE = 0.050

ACK_TIMEOUT = 1.0
RESPONSE_TIMEOUT = 1.0
BOOT_TIMEOUT = 3.0


class ProtoError(RuntimeError):
    pass


@dataclass
class Packet:
    """一个已解析的 SPI 包。"""

    outer_type: int
    payload: bytes

    @property
    def inner_cmd(self) -> int:
        """载荷首字节。判断包语义看这个，不要看外框。"""
        return self.payload[0] if self.payload else -1

    def command(self) -> wire.Command:
        return wire.decode_command(self.payload)


class PacketIo:
    """在 :class:`KernelDevice` 之上做包级别收发。"""

    def __init__(self, dev: KernelDevice, speed_hz: int = 0, chunk: int = CHUNK):
        self.dev = dev
        self.speed_hz = speed_hz
        self.chunk = chunk
        # 有些命令（0x82 读寄存器、0xa6 读 OTP、0xa2 复位）的 ACK 与响应
        # 落在同一个 IRQ 高电平窗口里。先收 ACK 的那一方会把响应也读进来，
        # 而“先等 ACK 再等响应”的调用方就会因为响应已到手却看不见而超时。
        # 多出来的包停在这里，由 next_packet() 先吐。
        self._stash: list[Packet] = []

    # -- 原始搬运 --

    def write(self, data: bytes) -> None:
        """一个 CS 周期内发出 data（全双工，丢弃 MISO）。"""
        off = 0
        n = len(data)
        while off < n:
            piece = data[off : off + self.chunk]
            self.dev.xfer(piece, len(piece), self.speed_hz)
            off += len(piece)

    def write_packet(self, outer_type: int, payload: bytes) -> None:
        """外框与载荷分两次发，和官方驱动的调用序列一致。"""
        self.write(wire.encode_outer_header(outer_type, len(payload)))
        self.write(payload)

    def read(self, length: int) -> bytes:
        """全双工读：MOSI 填 0xff，按段搬运。"""
        out = bytearray()
        while len(out) < length:
            want = min(self.chunk, length - len(out))
            out += self.dev.xfer(b"\xff" * want, want, self.speed_hz)
        return bytes(out)

    # -- 包级别 --

    def read_packet(self, timeout: float = RESPONSE_TIMEOUT) -> Packet | None:
        """读一个完整的包；总线上没东西时返回 None。

        头阶段对全 0 / 全 0xff 退避重试（总线空闲），载荷阶段按头里的长度
        精确读取。重试一直是跑完整个 timeout 预算，不设固定次数上限 ——
        固定 4 次 × ~4ms 只能覆盖十几毫秒，而 MCU 的响应窗口是 1 秒。
        """
        deadline = time.monotonic() + timeout
        attempt = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            hdr = self.read(wire.OUTER_HEADER_SIZE)
            attempt += 1
            busy = hdr in (b"\x00\x00\x00\x00", b"\xff\xff\xff\xff")
            if busy:
                # 总线空闲：退避一会再试，退避量不超过剩余预算
                time.sleep(min(0.005 + attempt * 0.005, max(remaining, 0.001)))
                continue
            try:
                outer_type, plen = wire.decode_outer_header(hdr)
            except wire.WireError:
                # 头校验坏只当作噪声，继续重试
                time.sleep(min(0.005 + attempt * 0.005, max(remaining, 0.001)))
                continue
            if plen > MAX_PAYLOAD:
                # 头看起来像噪声（或两包边界没对齐）：跳过，不退避以免丢真包
                continue
            if plen == 0:
                return Packet(outer_type, b"")
            tail = self.read(plen)
            if tail == b"\xff" * plen:
                # 报了长度但 MISO 全 0xff：头是噪声，继续找
                continue
            return Packet(outer_type, tail)

    # -- 命令收发的三种时序 --

    def send_no_ack(self, payload: bytes) -> None:
        """发一条不等回应的命令（例如 prefix01）。"""
        time.sleep(PRE_SEND_DELAY)
        self.write_packet(wire.OUTER_WRITE, payload)

    def next_packet(self, timeout: float = RESPONSE_TIMEOUT) -> Packet | None:
        """取下一个包：先吐停滞区，再上总线读。

        所有读包的调用方都应该走这里，不要直接调 read_packet —— 否则会把
        ACK 与响应同窗口的命令拆散。
        """
        if self._stash:
            return self._stash.pop(0)
        return self.read_packet(timeout)

    def stash_tail(self, timeout: float) -> None:
        """把总线上还挂着的包收进停滞区（用于 ACK 后的“同 IRQ”响应）。

        上游的做法是：发完命令等一个 IRQ，在那个高电平窗口里先读 ACK、
        再紧接着读响应，两次读都在同一个窗口内。本函数就是这个“再紧接着
        读”，读不到就静默返回。
        """
        pkt = self.read_packet(timeout)
        if pkt is not None:
            self._stash.append(pkt)

    def drain(self, seconds: float = 0.005) -> None:
        """丢清楚停滞区与总线上的残留包。"""
        self._stash.clear()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.read_packet(0.005) is None:
                break

    def send_and_ack(self, payload: bytes, timeout: float = ACK_TIMEOUT) -> None:
        """写 → ACK 包。上游允许重投一次。

        收到 ACK 后会多读一次，把落在同一 IRQ 窗口里的响应收进停滞区。
        这次额外的读不消耗调用方的超时预算（读不到就算了）。
        """
        last: Exception | None = None
        for attempt in range(CMD_ATTEMPTS):
            time.sleep(PRE_SEND_DELAY)
            self.write_packet(wire.OUTER_WRITE, payload)
            pkt = self.next_packet(timeout)
            if pkt is not None and pkt.inner_cmd == wire.CMD_ACK:
                # 同窗口的响应：不读会丢，读不到不报错
                self.stash_tail(SAME_IRQ_GRACE)
                return
            last = ProtoError(
                f"等 ACK 超时或类型不符（拿到 {pkt.inner_cmd if pkt else None}）"
            )
        raise ProtoError(f"命令 0x{payload[0]:02x} 未收到 ACK：{last}")

    def send_ack_then_response(
        self, payload: bytes, timeout: float = RESPONSE_TIMEOUT
    ) -> wire.Command:
        """写 → ACK → 再等一条响应（reset_sensor、读寄存器、读 OTP）。"""
        self.send_and_ack(payload, timeout)
        pkt = self.next_packet(timeout)
        if pkt is None:
            raise ProtoError(f"命令 0x{payload[0]:02x} 收到 ACK 但没有响应")
        return pkt.command()

    def send_single_response(self, payload: bytes, timeout: float = RESPONSE_TIMEOUT) -> wire.Command:
        """写 → 直接读一条响应，中间没有 ACK（get-mcu-state 等）。"""
        time.sleep(PRE_SEND_DELAY)
        self.write_packet(wire.OUTER_WRITE, payload)
        pkt = self.next_packet(timeout)
        if pkt is None:
            raise ProtoError(f"命令 0x{payload[0]:02x} 没有响应")
        return pkt.command()


# ---- 命令构造 ------------------------------------------------------------


def payload_ts(data: bytes) -> int:
    """时间戳变体的校验和：普通 checksum 再 +1。"""
    return (wire.checksum(data) + 1) & 0xFF


def cmd_idle(sleep_time: int = 0x14) -> bytes:
    return wire.encode_command(wire.CMD_IDLE, bytes([sleep_time, 0x00]))


def cmd_reset(reset_sensor: bool = True, soft_reset_mcu: bool = False, sleep: int = 0x14) -> bytes:
    flags = (0x01 if reset_sensor else 0) | (0x02 if soft_reset_mcu else 0)
    return wire.encode_command(wire.CMD_RESET, bytes([flags, sleep]))


def cmd_register_read(addr: int, length: int) -> bytes:
    data = bytes([0x00, addr & 0xFF, (addr >> 8) & 0xFF, length & 0xFF, 0x00])
    return wire.encode_command(wire.CMD_READ_SENSOR_REG, data)


def cmd_register_write(addr: int, value: int) -> bytes:
    data = bytes(
        [
            0x00,
            addr & 0xFF,
            (addr >> 8) & 0xFF,
            value & 0xFF,
            (value >> 8) & 0xFF,
        ]
    )
    return wire.encode_command(wire.CMD_WRITE_SENSOR_REG, data)


def cmd_firmware_version() -> bytes:
    return bytes([0xA8, 0x03, 0x00, 0x00, 0x00, 0xFF])


PREFIX01 = bytes([0x01, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x88])


def cmd_get_mcu_state(ms: int | None = None) -> bytes:
    """``af 06 00 55 ms_lo ms_hi 00 00 csum``。

    时间戳用 UTC 当天毫秒数的低 16 位，校验和走 +1 变体。注意末尾多出的
    那个 0x00 在 csum 之前，所以整包 9 字节。
    """
    if ms is None:
        now = time.time()
        ms = int((now % 86400) * 1000) & 0xFFFF
    body = bytes(
        [0xAF, 0x06, 0x00, 0x55, ms & 0xFF, (ms >> 8) & 0xFF, 0x00]
    )
    return body + bytes([0x00, payload_ts(body)])


def cmd_get_image(finger: bool) -> bytes:
    cmd = wire.CMD_GET_IMAGE_FG if finger else wire.CMD_GET_IMAGE_BG
    body = bytes([cmd, 0x03, 0x00, 0x01, 0x00, 0x00])
    return body[:5] + bytes([wire.checksum(body[:5])])


def cmd_read_otp() -> bytes:
    body = bytes([0xA6, 0x03, 0x00, 0x00, 0x00, 0x01])
    return body


def cmd_ack() -> bytes:
    return wire.encode_command(wire.CMD_ACK)
