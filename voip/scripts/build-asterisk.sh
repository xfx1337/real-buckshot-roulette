#!/bin/bash
# Build Asterisk 20 from source into voip/asterisk-local.
#
# Homebrew has no asterisk formula, and the Docker image cannot reach the
# gateway (see README), so the PBX is built and run natively. This takes a few
# minutes and only has to be done once.
set -euo pipefail

VOIP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="$VOIP_DIR/asterisk-local"
BUILD_DIR="$VOIP_DIR/build"
VERSION="20-current"

brew install jansson ncurses libedit libxml2 openssl@3 sqlite pkg-config

mkdir -p "$BUILD_DIR"
cd "$BUILD_DIR"
if [[ ! -f "asterisk-$VERSION.tar.gz" ]]; then
    curl -fL -O "https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-$VERSION.tar.gz"
fi
tar xzf "asterisk-$VERSION.tar.gz"
SRC="$(find "$BUILD_DIR" -maxdepth 1 -type d -name 'asterisk-20.*' | sort | tail -1)"
cd "$SRC"

BREW="$(brew --prefix)"
export PKG_CONFIG_PATH="$BREW/opt/ncurses/lib/pkgconfig:$BREW/opt/libedit/lib/pkgconfig:$BREW/opt/openssl@3/lib/pkgconfig:$BREW/opt/libxml2/lib/pkgconfig:$BREW/opt/sqlite/lib/pkgconfig:$BREW/opt/jansson/lib/pkgconfig"
export CFLAGS="-I$BREW/include -I$BREW/opt/ncurses/include -I$BREW/opt/libedit/include -I$BREW/opt/openssl@3/include -I$BREW/opt/sqlite/include"
export LDFLAGS="-L$BREW/lib -L$BREW/opt/ncurses/lib -L$BREW/opt/libedit/lib -L$BREW/opt/openssl@3/lib -L$BREW/opt/sqlite/lib"

./configure --prefix="$PREFIX" \
    --sysconfdir="$PREFIX/etc" \
    --localstatedir="$PREFIX/var" \
    --with-pjproject-bundled \
    --without-x11 --disable-xmldoc

# Two fixes are needed before this tree builds on macOS 26 / Apple clang.

# 1. main/Makefile hardcodes the pjproject library suffix to the OS version
#    that configure happened to see, which does not always match the one
#    pjproject then builds with. Read it from pjproject instead.
if grep -q '^PJ_TARGET := aarch64-apple-darwin' main/Makefile; then
    sed -i '' \
        's|^PJ_TARGET := aarch64-apple-darwin.*$|PJ_TARGET := $(shell grep "^export TARGET_NAME" $(PJPROJECT_SRCDIR)/build.mak \| sed "s/.*:= *//")|' \
        main/Makefile
fi

# 2. res_geolocation embeds XML with GNU ld syntax ("-znoexecstack", "-b
#    binary") that Apple's linker rejects. Nothing here uses it. menuselect
#    is built by configure, so it exists by now.
make menuselect.makeopts
"$SRC/menuselect/menuselect" --disable res_geolocation menuselect.makeopts

make -j"$(sysctl -n hw.ncpu)"
make install
make samples

echo
echo "Built into $PREFIX"
echo "Start it with: ./scripts/run-asterisk.sh"
