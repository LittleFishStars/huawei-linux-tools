# huawei-ntag0001-nfc

把 **Huawei MateBook X Pro 2023（MRGF-XX）内置 NFC** 在 Linux 上跑通的适配工程。

逆向对象是华为官方 Windows 驱动 `SpbNfcDriver.sys`（随 `NFC_23.2.3.0.zip` 分发）。

## 结论

$$\boxed{\text{这颗 NFC 是一张「永远在线的只读标签」，RF 侧自主工作，Linux 下无需驱动即已可用}}$$

- 芯片挂在 `i2c-0` 的 **0x57**，速率 400 kHz，控制器 `\_SB.PC00.I2C0`
- 中断是 `GpioInt(Edge, ActiveHigh, pin 176)` → **irq 213**，语义为 field-detect
- 手机贴上时芯片自己把 `https://consumer.huawei.com/en/support/huaweishare` 这条 NDEF 交给手机
- **手机上能看到链接就说明 NFC 功能已完整可用**，与操作系统无关

Windows 驱动做的是另一件事：「碰一碰 → PC 侧 Huawei Share 联动」。它不导入 `nfccx.sys`，
走私有 SPB 通道 + 空密钥 HMAC-SHA256 八组挑战应答，**不含任何标准 NCI 命令**。
这部分在 Linux 上不移植也不影响手机读标签。

## 目录

```
driver/      ntag_nfc.c + Makefile   内核模块：绑定 ACPI、暴露 /dev/ntag_nfc、注册 FD 中断
tools/       touchd.py               触碰事件守护进程（轮询 irq 计数，可挂回调）
             bus_diag.py             I2C 总线体检（对照组 / 全扫 / 组帧试错）
             ntag_dump.py            把 0x57 的数据流完整抓下来并解析 NDEF
             ntag_id.py              单次只读识别
             get_acpi.sh             dump + 反编译 ACPI，定位 NTAG0001
             fix_touchpad.sh         适配器 wedged 后救援触控板（已验证）
analysis/    SpbNfcDriver.sys.dis.txt  驱动全反汇编（objdump，5591 行）
             acpi/nfc_device.asl        ACPI 设备定义摘录 + GNUM 换算说明
docs/        逆向与适配报告.md          完整逆向报告
```

## 快速开始

```bash
# 1. 触碰事件（唯一还需要做的事）
sudo insmod driver/ntag_nfc.ko probe_on_load=0
sudo python3 tools/touchd.py --exec 'notify-send "NFC" "手机碰触于 %s"'
# 手机贴到掌托右侧 NFC 标识处，应弹通知

# 2. 看硬件事实
cat /sys/devices/.../ntag_nfc/info      # 从地址 / 中断号 / 摘要长度
grep ntag_nfc /proc/interrupts          # 触碰时计数 +1

# 3. 验证 ACPI（需要 root）
sudo tools/get_acpi.sh
```

## 构建内核模块

```bash
cd driver
sudo pacman -S --needed base-devel linux-headers
make
sudo insmod ntag_nfc.ko probe_on_load=0
```

模块参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `probe_on_load` | 1 | 装载时是否主动发探测帧。**建议 0**，0x57 上盲写有风险 |
| `irq_type` | 0 | 0=按 ACPI 自动，1=上升沿，2=下降沿，3=双边沿 |

## 踩过的坑

- **不要 unbind/bind `0000:00:15.0`**（PCI 控制器）来做总线复位。内核会重建 adapter
  但 i2c-hid 的 client 状态失效，**触控板直接失灵**。
  救援用 `tools/fix_touchpad.sh`（只重绑 client，不用重启）。
- i2c 控制器超时一次就会 wedged，之后所有传输都返回 `-110 ETIMEDOUT`，
  读数全不可信。`bus_diag.py` 的对照组就是为了区分「真数据」和「假数据」。
- `-110` 不等于设备坏：地址被 ACK、随后设备拉住 SCL，是「设备活着但没数据」。
  `-121 EREMOTEIO` 才是「这个地址没人」。

## 硬件事实速查

| 项 | 值 |
|---|---|
| ACPI 路径 | `\_SB.PC00.NFC_` |
| `_HID` | `NTAG0001`（无 `_CID` / `_SUB` / `_DSD`） |
| `_STA` | 恒 `0x0F` |
| 电源 | **无 `_PS0` / `_PS3`**，主机管不了电 |
| GPIO | **无 `GpioIo`**，主机无法复位 |
| I2C | `0x57`，`0x00061A80` = 400 kHz，7-bit |
| 中断 | `GpioInt(Edge, ActiveHigh, Exclusive, PullUp)`，pin 176 → irq 213 |
| Windows INF | `ACPI\NTAG0001` → `SpbNfcDriver.sys`（KMDF，HIDClass） |

## 许可

分析结果与自写代码可自由使用。`analysis/` 下为静态反汇编文本，仅作研究参考；
`SpbNfcDriver.sys` 本体为华为版权作品，未随本仓库分发。
