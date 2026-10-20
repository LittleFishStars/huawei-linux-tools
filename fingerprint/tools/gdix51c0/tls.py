# SPDX-License-Identifier: GPL-2.0-or-later
"""TLS 1.2 PSK 通道。

方向容易搞反：**传感器是 TLS 客户端，主机是 TLS 服务端**。上电后 MCU 主动发
ClientHello，主机用预先部署的 PSK 完成握手，之后所有图像数据都是 TLS 应用
数据记录（content type 0x17）里的密文。

为什么不自己实现 TLS：PSK-AES128-GCM-SHA256 的密钥导出与 record 层 AEAD 都由
OpenSSL 处理更可靠，Python 的 :mod:`ssl` 已经把 ``set_psk_server_callback``
暴露出来了，直接用。

SPI 侧有两条必须照抄的行为，否则握手走不通：

1. **外框类型不同**。普通命令用 0xa0 外框，TLS 记录用 **0xb0** 外框。
2. **要修补两个畸形记录**：MCU 会发一条长度为 0 的 ChangeCipherSpec
   （``14 03 03 00 00 00``），RFC 5246 不允许，补成 ``14 03 03 00 01 01``；
   以及 ClientKeyExchange 里的 PSK identity 尾部可能被截断成 0，
   还原成 ``Client_identity``。
"""

from __future__ import annotations

import ssl
import time
from dataclasses import dataclass, field

from . import wire

PSK_IDENTITY = b"Client_identity"
CIPHER = "PSK-AES128-GCM-SHA256"

#: 握手期间单条记录的最长等待（微秒 → 秒）
BOOT_TIMEOUT = 3.0
#: 建链成功后必须发的 ack-only 命令，否则 MCU 不进入可用状态
ESTABLISHED_GRACE = 0.1

_CKE_PREFIX = bytes(
    [0x16, 0x03, 0x03, 0x00, 0x15, 0x10, 0x00, 0x00, 0x11, 0x00, 0x0F]
)


class TlsError(RuntimeError):
    pass


def patch_inbound(record: bytes) -> bytes:
    """修复 MCU 发出的两条畸形 TLS 记录。"""
    if (
        len(record) >= 6
        and record[0] == 0x14
        and record[1] == 0x03
        and record[2] == 0x03
        and record[3:6] == b"\x00\x00\x00"
    ):
        # 空 ChangeCipherSpec：长度 0 改成 1，补上一字节 0x01
        return record[:4] + b"\x01" + b"\x01" + record[6:]

    full = len(_CKE_PREFIX) + len(PSK_IDENTITY)
    if (
        len(record) >= full
        and record[: len(_CKE_PREFIX)] == _CKE_PREFIX
        and record[len(_CKE_PREFIX) : full] != PSK_IDENTITY
    ):
        return (
            record[: len(_CKE_PREFIX)]
            + PSK_IDENTITY
            + record[full:]
        )
    return record


def split_records(stream: bytes) -> list[bytes]:
    """把一段字节流按 TLS record 切开（用于调试与回落路径）。"""
    out: list[bytes] = []
    off = 0
    while off + 5 <= len(stream):
        if stream[off] not in wire.TLS_CONTENT_TYPES or stream[off + 1] != 0x03:
            break
        rec_len = (stream[off + 3] << 8) | stream[off + 4]
        if off + 5 + rec_len > len(stream):
            break
        out.append(stream[off : off + 5 + rec_len])
        off += 5 + rec_len
    return out


@dataclass
class TlsChannel:
    """把一段 :class:`ssl.SSLObject` 会话架在 SPI 包搬运之上。

    ``read_packet`` / ``write_packet`` 由调用方提供（通常是
    :class:`gdix51c0.session.Session`），本类只关心 TLS 语义。
    """

    read_packet: "callable"  # (timeout_s) -> bytes | None
    write_packet: "callable"  # (payload: bytes) -> None
    psk: bytes
    identity_hint: bytes = PSK_IDENTITY
    handshake_timeout: float = BOOT_TIMEOUT
    _ctx: ssl.SSLContext | None = field(default=None, repr=False)
    _ssl: ssl.SSLObject | None = field(default=None, repr=False)
    _incoming: object = field(default=None, repr=False)
    _outgoing: object = field(default=None, repr=False)
    _handshaken: bool = False

    # -- 建立 --

    def _psk_callback(self, _sslobj, _identity):
        return self.psk

    def _make_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        ctx.set_ciphers(CIPHER)
        ctx.verify_mode = ssl.CERT_NONE
        ctx.set_psk_server_callback(self._psk_callback, self.identity_hint.decode())
        return ctx

    def _flush_outgoing(self) -> None:
        data = self._outgoing.read()  # type: ignore[attr-defined]
        if not data:
            return
        for record in split_records(data) or ([data] if data else []):
            self.write_packet(record)

    def handshake(self) -> None:
        if self._handshaken:
            return
        self._ctx = self._make_context()
        self._incoming = ssl.MemoryBIO()
        self._outgoing = ssl.MemoryBIO()
        self._ssl = self._ctx.wrap_bio(
            self._incoming, self._outgoing, server_side=True
        )

        deadline = time.monotonic() + self.handshake_timeout
        while True:
            try:
                self._ssl.do_handshake()
                break
            except ssl.SSLWantReadError:
                self._flush_outgoing()
                if time.monotonic() > deadline:
                    raise TlsError("TLS 握手超时（等 MCU 记录）")
                record = self.read_packet(BOOT_TIMEOUT)
                if record is None:
                    raise TlsError("TLS 握手期间 SPI 读超时")
                self._incoming.write(patch_inbound(record))  # type: ignore[attr-defined]
            except ssl.SSLError as exc:
                raise TlsError(f"TLS 握手失败: {exc}") from exc

        self._flush_outgoing()
        self._handshaken = True

        # MCU 只有在收到这条 ack-only 命令后才认为 TLS 通道可用
        time.sleep(ESTABLISHED_GRACE)
        self.write_packet(wire.encode_packet(wire.OUTER_WRITE, wire.CMD_TLS_ESTABLISHED))

    # -- 收发 --

    def send(self, plaintext: bytes) -> None:
        if not self._handshaken:
            raise TlsError("TLS 尚未握手")
        self._ssl.write(plaintext)  # type: ignore[union-attr]
        self._flush_outgoing()

    def recv_record(self, timeout: float = 1.0) -> bytes:
        """读一条完整的 SPI 包（应当是 TLS 记录），返回原始字节。"""
        record = self.read_packet(timeout)
        if record is None:
            raise TlsError(f"等 TLS 记录超时（{timeout * 1000:.0f} ms）")
        if not wire.tls_record_header_ok(record):
            raise TlsError(f"不是合法的 TLS 记录: {record[:8].hex()}")
        if record[0] != wire.TLS_APP_DATA:
            raise TlsError(
                f"期望应用数据 0x17，收到 content type 0x{record[0]:02x}"
            )
        return record

    def decrypt(self, record: bytes) -> bytes:
        """把一条 TLS 记录喂给 OpenSSL，返回解密后的明文。"""
        if not self._handshaken:
            raise TlsError("TLS 尚未握手")
        self._incoming.write(record)  # type: ignore[attr-defined]
        chunks: list[bytes] = []
        while True:
            try:
                chunk = self._ssl.read(16384)  # type: ignore[union-attr]
            except ssl.SSLWantReadError:
                break
            except ssl.SSLError as exc:
                raise TlsError(f"TLS 解密失败: {exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
        # 解密过程中可能产生会话票据之类的记录，一并冲出去
        self._flush_outgoing()
        if not chunks:
            raise TlsError("记录解密后没有明文（可能不是应用数据）")
        return b"".join(chunks)

    def recv_image(self, timeout: float = 1.0) -> bytes:
        return self.decrypt(self.recv_record(timeout))
