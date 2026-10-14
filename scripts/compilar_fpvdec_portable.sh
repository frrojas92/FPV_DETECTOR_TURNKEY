#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$ROOT/vendor/fpvdec"

BUILD="${1:-$ROOT/build/fpvdec-portable}"

echo "FPV TURNKEY — COMPILACIÓN PORTABLE"
echo "Fuente: $SOURCE"
echo "Destino: $BUILD"

command -v cmake >/dev/null
command -v pkg-config >/dev/null

pkg-config --exists libhackrf || {
    echo "ERROR: falta libhackrf-dev"
    exit 1
}

SDL_DIR="$(dpkg-query -L libsdl2-dev 2>/dev/null |
    grep -E '/(SDL2Config|sdl2-config)\.cmake$' |
    head -n 1 |
    xargs -r dirname)"

if [[ -z "$SDL_DIR" ]] || {
    [[ ! -f "$SDL_DIR/SDL2Config.cmake" ]] &&
    [[ ! -f "$SDL_DIR/sdl2-config.cmake" ]]
}; then
    echo "ERROR: no se encuentra SDL2Config.cmake"
    echo "Comprobar: dpkg-query -L libsdl2-dev"
    exit 1
fi

echo "SDL2 CMake: $SDL_DIR"

mkdir -p "$BUILD"

env -u CMAKE_PREFIX_PATH -u SDL2_DIR \
cmake -S "$SOURCE" -B "$BUILD" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_PREFIX_PATH=/usr \
    -DSDL2_DIR="$SDL_DIR" \
    -DCMAKE_SKIP_BUILD_RPATH=ON

cmake --build "$BUILD" --parallel 1

ctest --test-dir "$BUILD" --output-on-failure

echo
echo "Comprobando vinculación SDL2..."

SDL_PATH="$(ldd "$BUILD/fpvdec" |
    awk '/libSDL2-2.0.so.0/ {print $3}')"

case "$SDL_PATH" in
    /lib/*|/usr/lib/*)
        echo "SDL2 del sistema: $SDL_PATH"
        ;;
    *)
        echo "ERROR: SDL2 no corresponde al sistema: $SDL_PATH"
        exit 1
        ;;
esac

if readelf -d "$BUILD/fpvdec" |
   grep -Eq 'RPATH|RUNPATH'; then
    echo "ERROR: ejecutable contiene RPATH/RUNPATH"
    exit 1
fi

echo
echo "COMPILACIÓN PORTABLE APROBADA"
sha256sum "$BUILD/fpvdec"
