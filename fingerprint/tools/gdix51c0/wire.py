# SPDX-License-Identifier: GPL-2.0-or-later
"""Goodix 线协议组帧。

传感器在 SPI 上看到的结构永远是两段：

    外框 4 字节:  type(1) | len(1) | len(1) | csum
    载荷 len 字节: cmd(1) | inner_len(2) | data... | csum

其中 ``csum = (0xaa - sum(前面所有字节)) & 0xff``。

外框的 type 是搬运类别（0xa0 = 主机写、0xb0 = 主机读/ack），载荷的 type
才是真正的命令号。inner_len 包含末尾的校验和，所以 ``inner_len = len(data) + 1``。

这套编码与 libfprint-goodix-spi 的 goodix-wire.c 逐字节兼容（LGPL-2.1-or-later，
本文件为独立重写）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

OUTER_WRITE = 0xA0
OUTER_READ = 0xB0
OUTER_HEADER_SIZE = 4
COMMAND_HEADER_SIZE = 3
COMMAND_OVERHEAD = 4  # cmd + len(2) + csum
NULL_CHECKSUM = 0x88


class WireError(ValueError):
    """组帧/解帧错误。"""


def checksum(data: bytes) -> int:
    """``(0xaa - sum(data)) & 0xff``，和传感器的算法一致。"""
    return (0xAA - sum(data)) & 0xFF


def encode_outer_header(outer_type: int, payload_len: int) -> bytes:
    if not 0 <= payload_len <= 0xFFFF:
        raise WireError(f"载荷长度越界: {payload_len}")
    lo = payload_len & 0xFF
    hi = (payload_len >> 8) & 0xFF
    return bytes([outer_type & 0xFF, lo, hi, (outer_type + lo + hi) & 0xFF])


def decode_outer_header(header: bytes) -> tuple[int, int]:
    if len(header) < OUTER_HEADER_SIZE:
        raise WireError("外框不足 4 字节")
    outer_type = header[0]
    payload_len = header[1] | (header[2] << 8)
    expect = (outer_type + header[1] + header[2]) & 0xFF
    if header[3] != expect:
        raise WireError(
            f"外框校验失败: got 0x{header[3]:02x} want 0x{expect:02x} "
            f"({header.hex()})"
        )
    return outer_type, payload_len


def header_checksum_ok(header: bytes) -> bool:
    try:
        decode_outer_header(header)
    except WireError:
        return False
    return True


def encode_command(cmd: int, data: bytes = b"") -> bytes:
    """把 cmd + data 组成载荷（不含外框）。"""
    if len(data) > 0xFFFE:
        raise WireError("data 过长")
    inner_len = len(data) + 1
    body = bytes([cmd & 0xFF, inner_len & 0xFF, (inner_len >> 8) & 0xFF]) + data
    return body + bytes([checksum(body)])


def encode_packet(outer_type: int, cmd: int, data: bytes = b"") -> bytes:
    """完整的一帧：外框 + 载荷。"""
    payload = encode_command(cmd, data)
    return encode_outer_header(outer_type, len(payload)) + payload


@dataclass(frozen=True)
class Command:
    cmd: int
    data: bytes
    raw: bytes

    @property
    def short(self) -> str:
        return f"cmd=0x{self.cmd:02x} len={len(self.data)}"


def decode_command(packet: bytes, allow_null_checksum: bool = True) -> Command:
    """解一帧载荷（外框已剥掉）。"""
    if len(packet) < COMMAND_OVERHEAD:
        raise WireError(f"载荷太短: {len(packet)}")
    cmd = packet[0]
    inner_len = packet[1] | (packet[2] << 8)
    if inner_len < 1:
        raise WireError("inner_len 非法")
    total = COMMAND_HEADER_SIZE + inner_len
    if len(packet) < total:
        raise WireError(f"载荷被截断: 需要 {total} 实际 {len(packet)}")
    want = checksum(packet[: total - 1])
    got = packet[total - 1]
    if got != want and not (allow_null_checksum and got == NULL_CHECKSUM):
        raise WireError(
            f"载荷校验失败: got 0x{got:02x} want 0x{want:02x} ({packet[:total].hex()})"
        )
    return Command(cmd=cmd, data=bytes(packet[COMMAND_HEADER_SIZE : total - 1]), raw=bytes(packet[:total]))


def iter_frames(stream: bytes) -> Iterator[tuple[int, bytes]]:
    """从一个连续字节流里切出 (outer_type, payload) 序列。

    用于离线分析抓包文件：容错地跳过不合法的区间。
    """
    off = 0
    n = len(stream)
    while off + OUTER_HEADER_SIZE <= n:
        hdr = stream[off : off + OUTER_HEADER_SIZE]
        try:
            outer_type, plen = decode_outer_header(hdr)
        except WireError:
            off += 1
            continue
        if outer_type not in (OUTER_WRITE, OUTER_READ, 0x00, 0x01):
            off += 1
            continue
        end = off + OUTER_HEADER_SIZE + plen
        if end > n:
            break
        yield outer_type, stream[off + OUTER_HEADER_SIZE : end]
        off = end


# ---- 常用命令号（来自官方驱动与社区抓包）--------------------------------

CMD_NOP = 0x00
CMD_GET_IMAGE_BG = 0x20
CMD_GET_IMAGE_FG = 0x22
CMD_FDT_DOWN = 0x32
CMD_FDT_UP = 0x34
CMD_FDT_MODE = 0x36
CMD_NAV = 0x50
CMD_IDLE = 0x70
CMD_WRITE_SENSOR_REG = 0x80
CMD_READ_SENSOR_REG = 0x82
CMD_UPLOAD_CONFIG = 0x90
CMD_SET_DAC = 0x98
CMD_POWERDOWN_FREQ = 0x94
CMD_ENABLE_CHIP = 0x96
CMD_RESET = 0xA2
CMD_READ_OTP = 0xA6
CMD_FIRMWARE_VERSION = 0xA8
CMD_QUERY_MCU_STATE = 0xAE
CMD_GET_MCU_STATE = 0xAF
CMD_ACK = 0xB0
CMD_REQUEST_TLS = 0xD0
CMD_TLS_ESTABLISHED = 0xD4
CMD_PRESET_PSK_WRITE = 0xE0
CMD_READ = 0xE4

#: TLS 应用数据记录的 content type，出现在载荷首字节时表示这是图像数据
TLS_APP_DATA = 0x17
TLS_CONTENT_TYPES = (0x14, 0x15, 0x16, 0x17)


def tls_record_header_ok(record: bytes) -> bool:
    """检查一段字节是否像一个完整的 TLS 记录。"""
    if len(record) < 5:
        return False
    if record[0] not in TLS_CONTENT_TYPES:
        return False
    if record[1] != 0x03:
        return False
    rec_len = (record[3] << 8) | record[4]
    return 5 + rec_len <= len(record)
