#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ntag_id.py —— NTAG0001（i2c-0 @ 0x57）只读识别工具

上一轮的事实：
  * 从地址 0x57（ACPI _CRS 里声明的），irq 213，下降沿
  * 4 种 NCI 组帧全部失败：地址 ACK 了，但随后传输 -110(ETIMEDOUT)，总线被拉住
  * 0x57 不是任何已知 NCI 控制器的地址（NXP NCI 系列一律 0x28）

新假设（由驱动内部证据反推）：
  * 驱动里到处是 **16 字节块**：`FUN_140004914` 按 0x10 切块、上限 0x32(=50=3×16+2)、
    完整帧 0x12(=18=2+16)、SRAM 64 字节 = 4×16
  * 这是 **NFC Forum Type 2 Tag + I2C 接口**（NTAG I2C plus / NTAG 5 link / ST25DV / M24SR）
    的典型访问粒度 —— 2 字节内存地址 + 16 字节页
  * 它对 NCI 命令当然不会有任何回应

本工具 **只读**，绝不写数据，只写「地址指针」（2 字节），这是读操作的必要部分。
判定特征：
  * 读到 0x0000 起 16 字节，若 data[0] == 0x04 → NXP（NTAG 系列）
  * data[12:16] == E1 10 .. .. → Type 2 Tag 的 CC（Capability Container）
  * 两个都命中 → 100% 是 NXP 的 NFC 标签芯片（NTAG I2C plus / NTAG 5）

用法：
    sudo python3 ntag_id.py                  # 自动：地址 0x57，总线自动
    sudo python3 ntag_id.py --addr-bytes 0000 --read 16
    sudo python3 ntag_id.py --framings       # 逐个组帧试一遍
"""

import argparse
import ctypes
import fcntl
import os
import re
import sys
import time

I2C_SLAVE = 0x0703
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001

ADDR_DEFAULT = 0x57


class I2cMsg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class I2cRdwrData(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(I2cMsg)), ("nmsgs", ctypes.c_uint32)]


def guess_bus():
    p = "/sys/bus/i2c/devices/i2c-NTAG0001:00"
    m = re.search(r"-(\d+)$", os.path.realpath(p))
    return int(m.group(1)) if m else 0


class Bus:
    def __init__(self, nr, addr):
        self.fd = os.open("/dev/i2c-%d" % nr, os.O_RDWR)
        self.addr = addr
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)

    def xfer(self, write=None, read_len=0):
        msgs, keep = [], []
        if write:
            wb = (ctypes.c_uint8 * len(write))(*write)
            keep.append(wb)
            msgs.append(I2cMsg(self.addr, 0, len(write), wb))
        if read_len:
            rb = (ctypes.c_uint8 * read_len)()
            keep.append(rb)
            msgs.append(I2cMsg(self.addr, I2C_M_RD, read_len, rb))
        if not msgs:
            return b""
        arr = (I2cMsg * len(msgs))(*msgs)
        fcntl.ioctl(self.fd, I2C_RDWR, I2cRdwrData(arr, len(msgs)))
        return bytes(bytearray(keep[-1])) if read_len else b""

    def close(self):
        os.close(self.fd)


def hexdump(data):
    out = []
    for i in range(0, len(data), 8):
        chunk = data[i:i + 8]
        out.append("%04x  %s" % (i, " ".join("%02x" % b for b in chunk)))
    return "\n".join(out)


def errno_hint(e):
    n = getattr(e, "errno", None)
    if n in (110,):
        return "-110 ETIMEDOUT：地址 ACK 了但传输被拉住（时钟拉伸/SDA 卡死）。" \
               "→ 先复位 I2C 控制器再试（见文末）"
    if n in (121, 6, 70):
        return "%-4s 从地址没人应答（%s）→ 地址不对或芯片没上电" % (n, e.strerror)
    return "%s %s" % (n, getattr(e, "strerror", e))


def try_framing(bus, name, addr_bytes, read_len):
    try:
        bus.xfer(write=bytes(addr_bytes), read_len=0)
        rx = bus.xfer(read_len=read_len)
        print("\n[%s] 写地址 %s → 读 %d 字节 OK" % (name, bytes(addr_bytes).hex(" "), read_len))
        print(hexdump(rx))
        return rx
    except OSError as e:
        print("\n[%s] 失败: %s" % (name, errno_hint(e)))
        return None


def interpret(rx):
    if not rx:
        return
    cc = rx[12:16] if len(rx) >= 16 else b""
    print("\n--- 判读 ---")
    print("首字节 data[0] = 0x%02x   （NXP 的 UID 以 0x04 开头，ST 的以 0x02 开头）" % rx[0])
    if len(rx) >= 16:
        print("data[12:16] = %s  （Type 2 Tag 的 CC 应为 E1 10 .. ..）" % cc.hex(" "))
    if rx[0] == 0x04 and cc[:2] == b"\xe1\x10":
        print(">>> 命中：NXP 的 NFC Forum Type 2 Tag（NTAG I2C plus / NTAG 5 link 一类）")
        print("    UID = %s（后 4 字节在下一页 0x0004 起）" % rx[:3].hex(" "))
        print("    即：这颗芯片不是 NCI 控制器，走 NCI 永远不会有回应；")
        print("    华为的「碰一碰」就是手机当作读卡器去读这颗标签，PC 侧靠 I2C 读 SRAM/EEPROM。")
    elif rx[0] == 0x02:
        print(">>> 像是 ST 的 Type 2 Tag（ST25DV / M24SR 系列，I2C 地址 0x53-0x57 也吻合）")
    elif rx[:2] == b"\xe1\x10":
        print(">>> 读到的是 CC（说明地址指针落到了 block 3），确实是 Type 2 Tag")
    else:
        print(">>> 不像标准 Type 2 Tag 的内存映射，可能组帧不对（换 --framings 逐个试）")


def main():
    ap = argparse.ArgumentParser(description="NTAG0001 只读识别")
    ap.add_argument("--bus", type=int, default=None)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=ADDR_DEFAULT)
    ap.add_argument("--read", type=int, default=16)
    ap.add_argument("--addr-bytes", default=None,
                    help="手动指定地址指针字节（十六进制），如 0000")
    ap.add_argument("--framings", action="store_true", help="逐个组帧试一遍")
    args = ap.parse_args()

    busnr = args.bus if args.bus is not None else guess_bus()
    print("总线 /dev/i2c-%d，从地址 0x%02x" % (busnr, args.addr))
    try:
        bus = Bus(busnr, args.addr)
    except OSError as e:
        sys.exit("打不开总线: %s" % e)

    if args.addr_bytes:
        ab = bytes.fromhex(args.addr_bytes.replace(" ", ""))
        rx = try_framing(bus, "手动 %s" % args.addr_bytes, ab, args.read)
        interpret(rx)
        bus.close()
        return

    # 默认：先试最常见的 16 位大端地址 @0x0000（Type 2 Tag / NTAG I2C / ST25DV 都用这个）
    rx = try_framing(bus, "16 位地址 @0x0000", [0x00, 0x00], args.read)
    interpret(rx)
    if rx:
        bus.close()
        return

    if args.framings:
        for name, ab in [
            ("16 位地址 @0x0004", [0x00, 0x04]),
            ("8 位地址 @0x00", [0x00]),
            ("无地址", []),
            ("16 位地址 @0x0F8（SRAM 区）", [0x00, 0xF8]),
        ]:
            if not ab:
                try:
                    rx = bus.xfer(read_len=args.read)
                    print("\n[%s] 读 %d 字节 OK" % (name, args.read))
                    print(hexdump(rx))
                    interpret(rx)
                except OSError as e:
                    print("\n[%s] 失败: %s" % (name, errno_hint(e)))
                continue
            rx = try_framing(bus, name, ab, args.read)
            interpret(rx)

    bus.close()
    print("""
若全部是 -110(ETIMEDOUT)：总线被上一轮的非法写入卡住了，先复位控制器：
    sudo sh -c 'echo 0000:00:15.0 > /sys/bus/pci/drivers/i2c_designware_pci/unbind; \\
                sleep 1; \\
                echo 0000:00:15.0 > /sys/bus/pci/drivers/i2c_designware_pci/bind'
（驱动目录名用 ls -l /sys/bus/pci/devices/0000:00:15.0/driver 确认）
然后重新跑本脚本。
""")


if __name__ == "__main__":
    main()
