# Pre-built Native Libraries for PHF (Pereira-Huffman Format)

This directory contains pre-compiled native dynamic libraries for the PHF encoder/decoder engine.
The server automatically discovers and loads the matching binary for your platform.

## Included Binaries
- **Windows (x64)**: `phf.dll` (v1.1, with the `phf_encode_options_t` API).
- **macOS (universal: Apple Silicon arm64 + Intel x86_64, macOS 11+)**: `libphf.dylib`.
  Built from [libphf/phf](https://github.com/libphf/phf) commit `a5479b1` without OpenMP, so it
  depends only on `libSystem` (no Homebrew `libomp` needed).

## Building from source

The library is two C files, `phf.c` and `huff.c`. The simplest route is the CMake build of the
sibling `phf` repository (run from the workspace folder that contains both repos):

```bash
cmake -S phf -B phf/build -DCMAKE_BUILD_TYPE=Release -DPHF_BUILD_TESTS=OFF
cmake --build phf/build
```

The server also finds `phf/build/libphf.{so,dylib}` there, so copying is optional.

### Linux (Ubuntu / Debian / Fedora / Arch)
```bash
gcc -O3 -fPIC -shared -o bin/libphf.so \
    ../phf/libphf/src/phf.c ../phf/libphf/src/huff.c -I ../phf/libphf/include
```

### macOS (reproduces the bundled universal `libphf.dylib`)
```bash
cmake -S ../phf -B /tmp/phf-mac -DCMAKE_BUILD_TYPE=Release -DPHF_BUILD_TESTS=OFF -DPHF_OPENMP=OFF \
      "-DCMAKE_OSX_ARCHITECTURES=arm64;x86_64" -DCMAKE_OSX_DEPLOYMENT_TARGET=11.0
cmake --build /tmp/phf-mac
cp /tmp/phf-mac/libphf.dylib bin/libphf.dylib
install_name_tool -id @loader_path/libphf.dylib bin/libphf.dylib
codesign --force -s - bin/libphf.dylib
```
