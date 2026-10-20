# SPDX-License-Identifier: GPL-2.0
#
# 外部内核模块构建。
#   make            编译 gdix51c0_fp.ko
#   sudo make load 插入模块（需要内核头与 root）
#   sudo make unload
#
KDIR ?= /usr/lib/modules/$(shell uname -r)/build
PWD  := $(shell pwd)

obj-m += gdix51c0_fp.o

all:
	$(MAKE) -C $(KDIR) M=$(PWD) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) clean

load: all
	sudo insmod gdix51c0_fp.ko

unload:
	sudo rmmod gdix51c0_fp

install: all
	sudo insmod gdix51c0_fp.ko

.PHONY: all clean load unload install
