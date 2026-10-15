# Multiband MapletCodec patch for NES

All bands of a tile travel in one maplet, band-major (design decision: multiband = 3D tiles).
The preamble layout is unchanged. Byte 2, previously fixed at 1, now holds the band count (1..255).
Single-band blobs are byte-identical to before.

## Files

| File | Use |
|---|---|
| `MapletCodec.cpp.patch` | unified diff against your current `MapletCodec.cpp` (`git apply` from the fork root) |
| `MapletCodec.cpp` | the patched file, if you prefer to copy it |
| header changes | below; apply by hand, since I only saw the body of your header |
| `tests/MapletCodecGoldenTest.cpp` | gtest with 1-band and 2-band golden blobs (decode, byte-identical encode, spec mismatches, text round-trip) |

## Header changes (`MapletCodec.hpp`)

In `TileSpec`, after `double noData = 0.0;`:

```cpp
    uint8_t bands = 1;  // band-major: all bands of a tile travel in one maplet
    [[nodiscard]] uint64_t planeCells() const { return static_cast<uint64_t>(height) * static_cast<uint64_t>(width); }
    [[nodiscard]] uint64_t cellCount() const { return static_cast<uint64_t>(bands) * planeCells(); }   // replaces the old cellCount()
```

In `GridView`, after `double noData = 0.0;`:

```cpp
    uint8_t bands = 1;

    [[nodiscard]] uint64_t planeCells() const { return static_cast<uint64_t>(h) * static_cast<uint64_t>(w); }
    /// All cells of all bands (replaces the old cellCount()).
    [[nodiscard]] uint64_t cellCount() const { return static_cast<uint64_t>(bands) * planeCells(); }
    /// at(r, c) stays as is and reads band 0.
    [[nodiscard]] double at(uint32_t b, uint32_t r, uint32_t c) const { return cells[(b * planeCells()) + (static_cast<uint64_t>(r) * w) + c]; }
    /// Single-band view of band b, so per-band kernels stay unchanged.
    [[nodiscard]] GridView band(uint32_t b) const { return GridView{cells + (b * planeCells()), h, w, noData, 1}; }
```

`bands` is the last member of both structs, so existing aggregate initialisations such as
`GridView{scratch, h, w, noData}` still compile and mean "single band".

## Behaviour changes in `MapletCodec.cpp`

- **Binary decode.** Requires preamble byte 2 to equal `spec.bands`. A mismatch returns `SizeMismatch`, like a height/width mismatch; previously, anything other than 1 returned `BadDimensions`. The size check now covers `bands·h·w` cells.
- **Binary encode.** Writes `spec.bands` into byte 2 and returns `SizeMismatch` if `grid.bands != spec.bands`.
- **Text path.** Reads and writes `bands·h·w` numbers, band-major. No code change was needed beyond `cellCount()`.
- **Validation.** `spec.bands == 0` returns `BadDimensions` in both `encode` and `decode`.
- **Unrelated fix.** Adds `#include <cerrno>`. `parseCell` uses `errno` and only compiled through a transitive include.

## Still to do in your code (not visible to me)

- **Focal kernels.** `FocalKernels` / `FocalDriver::resolveRead` must loop over bands: `for b in 0..bands: kernel(view.band(b), ...)`. Halo resolution reads band `b` of each neighbour tile.
- **Scratch buffers.** Size them with `spec.cellCount()` (now `bands·h·w`), not `h·w`.
- **TileSpec for benchmark streams.** `format = Binary`, `noData = NaN`, `bands = <manifest raster.bands>`.

## Verified

The patched codec compiled with g++ 13 (`-std=c++20 -Wall -Wextra`, no warnings). Results:
- the gtest above: 5/5 passed;
- `crosscheck.cpp` on a 3-band and a 1-band mabench store: every payload decodes and re-encodes byte-identically, and valid/NoData counts match Python.
