#!/usr/bin/env python3
"""touchd.py — NTAG0001 「碰一碰」触碰事件守护进程

原理
    DSDT 里 NFC 设备只声明了两样资源：I2C 0x57 + 一根 GpioInt(pin 176, 上升沿)。
    这根中断就是芯片的 field-detect：手机贴近 -> 芯片被 RF 场唤醒 -> 拉高 pin 176。
    我们不需要读数据（标签内容在芯片里、由手机直接读走），只需要把这次触碰
    变成本机事件。

用法
    sudo python3 touchd.py                 # 前台，打印触碰事件
    sudo python3 touchd.py --exec CMD      # 每次触碰执行 CMD（%s 替换为时间戳）
    sudo python3 touchd.py --watch-dev     # 同时监听 /dev/ntag_nfc（如果装了模块）

        --exec 例子：
    sudo python3 touchd.py --exec 'notify-send "NFC" "手机碰触于 %s"'
    sudo python3 touchd.py --exec '/usr/local/bin/huawei-share-start'

输出
    每行一个事件：ISO8601 时间戳 + 事件类型，方便被脚本消费。
"""
import argparse
import glob
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime

IRQ_NAME = "ntag_nfc"
GPIO_CHIP = "gpiochip"          # 只用 sysfs 兜底时才用
GPIO_LINE = "176"


def log(msg):
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


# --------------------------------------------------------------------------
# 方式 A：直接 poll 一个 GPIO 值文件（最简单、零依赖、不要模块）
# --------------------------------------------------------------------------
def find_gpio_path():
    """在 sysfs 里找到 line 176 的 value 文件。

    注意：如果某驱动（如 ntag_nfc）已经用 gpiod 申请了这根线，
    sysfs 的 gpioN 节点可能不存在。这时退回方式 B。
    """
    # 1) 传统 sysfs：/sys/class/gpio/gpioN/value，需要先 export
    base = int(GPIO_LINE)
    for cand in (base, base):
        p = f"/sys/class/gpio/gpio{cand}/value"
        if os.path.exists(p):
            return p
    return None


# --------------------------------------------------------------------------
# 方式 B：/proc/interrupts 轮询（最可靠，任何情况下都能读）
# --------------------------------------------------------------------------
def irq_count_from_proc(name):
    """从 /proc/interrupts 里取指定 IRQ 名字那一行的合计计数。"""
    try:
        with open("/proc/interrupts") as f:
            for line in f:
                if not line.rstrip().endswith(name):
                    continue
                cols = line.split()
                # 第一列是 "213:"，中间若干 CPU 计数，最后是 类型/描述
                nums = []
                for c in cols[1:]:
                    if c.isdigit():
                        nums.append(int(c))
                    else:
                        break
                if nums:
                    return sum(nums)
    except OSError:
        pass
    return None


def run_cmd(cmd, ts):
    c = cmd.replace("%s", ts)
    try:
        subprocess.Popen(c, shell=True,
                         stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        log(f"  已执行: {c}")
    except OSError as e:
        log(f"  执行失败: {c} ({e})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--exec", dest="cmd", default=None,
                    help="每次触碰执行的 shell 命令，%%s 替换为时间戳")
    ap.add_argument("--interval", type=float, default=0.05,
                    help="轮询间隔秒（默认 0.05）")
    ap.add_argument("--name", default=IRQ_NAME, help="中断名（默认 ntag_nfc）")
    ap.add_argument("--once", action="store_true", help="只等一次触碰就退出")
    a = ap.parse_args()

    gpio_path = find_gpio_path()
    log(f"启动: 中断名={a.name} 轮询间隔={a.interval}s "
        f"GPIO路径={gpio_path or '不可用（走 /proc/interrupts）'}")

    if a.cmd:
        log(f"触碰回调: {a.cmd}")

    last = irq_count_from_proc(a.name)
    if last is None:
        log(f"警告: /proc/interrupts 里没有名字为 {a.name} 的中断行。")
        log("      中断只有在驱动申请后才会带名字——先装载模块：")
        log("        sudo insmod ntag_nfc.ko probe_on_load=0")
        return 1
    log(f"初始中断计数 = {last}")

    # 如果 sysfs GPIO 可用，优先用边沿检测（能区分贴上/离开）
    gpio_fd = None
    if gpio_path:
        try:
            with open(os.path.dirname(gpio_path) + "/edge", "w") as f:
                f.write("both")
            gpio_fd = open(gpio_path, "r+", buffering=0)
            log("使用 sysfs GPIO 边沿事件（可区分贴上/离开）")
        except OSError as e:
            log(f"sysfs GPIO 边沿不可用（{e}），退回中断计数")

    def loop_irq():
        nonlocal last
        cur = irq_count_from_proc(a.name)
        if cur is not None and cur != last:
            delta = cur - last
            last = cur
            ts = datetime.now().isoformat(timespec="seconds")
            log(f"NFC 触碰事件 (irq +{delta}, 累计 {cur})")
            if a.cmd:
                run_cmd(a.cmd, ts)
            return True
        return False

    def loop_gpio():
        # sysfs 边沿：read() 会阻塞到有事件，返回 "0"/"1"
        os.set_blocking(gpio_fd.fileno(), True)
        os.lseek(gpio_fd.fileno(), 0, os.SEEK_SET)
        val = gpio_fd.read(1)
        ts = datetime.now().isoformat(timespec="seconds")
        level = "贴上(高)" if val.strip() == b"1" else "离开(低)"
        log(f"NFC {level}")
        if level.startswith("贴上") and a.cmd:
            run_cmd(a.cmd, ts)
        return True

    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    while True:
        if gpio_fd is not None:
            try:
                loop_gpio()
                continue
            except OSError:
                log("sysfs GPIO 出错，切换到中断计数模式")
                gpio_fd = None
        else:
            if loop_irq() and a.once:
                return 0
            time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
