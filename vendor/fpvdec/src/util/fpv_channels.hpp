#pragma once

#include <cctype>
#include <cmath>
#include <string>

namespace famidec {

// 5.8 GHz analog FPV channel table (MHz).
inline constexpr char kFpvBands[5] = {'A', 'B', 'E', 'F', 'R'};
inline constexpr double kFpvMhz[5][8] = {
    {5865, 5845, 5825, 5805, 5785, 5765, 5745, 5725},  // A: Boscam A
    {5733, 5752, 5771, 5790, 5809, 5828, 5847, 5866},  // B: Boscam B
    {5705, 5685, 5665, 5645, 5885, 5905, 5925, 5945},  // E: Boscam E / DJI
    {5740, 5760, 5780, 5800, 5820, 5840, 5860, 5880},  // F: Airwave / Fatshark
    {5658, 5695, 5732, 5769, 5806, 5843, 5880, 5917},  // R: Raceband
};

// "F4" / "r8" -> carrier Hz. False if not a valid band+channel pair.
inline bool fpv_channel_freq(const std::string& name, double* hz) {
    if (name.size() != 2) return false;
    char b = static_cast<char>(std::toupper(static_cast<unsigned char>(name[0])));
    int band = -1;
    for (int i = 0; i < 5; ++i)
        if (kFpvBands[i] == b) band = i;
    if (band < 0 || name[1] < '1' || name[1] > '8') return false;
    *hz = kFpvMhz[band][name[1] - '1'] * 1e6;
    return true;
}

// Nearest channel name for a carrier frequency, e.g. "F4". Empty string when
// nothing is within tol_hz (real VTX drift a few hundred kHz; adjacent
// channels are >= ~2 MHz apart, so 1 MHz keeps the label honest).
inline std::string fpv_nearest_channel(double hz, double tol_hz = 1.0e6) {
    double best = tol_hz;
    std::string name;
    for (int b = 0; b < 5; ++b)
        for (int c = 0; c < 8; ++c) {
            double d = std::abs(hz - kFpvMhz[b][c] * 1e6);
            if (d <= best) {
                best = d;
                name = std::string(1, kFpvBands[b]) +
                       static_cast<char>('1' + c);
            }
        }
    return name;
}

}  // namespace famidec
