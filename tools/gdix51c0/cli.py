# SPDX-License-Identifier: GPL-2.0-or-later
"""``gdix51c0`` 命令行工具。

子命令一览：

    info        打印设备与驱动信息（不需要 PSK）
    diag        裸总线诊断：看 MISO 与中断计数
    otp         打印芯片 ID、OTP 摘要与自校准 DAC
    provision   生成并部署一个新 PSK（破坏性，会覆盖传感器里的旧密钥）
    capture     采一帧指纹并存成 PGM/PNG
    watch       循环等手指，采到就存
    selftest    纯软件自检（不需要硬件）
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import DEVICE_NODE, __version__, config, image, psk, wire
from .session import DEFAULT_PSK_STATE, Session, SessionError
from .transport import KernelDevice, TransportError


def _hexbytes(data: bytes, limit: int = 32) -> str:
    head = data[:limit].hex(" ")
    return head + (" …" if len(data) > limit else "")


# ---- 子命令 --------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    try:
        dev = KernelDevice(args.device)
        info = dev.info()
    except TransportError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(f"设备      {args.device}")
    print(f"ACPI HID  {info.hid}")
    print(f"IRQ       {info.irq}")
    print(f"复位 GPIO {'有' if info.reset_valid else '无'}"
          f"{'（低有效）' if info.reset_active_low else ''}")
    print(f"中断 GPIO {'有' if info.irq_gpio_valid else '无'}")
    print(f"SPI 速率  {info.speed_hz} Hz")
    print(f"分块大小  {info.chunk} 字节")
    print(f"中断计数  {dev.irq_count()}")
    # 中断电平需要 irq_gpio 被解析出来；没有这个资源时 ioctl 会返回 ENODEV，
    # 不是错误，只是这个功能不可用。
    if info.irq_gpio_valid:
        print(f"中断电平  {'高' if dev.irq_level() else '低'}")
    else:
        print("中断电平  不可用（_CRS 里没有可读的 GpioIo）")
    # 复位脚是必需项：拿不到时把 _CRS 全量打出来定位根因。
    # irq_gpio 是可选调试功能（默认 -1 不启用），不在此列。
    if not info.reset_valid:
        _print_missing_gpio_hint(info)
        _dump_crs(args.device)
    elif getattr(args, "verbose", False):
        _dump_crs(args.device)
    dev.close()


def _dump_crs(device: str) -> None:
    """读驱动导出的 _CRS 转储（sysfs resources 属性）。"""
    import glob

    # 设备路径 -> sysfs 属性。传进来的是 /dev/gdix51c0，不能直接拼。
    cands = glob.glob(
        "/sys/bus/spi/devices/spi-GDIX51C0:00/resources"
    ) + glob.glob("/sys/class/misc/gdix51c0/device/resources")
    if not cands:
        print("\n（找不到 _CRS 转储属性，可能驱动不是最新版）", file=sys.stderr)
        return
    try:
        with open(cands[0], encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"\n（读 _CRS 转储失败：{exc}）", file=sys.stderr)
        return
    print("\n== _CRS 实际声明的资源 ==")
    print(text.rstrip())


def _print_missing_gpio_hint(info) -> None:
    """复位脚拿不到时给出下一步的排查建议。"""
    print()
    if not info.reset_valid:
        print(
            "提示：_CRS 里没有可用的 GpioIo（复位脚）。这不一定会阻止采图：\n"
            "  传感器在收到 RESET 命令（0xa2）后会自己软复位。\n"
            "  先用下面这条看 _CRS 里到底有哪些 GPIO 资源：\n"
            "    cat /sys/bus/spi/devices/spi-GDIX51C0:00/resources\n"
            "  [] 里的数字就是资源序号，用模块参数指定：\n"
            "    sudo modprobe gdix51c0_fp reset_gpio_index=<序号>"
        )


def cmd_otp(args: argparse.Namespace) -> int:
    with Session(device_path=args.device) as sess:
        # 走完整的激活流程：_boot_probe 是会话第一步，跳过它 0xa8 会静默
        sess.start()
        chip_id = sess.sensor.chip_id
        otp = sess.sensor.otp
        dac = sess._dac_from_otp(otp)
    print(f"芯片 ID    0x{chip_id:04x}")
    print(f"OTP 长度   {len(otp)} 字节")
    print(f"OTP 前 32  {_hexbytes(otp, 32)}")
    print(f"自校准 DAC main=0x{dac[0]:04x} d1=0x{dac[1]:02x} "
          f"d2=0x{dac[2]:02x} d3=0x{dac[3]:02x}")
    return 0


def cmd_provision(args: argparse.Namespace) -> int:
    if not args.yes:
        print(
            "这会覆盖传感器里现有的 PSK，之后 Windows 侧与其它主机都要重新部署。\n"
            "确认请加 --yes。",
            file=sys.stderr,
        )
        return 2
    key = bytes.fromhex(args.psk_hex) if args.psk_hex else psk.random_psk()
    if len(key) != psk.PSK_LEN:
        print(f"错误：PSK 必须是 32 字节（64 个十六进制字符）", file=sys.stderr)
        return 2
    with Session(device_path=args.device, psk_state_file=args.state) as sess:
        sess.start(psk_bytes=key)
        sess.provision_psk(key)
    print(f"已部署新 PSK，写入 {args.state}")
    print(f"PSK = {key.hex()}")
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    with Session(device_path=args.device, psk_state_file=args.state) as sess:
        sess.start()
        if args.wait:
            print("请按手指…", file=sys.stderr)
            if not sess.arm_finger_detect(args.wait_ms):
                print("等待手指超时", file=sys.stderr)
                return 1
        frame = sess.capture(finger=True)
        return _save_frame(frame, args, sess)


def cmd_watch(args: argparse.Namespace) -> int:
    saved = 0
    with Session(device_path=args.device, psk_state_file=args.state) as sess:
        sess.start()
        print("持续等待手指，Ctrl-C 退出", file=sys.stderr)
        while saved < args.count:
            try:
                if not sess.arm_finger_detect(args.wait_ms):
                    continue
                frame = sess.capture(finger=True)
            except SessionError as exc:
                print(f"采图失败：{exc}", file=sys.stderr)
                continue
            args.output = _numbered(args.output, saved)
            _save_frame(frame, args, sess, quiet=True)
            saved += 1
    return 0


def _numbered(template: str, index: int) -> str:
    root, ext = os.path.splitext(template)
    return f"{root}-{index:02d}{ext}"


def _save_frame(frame: image.Frame, args: argparse.Namespace, sess: Session, quiet: bool = False) -> int:
    stats = frame.stats()
    out = args.output
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    if out.lower().endswith(".png"):
        _save_png(frame, out)
    else:
        frame.save_pgm(out, invert=args.invert)
    if not quiet:
        print(
            f"已保存 {out}  {frame.width}×{frame.height}  "
            f"min={stats['min']:.0f} max={stats['max']:.0f} "
            f"mean={stats['mean']:.0f} std={stats['std']:.1f}"
        )
        if args.ascii:
            print(frame.to_ascii(args.ascii_cols))
    return 0


def _save_png(frame: image.Frame, path: str) -> None:
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit(
            "导出 PNG 需要 python-pillow；或者把输出后缀改成 .pgm"
        ) from exc
    img = Image.frombytes(
        "L", (frame.width, frame.height), frame.to_pgm().split(b"\n", 3)[3]
    )
    img.save(path)


def cmd_selftest(args: argparse.Namespace) -> int:
    """纯软件自检：组帧、白盒 PSK、配置校验和、RAW12 解码。"""
    failures: list[str] = []

    def check(name: str, fn) -> None:
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as exc:  # noqa: BLE001 - 自检要把所有失败都收齐
            failures.append(f"{name}: {exc}")
            print(f"  FAIL {name}: {exc}")

    check("wire 组帧往返", _test_wire_roundtrip)
    check("wire 官方前缀帧", _test_wire_prefix01)
    check("白盒 PSK 往返与篡改拒绝", psk.selftest)
    check("白盒 PSK 官方向量", _test_psk_official_vector)
    check("MCU 配置校验和", config.selftest)
    check("RAW12 解码索引", image.selftest)
    check("帧容量", image.frame_capacity_check)
    check("TLS record 头识别", _test_tls_header)

    if failures:
        print(f"\n{len(failures)} 项失败", file=sys.stderr)
        return 1
    print("\n全部通过")
    return 0


def cmd_diag(args: argparse.Namespace) -> int:
    """裸总线诊断：看 MISO 上到底回了什么。

    初始化失败时先跑这个：能看到复位前后的 IRQ 计数、发出去的字节、
    以及 MISO 是真 0xff（总线空）还是别的东西（时序/极性不对）。
    """
    from . import proto as _proto
    from . import wire as _wire

    dev = KernelDevice(args.device)
    try:
        info = dev.info()
        print(f"ACPI HID   {info.hid}")
        print(f"IRQ        {info.irq}")
        print(f"复位 GPIO  {'有' if info.reset_valid else '无'}")
        print(f"初始中断计数 {dev.irq_count()}")

        def idle_read(n: int = 16) -> str:
            return dev.xfer(b"\xff" * n, n, 0).hex()

        print("\n-- 复位前 --")
        print(f"  空闲 MISO  {idle_read()}")

        print("\n-- 硬复位 --")
        before = dev.irq_count()
        dev.reset(10_000)
        time.sleep(0.2)
        after = dev.irq_count()
        delta = after - before
        print(f"  中断计数   {before} → {after}（差 {delta}）")
        if delta == 0:
            print("  ！复位后没有启动脉冲：MCU 可能未被复位，或中断脚不可用")
        print(f"  复位后 MISO {idle_read()}")

        print("\n-- prefix01 + 0xa8 --")
        dev.xfer(_wire.encode_outer_header(_wire.OUTER_WRITE, len(_proto.PREFIX01)), 0)
        dev.xfer(_proto.PREFIX01, 0)
        time.sleep(0.010)
        a8 = _proto.cmd_firmware_version()
        dev.xfer(_wire.encode_outer_header(_wire.OUTER_WRITE, len(a8)), 0)
        dev.xfer(a8, 0)
        for i in range(8):
            time.sleep(0.050)
            print(f"  +{(i + 1) * 50:3d}ms  中断={dev.irq_count():3d}  MISO={idle_read(32)}")

        print("\n-- 0xd5 force-unlock-tls --")
        d5 = _proto.cmd_force_unlock_tls()
        dev.xfer(_wire.encode_outer_header(_wire.OUTER_WRITE, len(d5)), 0)
        dev.xfer(d5, 0)
        for i in range(4):
            time.sleep(0.050)
            print(f"  +{(i + 1) * 50:3d}ms  中断={dev.irq_count():3d}  MISO={idle_read(32)}")
    finally:
        dev.close()

    print("\n如果全程 MISO 都是 ff 且中断计数不动：复位极性/时刻不对，或 MCU 没上电。")
    return 0


def _test_wire_roundtrip() -> None:
    pkt = wire.encode_packet(wire.OUTER_WRITE, wire.CMD_IDLE, b"\x14\x00")
    outer, plen = wire.decode_outer_header(pkt[:4])
    assert outer == wire.OUTER_WRITE
    cmd = wire.decode_command(pkt[4 : 4 + plen])
    assert cmd.cmd == wire.CMD_IDLE
    assert cmd.data == b"\x14\x00"


def _test_wire_prefix01() -> None:
    # 官方驱动硬编码的 8 字节前缀：末字节是 NULL_CHECKSUM 0x88，
    # 不是计算值（算出来是 0xa4），传感器对这条握手帧接受空校验和。
    prefix = bytes([0x01, 0x05, 0x00, 0x00, 0x00, 0x00, 0x00, 0x88])
    assert prefix[-1] == wire.NULL_CHECKSUM
    assert wire.checksum(prefix[:7]) != prefix[-1], "这条帧故意用空校验和"
    # 外框必须能自洽：真正解析从这里开始
    outer, plen = wire.decode_outer_header(
        wire.encode_outer_header(wire.OUTER_WRITE, len(prefix))
    )
    assert outer == wire.OUTER_WRITE and plen == len(prefix)

def _test_psk_official_vector() -> None:
    """用上游 selftest 的 oracle 向量校验白盒打包。"""
    key = bytes(range(32))
    want = bytes.fromhex(
        "45a3732acf2990f4ceb30e8996e4add12e887160d6dcaa99c8bcd8338c7743f2"
        "02ff20000000e7bc09962aedfb470fac3887de32465c5a71c88fe01dd776cfa5"
        "19e70c63f82401f8247324fde9c6562321d4d82b0dd552c0bf1f74716085b852"
        "658bfe772964"
    )
    got = psk.wrap(key)
    assert got == want, f"白盒容器不一致：\n want {want.hex()}\n  got {got.hex()}"
    assert psk.unwrap(got) == key


def _test_tls_header() -> None:
    # 一条 5+16 字节的应用数据记录
    rec = bytes([0x17, 0x03, 0x03, 0x00, 0x10]) + b"\x00" * 16
    assert wire.tls_record_header_ok(rec)
    assert not wire.tls_record_header_ok(rec[:10])
    assert not wire.tls_record_header_ok(b"\x99" + rec[1:])


# ---- 入口 ----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gdix51c0",
        description="HUAWEI MateBook X Pro (MRGF-XX) 指纹传感器 (GDIX51C0) 用户态工具",
    )
    p.add_argument("--version", action="version", version=f"gdix51c0 {__version__}")
    p.add_argument("--device", default=DEVICE_NODE, help=f"字符设备路径（默认 {DEVICE_NODE}）")
    sub = p.add_subparsers(dest="command", required=True)

    info_p = sub.add_parser("info", help="打印设备与驱动信息")
    info_p.add_argument("-v", "--verbose", action="store_true",
                        help="同时打印 _CRS 声明的全部资源")
    info_p.set_defaults(func=cmd_info)

    diag_p = sub.add_parser("diag", help="裸总线诊断：看 MISO 与中断计数")
    diag_p.set_defaults(func=cmd_diag)

    sub.add_parser("otp", help="读取芯片 ID、OTP 与自校准 DAC").set_defaults(func=cmd_otp)

    prov = sub.add_parser("provision", help="生成并部署新的 PSK（破坏性）")
    prov.add_argument("--state", default=DEFAULT_PSK_STATE, help="PSK 状态文件")
    prov.add_argument("--psk-hex", default=None, help="用指定的 64 位十六进制 PSK")
    prov.add_argument("--yes", "-y", action="store_true", help="确认覆盖传感器里的旧密钥")
    prov.set_defaults(func=cmd_provision)

    cap = sub.add_parser("capture", help="采一帧指纹")
    cap.add_argument("--state", default=DEFAULT_PSK_STATE, help="PSK 状态文件")
    cap.add_argument("--output", "-o", default="finger.pgm", help="输出文件（.pgm 或 .png）")
    cap.add_argument("--invert", action="store_true", help="反相（指纹纹路更接近常见观感）")
    cap.add_argument("--wait", action="store_true", help="先等手指按下再采")
    cap.add_argument("--wait-ms", type=int, default=5000, help="等手指的超时（毫秒）")
    cap.add_argument("--ascii", action="store_true", help="同时打印终端预览")
    cap.add_argument("--ascii-cols", type=int, default=40, help="终端预览宽度（字符数）")
    cap.set_defaults(func=cmd_capture)

    watch = sub.add_parser("watch", help="循环等手指并连续采图")
    watch.add_argument("--state", default=DEFAULT_PSK_STATE, help="PSK 状态文件")
    watch.add_argument("--output", "-o", default="finger.pgm", help="输出文件模板")
    watch.add_argument("--invert", action="store_true", help="反相")
    watch.add_argument("--count", type=int, default=1, help="采几张后退出（0 = 不限）")
    watch.add_argument("--wait-ms", type=int, default=30000, help="单次等手指的超时（毫秒）")
    watch.add_argument("--ascii", action="store_true")
    watch.add_argument("--ascii-cols", type=int, default=40)
    watch.set_defaults(func=cmd_watch)

    sub.add_parser("selftest", help="纯软件自检（不需要硬件）").set_defaults(func=cmd_selftest)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (SessionError, TransportError, psk.PskError, wire.WireError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
