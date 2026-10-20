#!/usr/bin/env python3
"""bus_diag.py — I2C 总线体检

目的：区分「芯片真的返回 0x00」和「总线/适配器在骗人（SDA 被拉低、缓冲没填、适配器假成功）」。
靠的是对照实验：往一个确定不存在的地址读数据，正常总线必须 NAK。

用法:
    sudo python3 bus_diag.py            # 只测对照组 + 0x57 的几种组帧
    sudo python3 bus_diag.py --scan      # 再加 0x08..0x77 全扫（每址读 1 字节，慢）
    sudo python3 bus_diag.py --bus 1     # 换总线
"""
import ctypes
import fcntl
import sys
import time

I2C_RDWR = 0x0707


class i2c_msg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class i2c_rdwr_ioctl_data(ctypes.Structure):
    _fields_ = [("msgs", ctypes.POINTER(i2c_msg)), ("nmsgs", ctypes.c_uint32)]


def xfer(fd, addr, write=None, read_len=0, repeated_start=False):
    """一次 ioctl：可写+读；repeated_start=True 时合成一条消息（Sr），否则拆成两条。"""
    keep = []
    msgs = []
    wbuf = None
    if write is not None and len(write) > 0:
        wbuf = (ctypes.c_uint8 * len(write))(*write)
        keep.append(wbuf)
    rbuf = None
    if read_len:
        rbuf = (ctypes.c_uint8 * read_len)()
        keep.append(rbuf)

    if wbuf is not None and rbuf is not None and repeated_start:
        # 写与读合成一条（内核会用 repeated START 一次性完成）
        msgs.append(i2c_msg(addr, 0, len(write), wbuf))
        msgs.append(i2c_msg(addr, 1, read_len, rbuf))
        nmsgs = 2
    else:
        if wbuf is not None:
            msgs.append(i2c_msg(addr, 0, len(write), wbuf))
        if rbuf is not None:
            msgs.append(i2c_msg(addr, 1, read_len, rbuf))
        nmsgs = len(msgs)

    if nmsgs == 0:
        return b""
    arr = (i2c_msg * nmsgs)(*msgs)
    data = i2c_rdwr_ioctl_data(arr, nmsgs)
    fcntl.ioctl(fd, I2C_RDWR, data)
    return bytes(rbuf) if rbuf is not None else b""


def probe(fd, addr, write=None, read_len=1, repeated_start=False):
    t0 = time.time()
    try:
        d = xfer(fd, addr, write=write, read_len=read_len, repeated_start=repeated_start)
        return ("ACK", d, time.time() - t0)
    except OSError as e:
        return ("ERR", e.errno, time.time() - t0)


def fmt(r):
    kind, val, dt = r
    if kind == "ACK":
        return f"应答 {val.hex(' ')}  ({dt*1000:.0f} ms)"
    num = {
        121: "NAK 无此设备",
        110: "超时（设备拉住总线不放）",
        6: "ENXIO",
        5: "EIO",
        95: "EOPNOTSUPP（适配器不支持这种消息）",
    }.get(val, "errno")
    return f"失败 {val} {num}  ({dt*1000:.0f} ms)"


def main():
    bus_nr = 0
    do_scan = False
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--scan":
            do_scan = True
        elif args[i] == "--bus" and i + 1 < len(args):
            bus_nr = int(args[i + 1])
            i += 1
        i += 1

    dev = f"/dev/i2c-{bus_nr}"
    fd = fcntl.ioctl.__self__ if False else None  # noqa
    try:
        f = open(dev, "rb+", buffering=0)
        fd = f.fileno()
    except OSError as e:
        print(f"打不开 {dev}: {e}")
        return 1

    print(f"总线 {dev}\n")

    # ---------- A. 对照组：这个地址必须 NAK ----------
    print("=" * 60)
    print("A. 对照组（确定不存在的地址，正常总线必须失败）")
    print("=" * 60)
    ctrl = {}
    for a in (0x60, 0x33):
        r = probe(fd, a)
        ctrl[a] = r[0]
        print(f"  0x{a:02x}: {fmt(r)}")

    # ---------- B. 目标地址的几种组帧 ----------
    print()
    print("=" * 60)
    print("B. 0x57 的四种组帧（都只读，不写）")
    print("=" * 60)
    tests = [
        ("裸读 1 字节", dict(read_len=1)),
        ("裸读 16 字节", dict(read_len=16)),
        ("写指针 0000 后读 16（两次传输）", dict(write=b"\x00\x00", read_len=16)),
        ("写指针 0000 + Sr 读 16（一次传输）", dict(write=b"\x00\x00", read_len=16, repeated_start=True)),
        ("写指针 f8 后读 4（读配置区）", dict(write=b"\x00\xf8", read_len=4)),
    ]
    for name, kw in tests:
        r = probe(fd, 0x57, **kw)
        print(f"  {name:34s}: {fmt(r)}")

    # ---------- C. 全扫 ----------
    if do_scan:
        print()
        print("=" * 60)
        print("C. 全扫 0x08..0x77（每址读 1 字节；超时的地址会很慢）")
        print("=" * 60)
        found = []
        for a in range(0x08, 0x78):
            r = probe(fd, a)
            if r[0] == "ACK":
                found.append(a)
                print(f"  0x{a:02x}: {fmt(r)}")
            elif r[1] == 110:
                print(f"  0x{a:02x}: {fmt(r)}")
        print(f"\n  应答地址: {[hex(x) for x in found] or '无'}")

    # ---------- D. 判读 ----------
    print()
    print("=" * 60)
    print("D. 判读")
    print("=" * 60)
    if any(v == "ACK" for v in ctrl.values()):
        print("  ! 空地址竟然应答了 -> 总线/适配器不可信：")
        print("    SDA 被某个从设备拉死在低电平（读回来全是 0x00），")
        print("    或者 intel-lpss 的 I2C 适配器假报成功。")
        print("    此时 0x57 的 0x00 是假的，不能当数据看。")
    else:
        print("  对照组正常 NAK -> 总线是诚实的，能区分「有设备」和「没设备」。")
        print("  0x57 应答且读到全 0x00 -> 这颗芯片在当前位置就是返回 0。")
        print("  常见原因：还没上电/没解除复位（需要 ACPI 里的 GPIO 时序），")
        print("  或者它根本不是标准 Type 2 Tag 的存储映射。")
    print()
    print("下一步（拿 ACPI 原文，看有没有使能/复位 GPIO 和 _PS0 上电时序）：")
    print("  sudo pacman -S --needed acpica")
    print("  cd /home/ylxc/nfc-re && sudo acpidump -o acpi.bin && acpixtract -a acpi.bin")
    print("  iasl -d DSDT.dat 2>/dev/null; ls *.dsl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
