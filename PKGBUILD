# Maintainer: ylxc <ylxc@localhost>
#
# 内核传输模块走 DKMS（源码装到 /usr/src/gdix51c0-fp-<ver>），用户态工具装成
# Python 包。makepkg 从本目录的 git 工作树取文件，不需要联网下载源码：
#
#   makepkg -si
#
# 打包会先编一次模块（需要 linux-headers）；装包后 DKMS 再为每个已安装内核各编一次。

pkgname=gdix51c0-fp
pkgver=0.1.10
pkgrel=1
pkgdesc='HUAWEI MateBook X Pro (MRGF-XX) 指纹传感器 GDIX51C0 的 SPI 传输模块与用户态工具'
arch=('any')
url='https://github.com/LittleFishStars/huawei-gdix51c0-fp'
license=('GPL-2.0-or-later')
depends=('dkms' 'python' 'python-cryptography')
optdepends=('python-pillow: 导出 PNG 指纹图')
makedepends=('linux-headers')
install=packaging/gdix51c0-fp.install
# 内核模块由自己编译，不让 makepkg 的 strip 去动 .ko
options=(!strip !debug)
# !debug：模块由 DKMS 自己编，没必要留 /usr/src/debug 下的源码副本

_srcname="gdix51c0-fp-${pkgver}"

# 源就是本地工作树：不走 download()，build() 里自己拷进 $srcdir
source=()
sha256sums=()

prepare() {
    rm -rf "${srcdir:?}/${_srcname}"
    mkdir -p "${srcdir}/${_srcname}"
    cp -a "${startdir}/driver" "${startdir}/tools" \
          "${startdir}/packaging" "${startdir}/tests" \
          "${startdir}/LICENSE" "${startdir}/README.md" \
          "${srcdir}/${_srcname}/"
    # 开发树里的编译产物不进包
    find "${srcdir}/${_srcname}" \
         \( -name '*.o' -o -name '*.ko' -o -name '*.mod' -o -name '*.cmd' \
            -o -name 'Module.symvers' -o -name 'modules.order' \
            -o -name '__pycache__' -o -name '*.pyc' \) -prune -exec rm -rf {} +
}

build() {
    cd "${srcdir}/${_srcname}"
    make -C "/usr/lib/modules/$(uname -r)/build" M="$PWD/driver" modules
}

package() {
    cd "${srcdir}/${_srcname}"

    # ---- DKMS 源：/usr/src/gdix51c0-fp-<ver>/ ----
    install -d "${pkgdir}/usr/src/${_srcname}"
    install -m644 driver/gdix51c0_fp.c "${pkgdir}/usr/src/${_srcname}/gdix51c0_fp.c"
    install -m644 packaging/Makefile.kbuild "${pkgdir}/usr/src/${_srcname}/Makefile"
    sed "s/@VERSION@/${pkgver}/" packaging/dkms.conf.in \
        > "${pkgdir}/usr/src/${_srcname}/dkms.conf"
    install -m644 LICENSE "${pkgdir}/usr/src/${_srcname}/LICENSE"

    # ---- 用户态 Python 包 ----
    local purelib
    purelib="$(python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))')"
    install -d "${pkgdir}${purelib}"
    cp -a tools/gdix51c0 "${pkgdir}${purelib}/"
    find "${pkgdir}" -name '__pycache__' -prune -exec rm -rf {} +
    find "${pkgdir}" -name '*.pyc' -delete

    # ---- 可执行入口 ----
    install -Dm755 packaging/gdix51c0 "${pkgdir}/usr/bin/gdix51c0"

    # ---- 文档与许可证 ----
    install -Dm644 README.md "${pkgdir}/usr/share/doc/${pkgname}/README.md"
    install -Dm644 LICENSE "${pkgdir}/usr/share/licenses/${pkgname}/LICENSE"

    # ---- 离线自检用到的单测一并装上 ----
    install -Dm644 tests/test_proto.py "${pkgdir}/usr/share/doc/${pkgname}/tests/test_proto.py"
}
