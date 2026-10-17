#!/usr/bin/env python3
"""ntag_dump.py — 把 0x57 吐出来的数据流完整拉一遍

已知：这颗芯片不是「可寻址存储」，而是「连续吐字节」。每次 READ 接着上次继续。
所以直接连读，然后做三件事：
  1) 十六进制 + ASCII 并排打出来
  2) 周期性检测（数据流一般是标签内存的循环，周期 = 标签有效负载长度）
  3) 提取可打印串，并试着按 NDEF 记录头解析

用法:
    sudo python3 ntag_dump.py             # 默认读 1024 字节，每块 32
    sudo python3 ntag_dump.py -n 2048 -c 16
"""
import argparse
import ctypes
import fcntl
import sys

I2C_RDWR = 0x0707
ADDR = 0x57


class i2c_msg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class i2c_rdwr_ioctl_data(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(i2c_msg)), ("nmsgs", ctypes.c_uint32)]


class Bus:
    def __init__(self, nr, addr):
        self.f = open(f"/dev/i2c-{nr}", "rb+", buffering=0)
        self.fd = self.f.fileno()
        self.addr = addr

    def read(self, n):
        buf = (ctypes.c_uint8 * n)()
        m = i2c_msg(self.addr, 1, n, buf)
        arr = (i2c_msg * 1)(m)
        d = i2c_rdwr_ioctl_data(arr, 1)
        fcntl.ioctl(self.fd, I2C_RDWR, d)
        return bytes(buf)

    def write(self, data):
        buf = (ctypes.c_uint8 * len(data))(*data)
        m = i2c_msg(self.addr, 0, len(data), buf)
        arr = (i2c_msg * 1)(m)
        d = i2c_rdwr_ioctl_data(arr, 1)
        fcntl.ioctl(self.fd, I2C_RDWR, d)


def hexdump(data, width=32, base=0):
    out = []
    for off in range(0, len(data), width):
        row = data[off:off + width]
        hexs = " ".join(f"{b:02x}" for b in row)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        out.append(f"{base + off:04x}  {hexs:<{width*3}}  |{asc}|")
    return "\n".join(out)


def find_period(buf, minp=8, maxp=1024):
    """找最小周期 p：buf[i] == buf[i+p] 的比例 > 0.9"""
    n = len(buf)
    best = None
    for p in range(minp, min(maxp, n // 2) + 1):
        same = 0
        tot = 0
        for i in range(0, n - p):
            tot += 1
            if buf[i] == buf[i + p]:
                same += 1
        if tot and same / tot > 0.9:
            best = p
            break
    return best


def ndef_walk(buf):
    """粗解析 NDEF 记录：看 0x91/0xD1 这类记录头"""
    print("  NDEF 记录尝试（TNF/SR/type/len）:")
    hits = 0
    for i in range(len(buf) - 4):
        hdr = buf[i]
        mb = (hdr >> 7) & 1
        me = (hdr >> 6) & 1
        sr = (hdr >> 4) & 1
        tnf = hdr & 0x07
        if not sr:
            continue
        tlen = buf[i + 1]
        plen = buf[i + 2]
        if tlen == 0 or tlen > 8 or plen == 0:
            continue
        if i + 3 + tlen + plen > len(buf):
            continue
        t = buf[i + 3:i + 3 + tlen]
        payload = buf[i + 3 + tlen:i + 3 + tlen + plen]
        # type 必须是可打印（well-known 类型是 ASCII，如 U / T / Sp）
        if not all(32 <= c < 127 for c in t):
            continue
        printable = sum(1 for c in payload if 32 <= c < 127 or c < 0x20) / len(payload)
        if printable < 0.9:
            continue
        hits += 1
        print(f"    off 0x{i:04x}: hdr={hdr:02x} MB={mb} ME={me} TNF={tnf} "
              f"type={t.decode()} len={plen}")
        print(f"      payload: {payload[:80]!r}")
    if not hits:
        print("    （没有找到形状规整的 NDEF 记录 — 流可能从中途开始，加 -n 多读一些）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--total", type=int, default=1024)
    ap.add_argument("-c", "--chunk", type=int, default=32)
    ap.add_argument("--bus", type=int, default=0)
    ap.add_argument("--addr", type=lambda x: int(x, 0), default=ADDR)
    a = ap.parse_args()

    bus = Bus(a.bus, a.addr)
    print(f"总线 /dev/i2c-{a.bus}  从地址 0x{a.addr:02x}  目标 {a.total} 字节，每块 {a.chunk}\n")

    data = bytearray()
    while len(data) < a.total:
        want = min(a.chunk, a.total - len(data))
        try:
            chunk = bus.read(want)
        except OSError as e:
            print(f"\n[在偏移 0x{len(data):04x} 处失败: {e}]")
            break
        if not chunk:
            print(f"\n[偏移 0x{len(data):04x} 读到空]")
            break
        data += chunk

    print("=" * 78)
    print("1. 原始数据")
    print("=" * 78)
    print(hexdump(bytes(data)))

    print()
    print("=" * 78)
    print("2. 周期性")
    print("=" * 78)
    p = find_period(bytes(data))
    if p:
        print(f"  检测到周期 = {p} 字节（0x{p:x}）→ 标签有效负载长度约 {p}")
        print(f"  第一个周期: {bytes(data[:p])!r}")
    else:
        print("  没检测到明确周期（数据不重复，或读得不够多）")

    print()
    print("=" * 78)
    print("3. 可打印串")
    print("=" * 78)
    cur = bytearray()
    start = 0
    for i, b in enumerate(data):
        if 32 <= b < 127:
            if not cur:
                start = i
            cur.append(b)
        else:
            if len(cur) >= 4:
                print(f"  0x{start:04x} ({len(cur):3d}B): {cur.decode('ascii')}")
            cur = bytearray()
    if len(cur) >= 4:
        print(f"  0x{start:04x} ({len(cur):3d}B): {cur.decode('ascii')}")

    print()
    print("=" * 78)
    print("4. NDEF")
    print("=" * 78)
    ndef_walk(bytes(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
