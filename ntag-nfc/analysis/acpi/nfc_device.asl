    }

    Scope (_SB.PC00)
    {
        Device (NFC)
        {
            Name (_HID, "NTAG0001")  // _HID: Hardware ID
            Method (_STA, 0, NotSerialized)  // _STA: Status
            {
                Return (0x0F)
            }

            Method (_CRS, 0, NotSerialized)  // _CRS: Current Resource Settings
            {
                Name (SBUF, ResourceTemplate ()
                {
                    I2cSerialBusV2 (0x0057, ControllerInitiated, 0x00061A80,
                        AddressingMode7Bit, "\\_SB.PC00.I2C0",
                        0x00, ResourceConsumer, , Exclusive,
                        )
                    GpioInt (Edge, ActiveHigh, Exclusive, PullUp, 0x0000,
                        "\\_SB.GPI0", 0x00, ResourceConsumer, ,
                        )
                        {   // Pin list
                            0x0000
                        }
                })
                CreateWordField (SBUF, 0x38, INT1)
                INT1 = GNUM (0x09080010)
                Return (SBUF) /* \_SB_.PC00.NFC_._CRS.SBUF */
            }
        }
    }
// ---- 以下为 GNUM 等辅助方法（从 DSDT 摘录，用于说明 GpioInt pin 号如何算出 176）----
        Method (GNUM, 1, NotSerialized)
        {
            Local0 = GNMB (Arg0)
            Local1 = GGRP (Arg0)
            Return ((GINF (Local1, 0x08) + Local0))
        }
    // GNUM(0x09080010): GGRP=0x08, GNMB=0x0010 -> GINF(group 8, 0x08) + 16
