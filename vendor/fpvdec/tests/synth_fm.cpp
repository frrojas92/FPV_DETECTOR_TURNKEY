// Golden test for the FPV FM path: synthesize an FM-ATV NTSC color-bar
// signal as int8 IQ (composite frequency-modulates the carrier, sync tip at
// the lowest frequency), run it through the same DSP chain as the live FM
// pipeline, and assert the decoded RGB values match the transmitted bars.
#include <algorithm>
#include <cmath>
#include <complex>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <vector>

#include "config.hpp"
#include "dsp/dc_blocker.hpp"
#include "dsp/fir.hpp"
#include "dsp/fm_detector.hpp"
#include "dsp/frame.hpp"
#include "dsp/ntsc_decoder.hpp"

using namespace famidec;

namespace {

// Default pipeline rate; deviation matches a real 5.8 GHz VTX (~2.5 MHz
// peak — +-5 MHz would sit at the 10 MSPS Nyquist edge where the phase
// step +-pi is ambiguous).
constexpr double kFs = 10e6;
constexpr double kDev = 2.5e6;
constexpr double kFsc = 315e6 / 88.0;
constexpr double kLineUs = 1e6 / 15734.264;  // 63.555 us
constexpr int kLinesPerField = 262;
constexpr int kVsyncLines = 3;
constexpr int kPostVsyncBlank = 13;  // matches NtscDecoder::kActiveStartLine

struct Bar {
    float r, g, b;  // 0..1
};
const Bar kBars[7] = {
    {0.75f, 0.75f, 0.75f}, {0.75f, 0.75f, 0.0f}, {0.0f, 0.75f, 0.75f},
    {0.0f, 0.75f, 0.0f},   {0.75f, 0.0f, 0.75f}, {0.75f, 0.0f, 0.0f},
    {0.0f, 0.0f, 0.75f},
};

void yuv_of(const Bar& bar, float* y, float* u, float* v) {
    *y = 0.299f * bar.r + 0.587f * bar.g + 0.114f * bar.b;
    *u = 0.492f * (bar.b - *y);
    *v = 0.877f * (bar.r - *y);
}

// Composite IRE value at a given position within a field.
float composite_ire(int line, double us, double theta) {
    bool vsync_line = line < kVsyncLines;
    if (vsync_line) {
        // broad pulse: asserted except a 4.7 us serration at end of line
        return (us > kLineUs - 4.7) ? 0.0f : -40.0f;
    }
    if (us < 4.7) return -40.0f;                       // hsync
    if (us >= 5.3 && us < 7.8)                          // burst on back porch
        return -20.0f * static_cast<float>(std::sin(theta));
    if (us < 9.4 || us >= 62.0) return 0.0f;            // porches
    int active_line = line - kVsyncLines - kPostVsyncBlank;
    if (active_line < 0 || active_line >= 240) return 0.0f;  // blank line
    double frac = (us - 9.4) / 52.6;
    int bar_idx = std::min(6, static_cast<int>(frac * 7.0));
    float y, u, v;
    yuv_of(kBars[bar_idx], &y, &u, &v);
    float chroma = u * static_cast<float>(std::sin(theta)) +
                   v * static_cast<float>(std::cos(theta));
    return (y + chroma) * 100.0f;
}

}  // namespace

int main(int argc, char** argv) {
    Config cfg;
    cfg.sample_rate = kFs;
    cfg.offset_hz = 0.0;
    cfg.mode = Config::Mode::Color;
    cfg.fm_dev_hz = kDev;

    const int fields = 30;
    const size_t total = static_cast<size_t>(fields * kLinesPerField *
                                             kLineUs * kFs / 1e6);
    // FM modulate: sync tip (-40 IRE) -> -dev, white (+100 IRE) -> +dev.
    std::vector<uint8_t> iq(total * 2);
    const double omega_sc = 2.0 * M_PI * kFsc / kFs;
    const double samples_per_line = kFs * kLineUs / 1e6;
    const double samples_per_field = samples_per_line * kLinesPerField;
    double phase = 0.0;
    for (size_t n = 0; n < total; ++n) {
        double in_field = std::fmod(static_cast<double>(n), samples_per_field);
        int line = static_cast<int>(in_field / samples_per_line);
        double us = std::fmod(in_field, samples_per_line) / kFs * 1e6;
        double theta = std::fmod(omega_sc * static_cast<double>(n), 2.0 * M_PI);
        float ire = composite_ire(line, us, theta);
        double f = kDev * ((ire + 40.0) / 70.0 - 1.0);
        phase = std::fmod(phase + 2.0 * M_PI * f / kFs, 2.0 * M_PI);
        auto clip8 = [](double x) {
            return static_cast<int8_t>(std::lround(std::fmax(-127.0, std::fmin(127.0, x))));
        };
        iq[2 * n] = static_cast<uint8_t>(clip8(100.0 * std::cos(phase)));
        iq[2 * n + 1] = static_cast<uint8_t>(clip8(100.0 * std::sin(phase)));
    }

    // Optional: dump the synthetic IQ as .cs8 for end-to-end fpvdec tests.
    if (argc > 1) {
        std::FILE* fp = std::fopen(argv[1], "wb");
        if (!fp) return 1;
        std::fwrite(iq.data(), 1, iq.size(), fp);
        std::fclose(fp);
        std::printf("wrote %zu IQ samples to %s\n", total, argv[1]);
        return 0;
    }

    // Same chain as the live FM pipeline (default: no post-detector LPF).
    TripleBuffer tb;
    NtscDecoder dec(cfg, tb);
    DcBlocker dcb;
    FirFilterC chan_lpf(design_lowpass(std::min(8.0e6, kFs * 0.49), kFs, 47));
    FmDetector fm_det(kFs, cfg.fm_dev_hz, cfg.invert);

    constexpr size_t kBlock = 32768;
    std::vector<std::complex<float>> cbuf(kBlock);
    std::vector<float> comp(kBlock);
    for (size_t off = 0; off < total; off += kBlock) {
        size_t ns = std::min(kBlock, total - off);
        for (size_t i = 0; i < ns; ++i) {
            std::complex<float> c(
                static_cast<int8_t>(iq[2 * (off + i)]) / 128.0f,
                static_cast<int8_t>(iq[2 * (off + i) + 1]) / 128.0f);
            cbuf[i] = dcb.process(c);
        }
        chan_lpf.process(cbuf.data(), cbuf.data(), ns);
        fm_det.process(cbuf.data(), comp.data(), ns);
        dec.process(comp.data(), ns);
    }

    uint64_t frames = dec.stats().frames.load();
    std::printf("decoded frames: %llu, lines: %llu, coasted: %llu\n",
                static_cast<unsigned long long>(frames),
                static_cast<unsigned long long>(dec.stats().lines.load()),
                static_cast<unsigned long long>(dec.stats().lines_coasted.load()));
    if (frames < 10) {
        std::printf("FAIL: fewer than 10 frames decoded\n");
        return 1;
    }
    const Frame* f = tb.acquire();
    if (!f) {
        std::printf("FAIL: no frame available\n");
        return 1;
    }

    int failures = 0;
    for (int b = 0; b < 7; ++b) {
        // sample at the center of each bar, mid-height
        int px = static_cast<int>((b + 0.5) / 7.0 * Frame::kWidth);
        int py = Frame::kHeight / 2;
        uint32_t p = f->rgba[static_cast<size_t>(py) * Frame::kWidth + px];
        int r = p & 0xff, g = (p >> 8) & 0xff, bl = (p >> 16) & 0xff;
        int er = static_cast<int>(kBars[b].r * 255.0f + 0.5f);
        int eg = static_cast<int>(kBars[b].g * 255.0f + 0.5f);
        int eb = static_cast<int>(kBars[b].b * 255.0f + 0.5f);
        int tol = 35;
        bool ok = std::abs(r - er) <= tol && std::abs(g - eg) <= tol &&
                  std::abs(bl - eb) <= tol;
        std::printf("bar %d: got (%3d,%3d,%3d) expect (%3d,%3d,%3d) %s\n", b,
                    r, g, bl, er, eg, eb, ok ? "OK" : "FAIL");
        if (!ok) ++failures;
    }
    if (failures) {
        std::printf("FAIL: %d bars out of tolerance\n", failures);
        return 1;
    }
    std::printf("PASS\n");
    return 0;
}
