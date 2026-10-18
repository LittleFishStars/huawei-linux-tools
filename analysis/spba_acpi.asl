/*
 * GDIX51C0 (SPBA) 的 ACPI 定义 —— 从本机 DSDT 反编译摘录
 *
 * 生成方式：
 *   sudo acpidump -b -o dsdt.dat
 *   iasl -d dsdt.dat
 * 摘录自 dsdt.dsl 第 98051 行起的 Device (SPBA)，以及相关的 _INI 钩子。
 *
 * 关键点（见下方 _CRS 与 _INI）：
 *   - 引脚编号不是硬编码，而是运行时由 GNUM(0x090E0001) / GNUM(0x0907000C)
 *     写进 ResourceTemplate 的占位符；本机求值结果是 321 / 140。
 *   - _INI 里有两次 SHPO（直接写 GPIO 寄存器把引脚设为输出高）：
 *       SHPO(0x090E0001, One)   -> INT 引脚
 *       SHPO(0x0907000C, One)   -> 复位引脚
 *     这两步是 BIOS 的固定上电序列，Windows 驱动依赖它完成初始化。
 *   - GpioInt 用 PullDefault（不指定上下拉），GpioIo 用 PullUp +
 *     IoRestrictionOutputOnly。
 */

        Device (SPBA)
        {
            Name (_ADR, Zero)  // _ADR: Address
            Name (_HID, "GDIX51C0")  // _HID: Hardware ID
            Name (_CID, "GDIX51C0")  // _CID: Compatible ID
            Name (_UID, One)  // _UID: Unique ID
            Name (SID0, One)
            If (Ones)
            {
                SID0 = GGIV (0x0902000C)
                If ((SID0 == Zero))
                {
                    _HID = "SIL6150"
                    _CID = "SIL6150"
                }
            }

            Method (_INI, 0, NotSerialized)  // _INI: Initialize
            {
                SHPO (0x090E0001, One)
                SHPO (0x0907000C, One)
            }

            Method (_STA, 0, NotSerialized)  // _STA: Status
            {
                If (HFTE)
                {
                    Return (0x0F)
                }

                Return (Zero)
            }

            Method (_CRS, 0, Serialized)  // _CRS: Current Resource Settings
            {
                Name (RBUF, ResourceTemplate ()
                {
                    SpiSerialBusV2 (0x0000, PolarityLow, FourWireMode, 0x08,
                        ControllerInitiated, 0x00989680, ClockPolarityLow,
                        ClockPhaseFirst, "\\_SB.PC00.SPI0",
                        0x00, ResourceConsumer, _Y76, Exclusive,
                        )
                    GpioInt (Level, ActiveHigh, ExclusiveAndWake, PullDefault, 0x0000,
                        "\\_SB.GPI0", 0x00, ResourceConsumer, ,
                        )
                        {   // Pin list
                            0x0000
                        }
                    GpioIo (Exclusive, PullUp, 0x0000, 0x0000, IoRestrictionOutputOnly,
                        "\\_SB.GPI0", 0x00, ResourceConsumer, ,
                        )
                        {   // Pin list
                            0x0000
                        }
                })
                CreateWordField (RBUF, \_SB.PC00.SPI0.SPBA._CRS._Y76._SPE, FREQ)  // _SPE: Speed
                If ((SID0 == Zero))
                {
                    FREQ = 0x005B8D80
                }

                CreateWordField (RBUF, 0x3B, GPIN)
                CreateWordField (RBUF, 0x5E, SPIN)
                GPIN = GNUM (0x090E0001)
                SPIN = GNUM (0x0907000C)
                Return (RBUF) /* \_SB_.PC00.SPI0.SPBA._CRS.RBUF */
            }
        }
