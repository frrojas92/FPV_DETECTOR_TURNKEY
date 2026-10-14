#pragma once

#include <cmath>
#include <complex>
#include <cstddef>
#include <vector>

#if defined(__AVX2__)
#include <immintrin.h>
#endif

namespace famidec {

// Polynomial atan2 approximation, |err| < 0.0015 rad. At the default
// deviation that is ~0.2 IRE — invisible — while std::atan2 per sample is
// one of the hottest spots of the whole 20 MSPS chain.
inline float fast_atan2f(float y, float x) {
    float ax = std::fabs(x), ay = std::fabs(y);
    float mx = ax > ay ? ax : ay;
    float mn = ax > ay ? ay : ax;
    if (mx == 0.0f) return 0.0f;
    float a = mn / mx;
    float s = a * a;
    float r = ((-0.0464964749f * s + 0.15931422f) * s - 0.327622764f) * s * a + a;
    if (ay > ax) r = 1.57079637f - r;
    if (x < 0.0f) r = 3.14159274f - r;
    return y < 0.0f ? -r : r;
}

#if defined(__AVX2__)
// 8-wide branchless version of fast_atan2f (same polynomial, quadrant fixup
// via blends, sign transferred from y).
inline __m256 fast_atan2_avx(__m256 y, __m256 x) {
    const __m256 absmask =
        _mm256_castsi256_ps(_mm256_set1_epi32(0x7fffffff));
    __m256 ax = _mm256_and_ps(x, absmask);
    __m256 ay = _mm256_and_ps(y, absmask);
    __m256 mx = _mm256_max_ps(ax, ay);
    __m256 mn = _mm256_min_ps(ax, ay);
    // mx==0 (both inputs zero): clamp the divisor; a becomes 0 -> r = 0.
    __m256 a = _mm256_div_ps(mn, _mm256_max_ps(mx, _mm256_set1_ps(1e-30f)));
    __m256 s = _mm256_mul_ps(a, a);
    __m256 r = _mm256_fmadd_ps(_mm256_set1_ps(-0.0464964749f), s,
                               _mm256_set1_ps(0.15931422f));
    r = _mm256_fmadd_ps(r, s, _mm256_set1_ps(-0.327622764f));
    r = _mm256_fmadd_ps(_mm256_mul_ps(r, s), a, a);
    __m256 m = _mm256_cmp_ps(ay, ax, _CMP_GT_OQ);
    r = _mm256_blendv_ps(r, _mm256_sub_ps(_mm256_set1_ps(1.57079637f), r), m);
    m = _mm256_cmp_ps(x, _mm256_setzero_ps(), _CMP_LT_OQ);
    r = _mm256_blendv_ps(r, _mm256_sub_ps(_mm256_set1_ps(3.14159274f), r), m);
    __m256 sign =
        _mm256_and_ps(y, _mm256_castsi256_ps(_mm256_set1_epi32(0x80000000)));
    return _mm256_xor_ps(r, sign);
}
#endif

// FM quadrature discriminator (differentiate the instantaneous phase).
//
// For each sample: p = in[i] * conj(prev); the argument of p is the phase
// advance between consecutive samples, i.e. the instantaneous frequency.
//
// out[i] = -atan2(im, re) * scale   (default, invert=false)
//
// The sign is INVERTED by default on purpose. In FM-ATV the sync tip sits at
// the lowest transmitted frequency (largest negative deviation), so a plain
// discriminator would output sync tips as the signal MINIMUM. The downstream
// AGC (agc.hpp) assumes a negative-modulation AM envelope where sync tip =
// signal MAXIMUM. Flipping the discriminator sign makes sync tips positive
// again, so the existing AGC / sync-separator / NTSC decoder / chroma demod
// all work unchanged.
//
// --invert (invert=true) flips it back, for non-standard VTX whose modulation
// polarity is reversed (sync tip at the highest frequency).
class FmDetector {
public:
    // scale_ = sample_rate / (2*pi*dev_hz): normalizes so that a +dev_hz
    // deviation maps to +1.0 at the output (before AGC re-scales to IRE).
    FmDetector(double sample_rate, double dev_hz, bool invert)
        : scale_(static_cast<float>(sample_rate /
                                    (2.0 * M_PI * dev_hz))),
          invert_(invert) {}

    void process(const std::complex<float>* in, float* out, size_t n) {
        if (n == 0) return;
        // Pass 1: p[i] = in[i] * conj(in[i-1]) into planar arrays. No serial
        // dependency (prev is just the neighbouring input sample).
        pre_.resize(n);
        pim_.resize(n);
        const float* v = reinterpret_cast<const float*>(in);
        pre_[0] = v[0] * prev_.real() + v[1] * prev_.imag();
        pim_[0] = v[1] * prev_.real() - v[0] * prev_.imag();
        for (size_t i = 1; i < n; ++i) {
            float xr = v[2 * i], xi = v[2 * i + 1];
            float pr = v[2 * i - 2], pi = v[2 * i - 1];
            pre_[i] = xr * pr + xi * pi;
            pim_[i] = xi * pr - xr * pi;
        }
        prev_ = in[n - 1];
        // Pass 2: phase = atan2, scaled. The atan2 is the expensive part, so
        // it runs 8-wide where available.
        const float g = (invert_ ? 1.0f : -1.0f) * scale_;
        size_t i = 0;
#if defined(__AVX2__)
        const __m256 vg = _mm256_set1_ps(g);
        for (; i + 8 <= n; i += 8)
            _mm256_storeu_ps(
                out + i,
                _mm256_mul_ps(vg, fast_atan2_avx(_mm256_loadu_ps(&pim_[i]),
                                                 _mm256_loadu_ps(&pre_[i]))));
#endif
        for (; i < n; ++i) out[i] = g * fast_atan2f(pim_[i], pre_[i]);
    }

private:
    std::complex<float> prev_{1.0f, 0.0f};
    float scale_;
    bool invert_;
    std::vector<float> pre_, pim_;
};

}  // namespace famidec
