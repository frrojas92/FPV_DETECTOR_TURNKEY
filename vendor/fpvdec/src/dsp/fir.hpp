#pragma once

#include <algorithm>
#include <cmath>
#include <complex>
#include <cstddef>
#include <type_traits>
#include <vector>

#if defined(__AVX2__)
#include <immintrin.h>
#endif

namespace famidec {

// Windowed-sinc FIR design (Hamming window).
inline std::vector<float> design_lowpass(double cutoff_hz, double sample_rate,
                                         int taps) {
    // taps must be odd for a symmetric type-I filter
    if ((taps & 1) == 0) ++taps;
    std::vector<float> h(taps);
    double fc = cutoff_hz / sample_rate;  // normalized (0..0.5)
    int m = taps - 1;
    double sum = 0.0;
    for (int i = 0; i < taps; ++i) {
        double n = i - m / 2.0;
        double sinc = (n == 0.0) ? 2.0 * fc
                                 : std::sin(2.0 * M_PI * fc * n) / (M_PI * n);
        double w = 0.54 - 0.46 * std::cos(2.0 * M_PI * i / m);
        h[i] = static_cast<float>(sinc * w);
        sum += h[i];
    }
    for (auto& v : h) v = static_cast<float>(v / sum);  // unity DC gain
    return h;
}

inline std::vector<float> design_bandpass(double f_lo, double f_hi,
                                          double sample_rate, int taps) {
    if ((taps & 1) == 0) ++taps;
    double fc = 0.5 * (f_lo + f_hi);
    double half_bw = 0.5 * (f_hi - f_lo);
    auto lp = design_lowpass(half_bw, sample_rate, taps);
    // Modulate lowpass prototype up to center frequency; x2 to keep passband
    // gain at unity for a real bandpass.
    std::vector<float> h(lp.size());
    int m = static_cast<int>(lp.size()) - 1;
    for (size_t i = 0; i < lp.size(); ++i) {
        double n = static_cast<double>(i) - m / 2.0;
        h[i] = static_cast<float>(
            2.0 * lp[i] * std::cos(2.0 * M_PI * fc / sample_rate * n));
    }
    return h;
}

// Streaming FIR over a contiguous block, with history carried across calls.
// Taps are stored reversed so the inner loop is a straight dot product the
// compiler can auto-vectorize.
template <typename Sample>
class FirFilter {
public:
    explicit FirFilter(std::vector<float> taps)
        : taps_(std::move(taps)), hist_(taps_.size() - 1, Sample{}) {
        std::reverse(taps_.begin(), taps_.end());
    }

    size_t delay() const { return (taps_.size() - 1) / 2; }

    // in/out may be the same buffer.
    void process(const Sample* in, Sample* out, size_t n) {
        size_t nt = taps_.size();
        size_t nh = nt - 1;
        // Work buffer: history + new samples
        work_.resize(nh + n);
        std::copy(hist_.begin(), hist_.end(), work_.begin());
        std::copy(in, in + n, work_.begin() + nh);
        const float* t = taps_.data();
        size_t i = 0;
#if defined(__AVX2__)
        // 8 outputs per iteration, one FMA per tap. Explicit intrinsics:
        // MSVC's auto-vectorizer leaves 4-5x on the table here, and this
        // kernel is most of the 20 MSPS budget.
        if constexpr (std::is_same_v<Sample, float>) {
            for (; i + 8 <= n; i += 8) {
                __m256 acc = _mm256_setzero_ps();
                const float* w = work_.data() + i;
                for (size_t k = 0; k < nt; ++k)
                    acc = _mm256_fmadd_ps(_mm256_set1_ps(t[k]),
                                          _mm256_loadu_ps(w + k), acc);
                _mm256_storeu_ps(out + i, acc);
            }
        }
#endif
        // Register-blocked portable kernel (and the tail of the AVX2 path):
        // B outputs accumulated together turns the inner loop into a per-tap
        // SIMD axpy (no reduction), which auto-vectorizes far better than a
        // per-output dot product.
        constexpr size_t B = 8;
        for (; i + B <= n; i += B) {
            Sample acc[B] = {};
            const Sample* w = work_.data() + i;
            for (size_t k = 0; k < nt; ++k) {
                float tk = t[k];
                for (size_t j = 0; j < B; ++j) acc[j] += w[k + j] * tk;
            }
            for (size_t j = 0; j < B; ++j) out[i + j] = acc[j];
        }
        for (; i < n; ++i) {
            Sample acc{};
            const Sample* w = work_.data() + i;
            for (size_t k = 0; k < nt; ++k) acc += w[k] * t[k];
            out[i] = acc;
        }
        std::copy(work_.end() - nh, work_.end(), hist_.begin());
    }

private:
    std::vector<float> taps_;
    std::vector<Sample> hist_;
    std::vector<Sample> work_;
};

using FirFilterF = FirFilter<float>;

// Complex FIR as two planar real FIRs. Filtering the real and imaginary
// planes separately vectorizes far better than accumulating interleaved
// std::complex in the inner loop.
class FirFilterC {
public:
    explicit FirFilterC(std::vector<float> taps)
        : re_(taps), im_(std::move(taps)) {}

    size_t delay() const { return re_.delay(); }

    // in/out may be the same buffer.
    void process(const std::complex<float>* in, std::complex<float>* out,
                 size_t n) {
        pre_.resize(n);
        pim_.resize(n);
        for (size_t i = 0; i < n; ++i) {
            pre_[i] = in[i].real();
            pim_[i] = in[i].imag();
        }
        re_.process(pre_.data(), pre_.data(), n);
        im_.process(pim_.data(), pim_.data(), n);
        for (size_t i = 0; i < n; ++i) out[i] = {pre_[i], pim_[i]};
    }

private:
    FirFilterF re_, im_;  // re_ copies taps, im_ takes the moved-from vector
    std::vector<float> pre_, pim_;
};

}  // namespace famidec
