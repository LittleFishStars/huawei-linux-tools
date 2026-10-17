# SPDX-License-Identifier: GPL-2.0-or-later
"""``/dev/gdix51c0`` 字符设备的 Python 封装。

只用 ctypes + fcntl，不依赖第三方库。ABI 必须与 driver/gdix51c0_fp.c 里的
结构体与 ioctl 编号保持一致，改动时两处同步。
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import struct
from dataclasses import dataclass

from . import DEVICE_NODE

# ---- ioctl 编号（与驱动一致）------------------------------------------

IOC_NRBITS = 8
IOC_TYPEBITS = 8
IOC_SIZEBITS = 14
IOC_DIRBITS = 2
IOC_NRSHIFT = 0
IOC_TYPESHIFT = IOC_NRSHIFT + IOC_NRBITS
IOC_SIZESHIFT = IOC_TYPESHIFT + IOC_TYPEBITS
IOC_DIRSHIFT = IOC_SIZESHIFT + IOC_SIZEBITS
IOC_NONE, IOC_WRITE, IOC_READ = 0, 1, 2

MAGIC = 0xD1


def _IOC(direction: int, nr: int, size: int) -> int:
    return (
        (direction << IOC_DIRSHIFT)
        | (size << IOC_SIZESHIFT)
        | (MAGIC << IOC_TYPESHIFT)
        | (nr << IOC_NRSHIFT)
    )


class _XferReq(ctypes.Structure):
    _fields_ = [
        ("tx_len", ctypes.c_uint32),
        ("rx_len", ctypes.c_uint32),
        ("speed_hz", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("tx_buf", ctypes.c_uint64),
        ("rx_buf", ctypes.c_uint64),
    ]


class _Info(ctypes.Structure):
    _fields_ = [
        ("hid", ctypes.c_char * 16),
        ("irq", ctypes.c_uint32),
        ("reset_valid", ctypes.c_uint32),
        ("reset_active_low", ctypes.c_uint32),
        ("speed_hz", ctypes.c_uint32),
        ("chunk", ctypes.c_uint32),
        ("irq_gpio_valid", ctypes.c_uint32),
    ]


_XFER_SIZE = ctypes.sizeof(_XferReq)
_INFO_SIZE = ctypes.sizeof(_Info)

IOC_XFER = _IOC(IOC_READ | IOC_WRITE, 0x01, _XFER_SIZE)
IOC_RESET = _IOC(IOC_WRITE, 0x02, ctypes.sizeof(ctypes.c_uint32))
IOC_INFO = _IOC(IOC_READ, 0x03, _INFO_SIZE)
IOC_IRQ_COUNT = _IOC(IOC_READ, 0x04, ctypes.sizeof(ctypes.c_uint64))
IOC_IRQ_LEVEL = _IOC(IOC_READ, 0x05, ctypes.sizeof(ctypes.c_uint32))
IOC_IRQ_WAIT = _IOC(IOC_READ | IOC_WRITE, 0x06, ctypes.sizeof(ctypes.c_uint32))


@dataclass(frozen=True)
class DeviceInfo:
    hid: str
    irq: int
    reset_valid: bool
    reset_active_low: bool
    speed_hz: int
    chunk: int
    irq_gpio_valid: bool

    def __str__(self) -> str:  # pragma: no cover - 仅用于打印
        return (
            f"hid={self.hid} irq={self.irq} "
            f"reset={'yes' if self.reset_valid else 'no'} "
            f"irq_gpio={'yes' if self.irq_gpio_valid else 'no'} "
            f"speed={self.speed_hz} chunk={self.chunk}"
        )


class TransportError(OSError):
    """设备层错误。"""


class KernelDevice:
    """驱动暴露的字符设备。

    线程安全性：驱动内部用 mutex 串行化 SPI 传输，但 IRQ 计数是独立的，
    因此多线程同时 xfer 在字节层面是安全的，协议层面不安全（调用方自己
    保证一次命令-响应是原子的）。本类不做额外加锁。
    """

    def __init__(self, path: str = DEVICE_NODE, open_now: bool = True):
        self.path = path
        self._fd = -1
        if open_now:
            self.open()

    # -- 生命周期 --

    def open(self) -> None:
        if self._fd >= 0:
            return
        try:
            self._fd = os.open(self.path, os.O_RDWR)
        except FileNotFoundError as exc:
            raise TransportError(
                f"{self.path} 不存在：先 insmod driver/gdix51c0_fp.ko"
            ) from exc
        except PermissionError as exc:
            raise TransportError(
                f"{self.path} 需要 root（模块把权限设为 0600）"
            ) from exc

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> "KernelDevice":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @property
    def fd(self) -> int:
        if self._fd < 0:
            self.open()
        return self._fd

    # -- ioctl 封装 --

    def info(self) -> DeviceInfo:
        raw = ctypes.create_string_buffer(_INFO_SIZE)
        fcntl.ioctl(self.fd, IOC_INFO, raw, True)
        inf = _Info.from_buffer_copy(raw)
        return DeviceInfo(
            hid=inf.hid.decode("ascii", "replace").rstrip("\x00"),
            irq=inf.irq,
            reset_valid=bool(inf.reset_valid),
            reset_active_low=bool(inf.reset_active_low),
            speed_hz=inf.speed_hz,
            chunk=inf.chunk,
            irq_gpio_valid=bool(inf.irq_gpio_valid),
        )

    def xfer(self, tx: bytes, rx_len: int | None = None, speed_hz: int = 0) -> bytes:
        """全双工一次搬运。

        tx 走 MOSI；MISO 侧回来 max(len(tx), rx_len) 字节。rx_len 缺省等于
        len(tx)，此时返回值与 tx 等长（经典 SPI 全双工语义）。rx_len 大于
        len(tx) 时，多出来的时钟周期里 MOSI 由驱动填 0xff。
        """
        total = max(len(tx), rx_len or 0)
        if total == 0:
            return b""

        tx_buf = ctypes.create_string_buffer(tx, len(tx)) if tx else None
        rx_buf = ctypes.create_string_buffer(total)

        req = _XferReq(
            tx_len=len(tx),
            rx_len=total,
            speed_hz=speed_hz,
            flags=0,
            tx_buf=ctypes.addressof(tx_buf) if tx_buf else 0,
            rx_buf=ctypes.addressof(rx_buf),
        )
        fcntl.ioctl(self.fd, IOC_XFER, req, True)
        return rx_buf.raw[:total]

    def reset(self, hold_us: int = 10_000) -> None:
        fcntl.ioctl(self.fd, IOC_RESET, struct.pack("<I", hold_us))

    def irq_count(self) -> int:
        raw = ctypes.create_string_buffer(8)
        fcntl.ioctl(self.fd, IOC_IRQ_COUNT, raw, True)
        return struct.unpack("<Q", raw.raw)[0]

    def irq_level(self) -> int:
        raw = ctypes.create_string_buffer(4)
        fcntl.ioctl(self.fd, IOC_IRQ_LEVEL, raw, True)
        return struct.unpack("<I", raw.raw)[0]

    def irq_wait(self, timeout_ms: int) -> int | None:
        """等下一个中断边沿。

        返回新的累计计数；超时返回 None。语义是"消费一个计数"，与驱动的
        irq_served 对齐——因此调用方拿到 None 时不能假定硬件没动作。
        """
        raw = ctypes.create_string_buffer(struct.pack("<I", timeout_ms))
        try:
            fcntl.ioctl(self.fd, IOC_IRQ_WAIT, raw, True)
        except TimeoutError:
            return None
        return struct.unpack("<I", raw.raw)[0]


def probe() -> DeviceInfo:
    """不建立持久句柄地探一次设备。"""
    with KernelDevice() as dev:
        return dev.info()
