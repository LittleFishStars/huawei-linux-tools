/*
 * GDIX51C0 在 ACPI 中的位置（从 /sys 反推，非 acpidump 输出）
 *
 * 本机 dmesg_restrict=1 且 sudo 需要密码，无法 acpidump 读原始表，也无法
 * 读 /sys/firmware/acpi/tables/DSDT（0400 root）。下面是从 sysfs 能直接确认
 * 的事实，配合 iasl 反编译 DSDT 后即可补全 _CRS 与 _DSM。
 *
 * 已确认：
 *   ACPI 路径   \_SB_.PC00.SPI0.SPBA
 *   _HID/_CID   GDIX51C0
 *   _ADR        0
 *   _UID        1
 *   modalias    acpi:GDIX51C0:GDIX51C0:
 *   父控制器    \_SB_.PC00.SPI0（PCI 8086:51aa, pxa2xx_spi_pci）
 *
 * 设备定义大致是这个形状（字段值待真机 acpidump 确认）：
 */

Device (SPBA)
{
    Name (_ADR, Zero)
    Name (_UID, One)
    Name (_HID, "GDIX51C0")
    Name (_CID, "GDIX51C0")

    /*
     * _CRS 里应当有：
     *   - 一个 SpiSerialBus 资源（挂到父控制器 SPKI）
     *   - 一个 GpioInt（中断脚，ActiveHigh 或 ActiveLow，Edge——待确认）
     *   - 一个 GpioIo（复位脚，GpioOut）
     *
     * 上游驱动硬编码的 line 321 与 140 是 MateBook 16s 的值，本机不适用。
     * 内核对这两个资源的解析入口：
     *   acpi_dev_gpio_irq_get(adev, index)            → 取 GpioInt 并映射 irq
     *   devm_gpiod_get_index(dev, NULL, index, ...)   → 取 GpioIo
     * index 是在同类资源里的序号（从 0 开始）。
     */
    Name (_CRS, ResourceTemplate ()
    {
        SpiSerialBus (1, PolarityLow, FourWireMode, 8,
            ControllerInitiated, 1000000, ClockPolarityLow,
            ClockPhaseFirst, "\\_SB.PC00.SPI0", 0, ResourceConsumer, , )

        GpioInt (Edge, ActiveHigh, Exclusive, PullDown, 0,
            "\\_SB.PC00.GPI0", 0, ResourceConsumer, , ) { 0 }

        GpioIo (Shared, PullUp, 0, 0, IoRestrictionOutputOnly,
            "\\_SB.PC00.GPI0", 0, ResourceConsumer, , ) { 0 }
    })

    Method (_DSM, 4, NotSerialized) { /* 待确认 */ }
}
