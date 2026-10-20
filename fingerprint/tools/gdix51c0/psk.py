# SPDX-License-Identifier: GPL-2.0-or-later
"""PSK 的 white-box 封装。

传感器不接受裸的 32 字节 TLS 预共享密钥，只接受 102 字节的 white-box 容器，
写入寄存器 ``0xbb010003``。容器布局（逆向自 gfspi.dll 的 whitebox_wrap）::

    front(32) | hdr(2) | len(4) | h16(16) | ct(32) | tag(16)

    hdr    = 02 ff
    len    = uint32_le(32)
    h16    = SHA256(hdr | len | psk[0:8] | (03 00 00 00) × 16)[0:16]
    ct,tag = AES-256-GCM(KEY, iv=h16, aad=AAD, pt=psk)
    front  = HMAC-SHA256(HMAC_KEY, hdr | len | ct | tag)

KEY 与 HMAC_KEY 从同一段 48 字节常量里取（KEY = kbuf[0:32]，
HMAC_KEY = kbuf[16:48]，两者重叠 16 字节）。

依赖 cryptography（AES-GCM）与 hashlib（SHA-256 / HMAC），都是标准 Python 生态。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct

PSK_LEN = 32
WB_LEN = 102

WB_ADDR = 0xBB010003
WB_HASH_ADDR = 0xBB020003

_KBUF = bytes(
    [
        0x58, 0xF0, 0x13, 0xEE, 0xB7, 0xE2, 0x16, 0xE2,
        0xC1, 0xCD, 0x0E, 0x8F, 0xFF, 0xA7, 0xDB, 0xD6,
        0x79, 0x9C, 0xD9, 0x2A, 0xA1, 0x49, 0x01, 0x3E,
        0xA0, 0xE9, 0xF9, 0xFD, 0x7D, 0xC6, 0xD9, 0x4E,
        0x3E, 0xF6, 0x3A, 0x75, 0x98, 0xC2, 0xA4, 0x93,
        0x31, 0x29, 0xE8, 0x71, 0xEA, 0x03, 0x04, 0x3B,
    ]
)
_AAD = bytes(
    [
        0x52, 0x2D, 0xC1, 0xF0, 0x99, 0x56, 0x7D, 0x07,
        0xF4, 0x7F, 0x37, 0xA3, 0x2A, 0x84, 0x42, 0x7D,
    ]
)

KEY = _KBUF[0:32]
HMAC_KEY = _KBUF[16:48]

OFF_FRONT, OFF_HDR, OFF_LEN = 0x00, 0x20, 0x22
OFF_H16, OFF_CT, OFF_TAG = 0x26, 0x36, 0x56

HDR = b"\x02\xff"


class PskError(ValueError):
    pass


def _aes_gcm_encrypt(pt: bytes) -> tuple[bytes, bytes]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    iv = _binding_hash(pt[:8])
    blob = AESGCM(KEY).encrypt(iv, pt, _AAD)
    return blob[:-16], blob[-16:]


def _aes_gcm_decrypt(iv: bytes, ct: bytes, tag: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(KEY).decrypt(iv, ct + tag, _AAD)


def _binding_hash(psk_prefix: bytes) -> bytes:
    """h16：把 PSK 前 8 字节绑进 IV，使容器无法在 PSK 之间交叉使用。"""
    return hashlib.sha256(
        HDR + struct.pack("<I", 32) + psk_prefix + b"\x03\x00\x00\x00" * 16
    ).digest()[:16]


def wrap(psk: bytes) -> bytes:
    """32 字节 PSK → 102 字节 white-box 容器。"""
    if len(psk) != PSK_LEN:
        raise PskError(f"PSK 必须是 {PSK_LEN} 字节，收到 {len(psk)}")
    iv = _binding_hash(psk[:8])
    ct, tag = _aes_gcm_encrypt(psk)
    len_field = struct.pack("<I", 32)
    front = hmac.new(HMAC_KEY, HDR + len_field + ct + tag, hashlib.sha256).digest()
    out = front + HDR + len_field + iv + ct + tag
    if len(out) != WB_LEN:  # pragma: no cover - 结构性断言
        raise PskError(f"容器长度异常: {len(out)}")
    return out


def unwrap(wb: bytes) -> bytes:
    """102 字节容器 → 32 字节 PSK，全部认证都过才返回。"""
    if len(wb) != WB_LEN:
        raise PskError(f"容器必须是 {WB_LEN} 字节，收到 {len(wb)}")
    front = wb[OFF_FRONT:OFF_FRONT + 32]
    hdr = wb[OFF_HDR:OFF_LEN]
    len_field = wb[OFF_LEN:OFF_H16]
    iv = wb[OFF_H16:OFF_CT]
    ct = wb[OFF_CT:OFF_TAG]
    tag = wb[OFF_TAG:OFF_TAG + 16]

    if hdr != HDR:
        raise PskError(f"头部不是 02 ff，而是 {hdr.hex()}")
    if struct.unpack("<I", len_field)[0] != 32:
        raise PskError("长度字段不是 32")

    want = hmac.new(HMAC_KEY, hdr + len_field + ct + tag, hashlib.sha256).digest()
    if not hmac.compare_digest(front, want):
        raise PskError("front HMAC 校验失败")

    try:
        psk = _aes_gcm_decrypt(iv, ct, tag)
    except Exception as exc:  # cryptography 抛 InvalidTag
        raise PskError(f"GCM 认证失败: {exc}") from exc

    if not hmac.compare_digest(iv, _binding_hash(psk[:8])):
        raise PskError("h16 绑定校验失败")
    return psk


def wb_hash(wb: bytes) -> bytes:
    """MCU 在 0xbb020003 里存的就是 SHA-256(容器)，用来验证写入是否生效。"""
    return hashlib.sha256(wb).digest()


def provision_payload(psk: bytes) -> bytes:
    """构造 0xe0 命令的 data：addr(LE32) | len(LE32) | WB(102) | 4 字节对齐填充。"""
    wb = wrap(psk)
    body = struct.pack("<I", WB_ADDR) + struct.pack("<I", len(wb)) + wb
    pad = (-len(body)) % 4
    return body + b"\x00" * pad


def load_state(path: str) -> bytes | None:
    """从状态文件读 PSK（十六进制文本，权限 0600）。"""
    try:
        with open(path, "r", encoding="ascii") as fh:
            text = fh.read().strip()
    except FileNotFoundError:
        return None
    raw = bytes.fromhex(text)
    if len(raw) != PSK_LEN:
        raise PskError(f"{path} 内容不是 {PSK_LEN} 字节的十六进制")
    return raw


def save_state(path: str, psk: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, psk.hex().encode("ascii") + b"\n")
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def random_psk() -> bytes:
    return os.urandom(PSK_LEN)


def selftest() -> None:
    """往返自检：wrap → unwrap 必须拿回原值，且篡改必须被拒。"""
    psk = bytes(range(32))
    wb = wrap(psk)
    assert unwrap(wb) == psk, "往返失败"
    bad = bytearray(wb)
    bad[OFF_CT] ^= 0x01
    try:
        unwrap(bytes(bad))
    except PskError:
        return
    raise AssertionError("篡改后的容器没有被拒绝")
