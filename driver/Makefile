# ntag_nfc - Huawei NTAG0001 NFC over I2C
#
# Arch Linux 构建:
#   sudo pacman -S --needed base-devel linux-headers   # 与当前内核匹配的 headers
#   make
#   sudo insmod ntag_nfc.ko
#
obj-m += ntag_nfc.o

KDIR ?= /lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean
	rm -f modules.order Module.symvers

reload: all
	-sudo rmmod ntag_nfc 2>/dev/null || true
	sudo insmod ntag_nfc.ko
	sudo dmesg | tail -40 | grep -iE "ntag|i2c|irq" || true

reload-quiet: all
	-sudo rmmod ntag_nfc 2>/dev/null || true
	sudo insmod ntag_nfc.ko probe_on_load=0

info:
	@cat /sys/bus/i2c/devices/i2c-NTAG0001:00/ntag_nfc/* 2>/dev/null || \
	 find /sys/devices -name "info" -path "*NTAG0001*" -exec cat {} \; 2>/dev/null || \
	 echo "驱动未绑定，先 make reload"

.PHONY: all clean reload reload-quiet info
