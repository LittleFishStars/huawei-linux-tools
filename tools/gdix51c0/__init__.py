# SPDX-License-Identifier: GPL-2.0-or-later
"""GDIX51C0 指纹传感器用户态协议栈。

模块划分：

    transport  /dev/gdix51c0 字符设备的 ioctl 封装（ctypes，无第三方依赖）
    wire       Goodix 外框 + 命令包的组帧与解帧
    tls        基于 ssl.MemoryBIO 的 TLS 1.2 PSK 会话（本端充当 server）
    psk        PSK 的 white-box 容器打包 / 解包（AES-256-GCM + HMAC-SHA256）
    proto      命令构造与初始化序列、FDT、采图
    image      RAW12 解包、方向纠正、PNG 导出
    session    面向使用者的高层 API：init / capture / wait_finger
"""

__version__ = "0.1.7"

ACPI_HID = "GDIX51C0"
DEVICE_NODE = "/dev/gdix51c0"
