#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ntag_probe.py —— 华为 NTAG0001 内置 NFC（I2C）用户态探测 / 调试工具

背景
    华为 MateBook X Pro 2023 (MRGF-XX) 的内置 NFC 控制器是一个 I2C 从设备：
      * ACPI 设备    : NTAG0001            (Windows 侧 INF 用 ACPI\\NTAG0001 绑定)
      * I2C 总线     : i2c-0  (Intel LPSS DesignWare, PCI 0000:00:15.0)
      * 中断         : _CRS 里的 GpioInt
      * 内核已创建   : /sys/bus/i2c/devices/i2c-NTAG0001:00  (从地址来自 ACPI，未绑定驱动)
    Windows 驱动 SpbNfcDriver.sys 只是个搬运工 + 华为私有控制协议，不实现 NFC 标准协议，
    所以 Linux 侧最快的路线是：直接按 NFC Forum NCI 去问芯片。

这个脚本做三件事
    1. scan        在总线上扫哪些从地址有应答（找出芯片地址的兜底办法）
    2. reset       按几种常见 NCI-over-I2C 帧格式发 CORE_RESET_CMD，打印原始应答
    3. raw         原始写/读，用于手搓协议

    (更推荐用内核模块 ntag_nfc.ko：它会直接打印 ACPI 里声明的从地址/中断号，
     并创建 /dev/ntag_nfc；本脚本是它的用户态等价物与调试补充。)

用法
    sudo python3 ntag_probe.py scan
    sudo python3 ntag_probe.py reset
    sudo python3 ntag_probe.py raw --write 20000101 --read 16
    sudo python3 ntag_probe.py info

权限
    /dev/i2c-* 属于 root:i2c 权限 0660，需要 root 或把用户加入 i2c 组。
"""

import argparse
import ctypes
import fcntl
import glob
import os
import re
import struct
import sys
import time

I2C_SLAVE = 0x0703
I2C_FUNCS = 0x0705
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001
I2C_FUNC_I2C = 0x00000001

NTAG_ACPI_DIR = "/sys/bus/i2c/devices/i2c-NTAG0001:00"
NTAG_MODULE_INFO = [
    "/sys/bus/i2c/devices/i2c-NTAG0001:00/ntag_nfc/info",
    "/sys/devices/platform/NTAG0001:00/ntag_nfc/info",
]


class I2cMsg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class I2cRdwrData(ctypes.Structure):
    _fields_ = [
        ("msgs", ctypes.POINTER(I2cMsg)),
        ("nmsgs", ctypes.c_uint32),
    ]


def guess_bus():
    """从 sysfs 里找出 NTAG0001 挂在哪个 i2c 总线上。"""
    link = os.path.join(NTAG_ACPI_DIR, "name")
    if os.path.exists(link):
        m = re.search(r"-(\d+)$", os.path.realpath(NTAG_ACPI_DIR))
        if m:
            return int(m.group(1))
    # 兜底：从 physical_node 反查
    for p in glob.glob("/sys/bus/i2c/devices/i2c-*/name"):
        try:
            if open(p).read().strip().startswith("NTAG"):
                m = re.search(r"i2c-(\d+)", p)
                if m:
                    return int(m.group(1))
        except OSError:
            pass
    return None


def guess_addr():
    """内核模块加载过的话，从它的 sysfs 里直接读出 ACPI 声明的从地址。"""
    for p in NTAG_MODULE_INFO:
        try:
            txt = open(p).read()
        except OSError:
            continue
        m = re.search(r"i2c_addr\s*:\s*(0x[0-9a-fA-F]+)", txt)
        if m:
            return int(m.group(1), 16)
    return None


class Bus:
    def __init__(self, busnr, addr):
        self.path = "/dev/i2c-%d" % busnr
        self.fd = os.open(self.path, os.O_RDWR)
        self.addr = addr
        fcntl.ioctl(self.fd, I2C_SLAVE, addr)

    def funcs(self):
        buf = ctypes.create_string_buffer(8)
        fcntl.ioctl(self.fd, I2C_FUNCS, buf)
        return struct.unpack("<Q", buf.raw[:8])[0]

    def xfer(self, write=None, read_len=0, addr=None):
        """一次原子传输：可选先写，再读。返回读到的 bytes。"""
        a = self.addr if addr is None else addr
        msgs = []
        keep = []
        if write:
            wb = (ctypes.c_uint8 * len(write))(*write)
            keep.append(wb)
            m = I2cMsg(a, 0, len(write), wb)
            msgs.append(m)
        if read_len:
            rb = (ctypes.c_uint8 * read_len)()
            keep.append(rb)
            m = I2cMsg(a, I2C_M_RD, read_len, rb)
            msgs.append(m)
        if not msgs:
            return b""
        arr = (I2cMsg * len(msgs))(*msgs)
        data = I2cRdwrData(arr, len(msgs))
        fcntl.ioctl(self.fd, I2C_RDWR, data)
        if read_len:
            return bytes(bytearray(keep[-1]))
        return b""

    def close(self):
        os.close(self.fd)


def hexs(b):
    return " ".join("%02x" % x for x in b)


def cmd_info(args):
    print("ACPI 设备目录 :", NTAG_ACPI_DIR, "存在" if os.path.isdir(NTAG_ACPI_DIR) else "不存在")
    bus = args.bus if args.bus is not None else guess_bus()
    addr = guess_addr()
    print("推测 I2C 总线 :", bus)
    print("ACPI 从地址   :", ("0x%02x" % addr) if addr else "未知(加载 ntag_nfc.ko 后可读到)")
    try:
        print("i2c client    :", open(os.path.join(NTAG_ACPI_DIR, "uevent")).read().strip() or "(空)")
    except OSError:
        pass
    for p in glob.glob("/sys/bus/acpi/devices/NTAG0001:00/*"):
        print("  acpi sysfs  :", os.path.basename(p))
    for p in NTAG_MODULE_INFO:
        try:
            print("--- %s ---" % p)
            print(open(p).read())
        except OSError:
            continue
    print("提示: 从地址与 GpioInt 中断号都写在 ACPI _CRS 里；")
    print("      执行 `sudo insmod ntag_nfc.ko` 后看 dmesg，或 `sudo acpidump -o acpi.bin` 再看 DSDT。")


def cmd_scan(args):
    bus = args.bus if args.bus is not None else guess_bus()
    if bus is None:
        sys.exit("找不到 NTAG0001 所在总线，请用 --bus 指定")
    print("扫描 %s 上的 7-bit 地址 (0x08-0x77) ..." % bus)
    found = []
    for a in range(0x08, 0x78):
        if a in range(0x30, 0x38) or a in range(0x50, 0x58):
            pass  # 保留地址也一起试，宁可多报
        try:
            dev = Bus(bus, a)
        except OSError as e:
            sys.exit("打开总线失败: %s" % e)
        try:
            dev.xfer(read_len=1)
            found.append(a)
            print("  应答: 0x%02x" % a)
        except OSError:
            pass
        finally:
            dev.close()
    if not found:
        print("没有任何地址应答。NFC 芯片在没有数据可读时会 NAK 读操作，这属于正常现象；")
        print("用 'reset' 子命令直接对目标地址写 CORE_RESET 更有意义。")
    return found


# 常见的 NCI-over-I2C 组帧方式（厂商差异就在这几个字节上）
FRAMINGS = [
    # (名字, 写前缀生成函数(len)->bytes, 读前缀 bytes)
    ("裸写 + 裸读        ", lambda n: b"", b""),
    ("写长度字节 + 裸读  ", lambda n: bytes([n]), b""),
    ("0x00 头 + 0x00 头  ", lambda n: b"\x00", b"\x00"),
    ("长度字节双向       ", lambda n: bytes([n]), None),  # None = 读之前写长度
]


def cmd_reset(args):
    bus = args.bus if args.bus is not None else guess_bus()
    addr = args.addr if args.addr is not None else guess_addr()
    if bus is None or addr is None:
        sys.exit("缺少总线/地址：用 --bus/--addr 指定，或先 insmod ntag_nfc.ko")
    core_reset = bytes([0x20, 0x00, 0x01, 0x01])   # NCI CORE_RESET_CMD, reset config
    read_len = args.read
    print("总线 %s，从地址 0x%02x，读取长度 %d" % (bus, addr, read_len))
    for name, wpre, rpre in FRAMINGS:
        dev = Bus(bus, addr)
        try:
            w = wpre(len(core_reset)) + core_reset
            print("\n[%s] 写: %s" % (name, hexs(w)))
            dev.xfer(write=w)
        except OSError as e:
            print("  写失败: %s" % e)
            dev.close()
            continue
        time.sleep(0.06)
        try:
            if rpre is None:
                dev.xfer(write=bytes([read_len]))
            elif rpre:
                dev.xfer(write=rpre)
            rx = dev.xfer(read_len=read_len)
            print("  读: %s" % (hexs(rx) if rx else "(空)"))
            if len(rx) >= 2 and rx[0] == 0x40 and rx[1] == 0x00:
                print("  >>> 命中 NCI CORE_RESET_RSP, status=0x%02x" % (rx[3] if len(rx) > 3 else -1))
            if len(rx) >= 2 and rx[0] == 0x60 and rx[1] == 0x00:
                print("  >>> 收到 CORE_RESET_NTF（里面通常带厂商/芯片型号信息）")
        except OSError as e:
            print("  读失败: %s" % e)
        finally:
            dev.close()


def cmd_raw(args):
    bus = args.bus if args.bus is not None else guess_bus()
    addr = args.addr if args.addr is not None else guess_addr()
    if bus is None or addr is None:
        sys.exit("缺少总线/地址：用 --bus/--addr 指定，或先 insmod ntag_nfc.ko")
    w = bytes.fromhex(args.write.replace(" ", "")) if args.write else b""
    dev = Bus(bus, addr)
    try:
        if w:
            dev.xfer(write=w)
            print("已写 %d 字节: %s" % (len(w), hexs(w)))
            time.sleep(args.delay)
        if args.read:
            rx = dev.xfer(read_len=args.read)
            print("读到 %d 字节: %s" % (len(rx), hexs(rx)))
    except OSError as e:
        sys.exit("传输失败: %s" % e)
    finally:
        dev.close()


def cmd_irq(args):
    """轮询内核模块统计的中断计数（用来确认 GpioInt 真的接上了）。"""
    p = "/sys/bus/i2c/devices/i2c-NTAG0001:00/ntag_nfc/info"
    if not os.path.exists(p):
        sys.exit("请先 insmod ntag_nfc.ko（没有 %s）" % p)
    last = None
    print("监视中断计数（Ctrl-C 退出）...")
    while True:
        txt = open(p).read()
        m = re.search(r"irq_count\s*:\s*(\d+)", txt)
        n = int(m.group(1)) if m else -1
        if n != last:
            print(time.strftime("%H:%M:%S"), "irq_count =", n)
            last = n
        time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser(description="NTAG0001 内置 NFC (I2C) 探测工具")
    ap.add_argument("--bus", type=int, default=None, help="i2c 总线号，默认自动探测")
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=None, help="从地址，默认自动探测")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("info", help="打印已知的设备/总线/驱动信息")
    sub.add_parser("scan", help="扫描总线上的应答地址")
    p = sub.add_parser("reset", help="按多种 NCI 组帧发 CORE_RESET_CMD")
    p.add_argument("--read", type=int, default=32, help="读回字节数，默认 32")
    p = sub.add_parser("raw", help="原始读写")
    p.add_argument("--write", default=None, help="要写的十六进制，如 20000101")
    p.add_argument("--read", type=int, default=0, help="读回字节数")
    p.add_argument("--delay", type=float, default=0.06, help="写后等待秒数")
    sub.add_parser("irq", help="监视中断计数")

    args = ap.parse_args()
    {"info": cmd_info, "scan": cmd_scan, "reset": cmd_reset,
     "raw": cmd_raw, "irq": cmd_irq}[args.cmd](args)


if __name__ == "__main__":
    main()
