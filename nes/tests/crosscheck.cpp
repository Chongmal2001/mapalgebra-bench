// Walks a mabench frame file, decodes every payload with the real Raster::decode,
// re-encodes it with Raster::encode and requires the bytes to be identical.
#include <MapletCodec.hpp>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <limits>
#include <vector>

int main(int argc, char** argv)
{
    if (argc < 2) { std::fprintf(stderr, "usage: crosscheck FRAME_FILE [noData]\n"); return 2; }
    std::ifstream f(argv[1], std::ios::binary);
    std::vector<char> buf((std::istreambuf_iterator<char>(f)), {});
    const double noData = argc > 2 ? std::strtod(argv[2], nullptr) : std::numeric_limits<double>::quiet_NaN();
    if (buf.size() < 16 || std::memcmp(buf.data(), "MPLFRM03", 8) != 0) { std::puts("bad frame magic"); return 1; }
    uint32_t nRecords; std::memcpy(&nRecords, buf.data() + 12, 4);
    size_t off = 16; double sum = 0; uint64_t valid = 0, nan = 0;
    for (uint32_t i = 0; i < nRecords; ++i)
    {
        uint32_t cols, rows; uint16_t bands;
        std::memcpy(&bands, buf.data() + off + 12, 2);
        std::memcpy(&cols, buf.data() + off + 24, 4);
        std::memcpy(&rows, buf.data() + off + 28, 4);
        off += 32;
        Raster::TileSpec spec{Raster::WireFormat::Binary, Raster::CellType::Float64, cols, rows, noData, static_cast<uint8_t>(bands)};
        const uint64_t blobSize = Raster::encodedSizeBinary(spec);
        std::vector<double> scratch(spec.cellCount());
        Raster::GridView view;
        auto st = Raster::decode(reinterpret_cast<const int8_t*>(buf.data() + off), blobSize, spec, scratch.data(), scratch.size(), view);
        if (st != Raster::CodecStatus::Ok) { std::printf("record %u: decode status %d\n", i, static_cast<int>(st)); return 1; }
        std::vector<int8_t> re(blobSize); uint64_t written = 0;
        st = Raster::encode(view, spec, re.data(), re.size(), written);
        if (st != Raster::CodecStatus::Ok || written != blobSize || std::memcmp(re.data(), buf.data() + off, blobSize) != 0)
        { std::printf("record %u: re-encode differs (status %d)\n", i, static_cast<int>(st)); return 1; }
        for (uint64_t k = 0; k < view.cellCount(); ++k)
        { if (view.isNoData(view.cells[k])) ++nan; else { ++valid; sum += view.cells[k]; } }
        off += blobSize;
    }
    if (off != buf.size()) { std::puts("trailing bytes"); return 1; }
    std::printf("OK records=%u valid=%llu nodata=%llu sum=%.17g\n", nRecords, (unsigned long long)valid, (unsigned long long)nan, sum);
    return 0;
}
