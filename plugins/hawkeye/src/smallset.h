// MIT License
//
// Copyright (c) 2019 Ruhr University Bochum, Chair for Embedded Security. All Rights reserved.
// Copyright (c) 2019 Marc Fyrbiak, Sebastian Wallat, Max Hoffmann ("ORIGINAL AUTHORS"). All rights reserved.
// Copyright (c) 2021 Max Planck Institute for Security and Privacy. All Rights reserved.
// Copyright (c) 2021 Jörn Langheinrich, Julian Speith, Nils Albartus, René Walendy, Simon Klix ("ORIGINAL AUTHORS"). All Rights reserved.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

/**
 * @file smallset.h
 * @brief Set of up to 256 small integers backed by AVX2, NEON, or plain 64-bit words.
 *
 * The S-box canonical form search spends nearly all of its time in these sets, which is why there
 * is one implementation per instruction set. `smallset_portable_t` is compiled on every platform
 * and defines the semantics the vectorized backends have to reproduce, so the unit tests can diff
 * whichever backend the machine selected against it. The header is internal to the plugin; every
 * definition is TU-local so that no backend can leak across a binary boundary.
 */

#pragma once

#include "hal_core/defines.h"

#include <cstdio>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

// debug options to enforce using default implementation
// #undef __AVX2__
// #undef __ARM_NEON

#ifdef __AVX2__
#include <immintrin.h>
#elif defined(__ARM_NEON)
#include <arm_neon.h>
#endif

namespace hal
{
    namespace hawkeye
    {
        namespace detail
        {
            namespace
            {
                constexpr u64 _ONE_ = 1;

                /**
                 * Reference implementation of the set, used wherever no vector unit is available and as the
                 * behavioral specification of the AVX2 and NEON backends in the tests.
                 */
                class smallset_portable_t
                {
                public:
                    smallset_portable_t(int preset = 0);

                    void set(u8 bit);
                    bool is_set(u8 bit) const;

                    void dump() const;

                    smallset_portable_t operator|(const smallset_portable_t& other) const;
                    smallset_portable_t operator&(const smallset_portable_t& other) const;
                    smallset_portable_t operator^(const smallset_portable_t& other) const;
                    smallset_portable_t shuffle(u8 shift) const;

                    void to_array(u64* arr, bool swap = false) const;

                    u8 least_bit() const;
                    static int least_bit(u64 dw);

                    int size() const;
                    static int size(u64 dw, int level);

                    bool empty() const;

                private:
                    u64 dw64[4];
                };

                inline smallset_portable_t::smallset_portable_t(int preset)
                {
                    memset(dw64, 0, sizeof(dw64));
                    switch (preset)
                    {
                        case 8:
                            dw64[0] = 0xFF;
                            break;
                        case 16:
                            dw64[0] = 0xFFFF;
                            break;
                        case 32:
                            dw64[0] = 0xFFFFFFFF;
                            break;
                        case 64:
                            memset(dw64, 0xFF, sizeof(u64));
                            break;
                        case 128:
                            memset(dw64, 0xFF, 2 * sizeof(u64));
                            break;
                        case 256:
                            memset(dw64, 0xFF, sizeof(dw64));
                            break;
                    }
                }

                inline bool smallset_portable_t::empty() const
                {
                    for (int i = 0; i < 4; i++)
                    {
                        if (dw64[i])
                        {
                            return false;
                        }
                    }
                    return true;
                }

                inline u8 smallset_portable_t::least_bit() const
                {
                    for (int i = 0; i < 4; i++)
                    {
                        if (dw64[i])
                        {
                            return i * 64 + least_bit(dw64[i]);
                        }
                    }
                    std::cerr << "Called smallset_portable_t::least_bit() on empty set\n" << std::endl;
                    return 0;
                }

                inline smallset_portable_t smallset_portable_t::shuffle(u8 shift) const
                {
                    smallset_portable_t retval(*this);
                    if (shift & 0x80)
                    {
                        smallset_portable_t temp = retval;
                        retval.dw64[0]           = temp.dw64[2];
                        retval.dw64[1]           = temp.dw64[3];
                        retval.dw64[2]           = temp.dw64[0];
                        retval.dw64[3]           = temp.dw64[1];
                    }
                    if (shift & 0x40)
                    {
                        smallset_portable_t temp = retval;
                        retval.dw64[0]           = temp.dw64[1];
                        retval.dw64[1]           = temp.dw64[0];
                        retval.dw64[2]           = temp.dw64[3];
                        retval.dw64[3]           = temp.dw64[2];
                    }
                    if (shift & 0x20)
                    {
                        for (int i = 0; i < 4; i++)
                        {
                            retval.dw64[i] = ((retval.dw64[i] & 0xFFFFFFFF00000000ULL) >> 32) | ((retval.dw64[i] & 0x00000000FFFFFFFFULL) << 32);
                        }
                    }
                    if (shift & 0x10)
                    {
                        for (int i = 0; i < 4; i++)
                        {
                            retval.dw64[i] = ((retval.dw64[i] & 0xFFFF0000FFFF0000ULL) >> 16) | ((retval.dw64[i] & 0x0000FFFF0000FFFFULL) << 16);
                        }
                    }
                    if (shift & 0x08)
                    {
                        for (int i = 0; i < 4; i++)
                        {
                            retval.dw64[i] = ((retval.dw64[i] & 0xFF00FF00FF00FF00ULL) >> 8) | ((retval.dw64[i] & 0x00FF00FF00FF00FFULL) << 8);
                        }
                    }
                    if (shift & 0x04)
                    {
                        for (int i = 0; i < 4; i++)
                        {
                            retval.dw64[i] = ((retval.dw64[i] & 0xF0F0F0F0F0F0F0F0ULL) >> 4) | ((retval.dw64[i] & 0x0F0F0F0F0F0F0F0FULL) << 4);
                        }
                    }
                    if (shift & 0x02)
                    {
                        for (int i = 0; i < 4; i++)
                        {
                            retval.dw64[i] = ((retval.dw64[i] & 0xCCCCCCCCCCCCCCCCULL) >> 2) | ((retval.dw64[i] & 0x3333333333333333ULL) << 2);
                        }
                    }
                    if (shift & 0x01)
                    {
                        for (int i = 0; i < 4; i++)
                        {
                            retval.dw64[i] = ((retval.dw64[i] & 0xAAAAAAAAAAAAAAAAULL) >> 1) | ((retval.dw64[i] & 0x5555555555555555ULL) << 1);
                        }
                    }
                    return retval;
                }

                inline void smallset_portable_t::to_array(u64* arr, bool swap) const
                {
                    for (int i = 0; i < 4; i++)
                    {
                        arr[i] = dw64[swap ? 3 - i : i];
                    }
                }

                inline void smallset_portable_t::set(u8 bit)
                {
                    dw64[bit / 64] |= (_ONE_ << (bit % 64));
                }

                inline bool smallset_portable_t::is_set(u8 bit) const
                {
                    return (dw64[bit / 64] & (_ONE_ << (bit % 64))) != 0;
                }

                inline int smallset_portable_t::size() const
                {
                    int retval = 0;
                    for (int i = 0; i < 4; i++)
                        retval += size(dw64[i], 0);
                    return retval;
                }

                inline int smallset_portable_t::size(u64 dw, int level)
                {
                    static const u64 segmask[]    = {0xFFFFFFFF, 0xFFFF, 0xFF, 0xF};
                    static const int segshft[]    = {32, 16, 8, 4};
                    static const int szlookup[16] = {0, 1, 1, 2, 1, 2, 2, 3, 1, 2, 2, 3, 2, 3, 3, 4};

                    if (level >= 4)
                        return szlookup[dw & 0xF];

                    int retval = 0;
                    retval += size(dw & segmask[level], level + 1);
                    dw >>= segshft[level];
                    retval += size(dw & segmask[level], level + 1);

                    return retval;
                }

                inline int smallset_portable_t::least_bit(u64 dw)
                {
                    static const u64 segmask[]    = {0xFFFFFFFF, 0xFFFF, 0xFF, 0xF};
                    static const int lblookup[16] = {-61, 0, 1, 0, 2, 0, 1, 0, 3, 0, 1, 0, 2, 0, 1, 0};

                    int retval = 0;
                    int segval = 32;

                    for (int iseg = 0; iseg < 4; iseg++)
                    {
                        if (!(dw & segmask[iseg]))
                        {
                            retval += segval;
                            dw >>= segval;
                        }
                        segval /= 2;
                    }

                    return retval + lblookup[dw & 0xF];
                }

                inline void smallset_portable_t::dump() const
                {
                    for (int i = 3; i >= 0; i--)
                        printf("%016" PRIx64, dw64[i]);
                    printf("\n");

                    for (unsigned int i = 0; i < 256; i++)
                    {
                        if (dw64[i / 64] & (_ONE_ << (i % 64)))
                            printf("%8d\n", i);
                    }
                }

                inline smallset_portable_t smallset_portable_t::operator|(const smallset_portable_t& other) const
                {
                    smallset_portable_t retval = other;
                    for (int i = 0; i < 4; i++)
                        retval.dw64[i] |= dw64[i];
                    return retval;
                }

                inline smallset_portable_t smallset_portable_t::operator&(const smallset_portable_t& other) const
                {
                    smallset_portable_t retval = other;
                    for (int i = 0; i < 4; i++)
                        retval.dw64[i] &= dw64[i];
                    return retval;
                }

                inline smallset_portable_t smallset_portable_t::operator^(const smallset_portable_t& other) const
                {
                    smallset_portable_t retval = other;
                    for (int i = 0; i < 4; i++)
                        retval.dw64[i] ^= dw64[i];
                    return retval;
                }

#ifdef __AVX2__
                using smallset_t = __m256i;

                /// name of the backend the current translation unit was compiled for, for diagnostics in tests
                constexpr const char* SMALLSET_BACKEND = "avx2";
#elif defined(__ARM_NEON)
                using smallset_t = uint64x2x2_t;

                constexpr const char* SMALLSET_BACKEND = "neon";
#else
                using smallset_t = smallset_portable_t;

                constexpr const char* SMALLSET_BACKEND = "portable";
#endif

                /**
                 * Read the set out as four 64-bit words, word 0 holding the elements 0 to 63.
                 *
                 * This is the only place that takes a vector apart, so every other operation below works on
                 * whole vectors and the tests can compare any backend against the portable one word by word.
                 */
                inline void smallset_to_chunks(const smallset_t& a, u64* chunks)
                {
#ifdef __AVX2__
                    chunks[0] = _mm256_extract_epi64(a, 0);
                    chunks[1] = _mm256_extract_epi64(a, 1);
                    chunks[2] = _mm256_extract_epi64(a, 2);
                    chunks[3] = _mm256_extract_epi64(a, 3);
#elif defined(__ARM_NEON)
                    chunks[0] = vgetq_lane_u64(a.val[0], 0);
                    chunks[1] = vgetq_lane_u64(a.val[0], 1);
                    chunks[2] = vgetq_lane_u64(a.val[1], 0);
                    chunks[3] = vgetq_lane_u64(a.val[1], 1);
#else
                    a.to_array(chunks);
#endif
                }

                inline void smallset_print(const std::string& name, const smallset_t& a)
                {
                    u64 chunks[4];
                    smallset_to_chunks(a, chunks);

                    std::cout << name << ": 0b";
                    for (int i = 3; i >= 0; i--)
                    {
                        for (int j = 63; j >= 0; j--)
                        {
                            u32 bit = (chunks[i] >> j) & 1;
                            std::cout << bit;
                        }
                        std::cout << " ";
                    }
                    std::cout << std::endl;
                }

                inline u8 smallset_least_element(const smallset_t& a)
                {
                    u64 chunks[4];
                    smallset_to_chunks(a, chunks);

                    for (u32 i = 0; i < 4; i++)
                    {
                        u64 current_chunk = chunks[i];
                        if (current_chunk != 0)
                        {
                            u8 idx = __builtin_ctzll(current_chunk) + i * 64;
                            return idx;
                        }
                    }

                    // set is empty -- caller's fault
                    std::cout << "CALLED LEAST ELEMENT ON EMPTY SET!" << std::endl;
                    return 0;
                }

                inline smallset_t smallset_intersect(const smallset_t& a, const smallset_t& b)
                {
#ifdef __AVX2__
                    return _mm256_and_si256(a, b);
#elif defined(__ARM_NEON)
                    return {vandq_u64(a.val[0], b.val[0]), vandq_u64(a.val[1], b.val[1])};
#else
                    return (a & b);
#endif
                }

                inline smallset_t smallset_union(const smallset_t& a, const smallset_t& b)
                {
#ifdef __AVX2__
                    return _mm256_or_si256(a, b);
#elif defined(__ARM_NEON)
                    return {vorrq_u64(a.val[0], b.val[0]), vorrq_u64(a.val[1], b.val[1])};
#else
                    return (a | b);
#endif
                }

                inline u16 smallset_size(const smallset_t& a)
                {
                    u64 chunks[4];
                    smallset_to_chunks(a, chunks);

                    u16 count = 0;
                    for (u32 i = 0; i < 4; i++)
                    {
                        count += __builtin_popcountll(chunks[i]);
                    }
                    return count;
                }

                inline bool smallset_is_empty(const smallset_t& a)
                {
#ifdef __AVX2__
                    return _mm256_testz_si256(a, a);
#elif defined(__ARM_NEON)
                    // vceqzq_u64 sets a lane to all ones exactly if it was zero, so both halves are zero if
                    // and only if all four lanes of the AND are all ones
                    const uint64x2_t tmp = vandq_u64(vceqzq_u64(a.val[0]), vceqzq_u64(a.val[1]));
                    return (vgetq_lane_u64(tmp, 0) & vgetq_lane_u64(tmp, 1) & 1) != 0;
#else
                    return a.empty();
#endif
                }

                inline smallset_t smallset_add_element(const smallset_t& a, const u8 elm)
                {
                    // compute union of a and {elm}
#if !defined(__AVX2__) && !defined(__ARM_NEON)
                    smallset_t retval(a);
                    retval.set(elm);
                    return retval;
#else
                    const u32 index = elm / 64;
#ifdef __AVX2__
                    u64 mask[4]   = {0};
                    mask[index]   = (u64)1 << (elm % 64);
                    __m256i _mask = _mm256_set_epi64x(mask[3], mask[2], mask[1], mask[0]);
                    return _mm256_or_si256(a, _mask);
#elif defined(__ARM_NEON)
                    u64 mask[2]            = {0};
                    mask[index & 1]        = (u64)1 << (elm % 64);
                    const uint64x2_t _mask = vld1q_u64(mask);
                    if (index < 2)
                    {
                        return {vorrq_u64(a.val[0], _mask), a.val[1]};
                    }
                    else
                    {
                        return {a.val[0], vorrq_u64(a.val[1], _mask)};
                    }
#endif
#endif
                }

                inline smallset_t smallset_shift(const smallset_t& b, const u8 shift)
                {
#if !defined(__AVX2__) && !defined(__ARM_NEON)
                    return b.shuffle(shift);
#else
                    auto a = b;
                    // compute a \oplus shift
                    if ((shift >> 7) & 0x1)
                    {
#ifdef __AVX2__
                        a = _mm256_permute2x128_si256(a, a, 1);
#else
                        a.val[0] = b.val[1];
                        a.val[1] = b.val[0];
#endif
                    }
                    if ((shift >> 6) & 0x1)
                    {
#ifdef __AVX2__
                        a = _mm256_permute4x64_epi64(a, _MM_SHUFFLE(2, 3, 0, 1));
#else
                        a.val[0] = vextq_u64(a.val[0], a.val[0], 1);
                        a.val[1] = vextq_u64(a.val[1], a.val[1], 1);
#endif
                    }
                    if ((shift >> 5) & 0x1)
                    {
#ifdef __AVX2__
                        a = _mm256_shuffle_epi32(a, _MM_SHUFFLE(2, 3, 0, 1));
#else
                        // swap the two 32-bit halves of every 64-bit lane; this used to be written as a
                        // C-style cast between vector types, which only compiles where the implicit
                        // conversion is allowed (-flax-vector-conversions) and which gcc has been reported
                        // to reject on aarch64, so every reinterpretation is spelled out as the intrinsic
                        a.val[0] = vreinterpretq_u64_u32(vrev64q_u32(vreinterpretq_u32_u64(a.val[0])));
                        a.val[1] = vreinterpretq_u64_u32(vrev64q_u32(vreinterpretq_u32_u64(a.val[1])));
#endif
                    }
                    if ((shift >> 4) & 0x1)
                    {
#ifdef __AVX2__
                        a = _mm256_shufflelo_epi16(a, _MM_SHUFFLE(2, 3, 0, 1));
                        a = _mm256_shufflehi_epi16(a, _MM_SHUFFLE(2, 3, 0, 1));
#else
                        // reversing the 16-bit lanes of a 64-bit lane and then its 32-bit halves leaves
                        // every pair of adjacent 16-bit lanes swapped
                        a.val[0] = vreinterpretq_u64_u16(vrev64q_u16(vreinterpretq_u16_u64(a.val[0])));
                        a.val[0] = vreinterpretq_u64_u32(vrev64q_u32(vreinterpretq_u32_u64(a.val[0])));
                        a.val[1] = vreinterpretq_u64_u16(vrev64q_u16(vreinterpretq_u16_u64(a.val[1])));
                        a.val[1] = vreinterpretq_u64_u32(vrev64q_u32(vreinterpretq_u32_u64(a.val[1])));
#endif
                    }
                    if ((shift >> 3) & 0x1)
                    {
#ifdef __AVX2__
                        const __m256i mask = _mm256_set_epi8(14, 15, 12, 13, 10, 11, 8, 9, 6, 7, 4, 5, 2, 3, 0, 1, 14, 15, 12, 13, 10, 11, 8, 9, 6, 7, 4, 5, 2, 3, 0, 1);
                        a                  = _mm256_shuffle_epi8(a, mask);
#else
                        // same trick one level down: reversing the bytes and then the 16-bit lanes swaps
                        // every pair of adjacent bytes
                        a.val[0] = vreinterpretq_u64_u8(vrev64q_u8(vreinterpretq_u8_u64(a.val[0])));
                        a.val[0] = vreinterpretq_u64_u16(vrev64q_u16(vreinterpretq_u16_u64(a.val[0])));
                        a.val[1] = vreinterpretq_u64_u8(vrev64q_u8(vreinterpretq_u8_u64(a.val[1])));
                        a.val[1] = vreinterpretq_u64_u16(vrev64q_u16(vreinterpretq_u16_u64(a.val[1])));
#endif
                    }
                    if ((shift >> 2) & 0x1)
                    {
#ifdef __AVX2__
                        const __m256i mask_high = _mm256_set1_epi8((char)0xF0);
                        const __m256i mask_low  = _mm256_set1_epi8(0x0F);
                        const __m256i high      = _mm256_and_si256(a, mask_high);
                        const __m256i low       = _mm256_and_si256(a, mask_low);
                        a                       = _mm256_or_si256(_mm256_srli_epi16(high, 4), _mm256_slli_epi16(low, 4));
#else
                        const uint64x2_t mask_high = vdupq_n_u64(0xF0F0F0F0F0F0F0F0);
                        const uint64x2_t mask_low  = vdupq_n_u64(0x0F0F0F0F0F0F0F0F);

                        for (u32 i = 0; i < 2; i++)
                        {
                            const uint64x2_t high = vandq_u64(a.val[i], mask_high);
                            const uint64x2_t low  = vandq_u64(a.val[i], mask_low);

                            a.val[i] = vorrq_u64(vshrq_n_u64(high, 4), vshlq_n_u64(low, 4));
                        }
#endif
                    }
                    if ((shift >> 1) & 0x1)
                    {
#ifdef __AVX2__
                        const __m256i mask_high = _mm256_set1_epi8((char)0xCC);
                        const __m256i mask_low  = _mm256_set1_epi8(0x33);
                        const __m256i high      = _mm256_and_si256(a, mask_high);
                        const __m256i low       = _mm256_and_si256(a, mask_low);
                        a                       = _mm256_or_si256(_mm256_srli_epi16(high, 2), _mm256_slli_epi16(low, 2));
#else
                        const uint64x2_t mask_high = vdupq_n_u64(0xCCCCCCCCCCCCCCCC);
                        const uint64x2_t mask_low  = vdupq_n_u64(0x3333333333333333);

                        for (u32 i = 0; i < 2; i++)
                        {
                            const uint64x2_t high = vandq_u64(a.val[i], mask_high);
                            const uint64x2_t low  = vandq_u64(a.val[i], mask_low);

                            a.val[i] = vorrq_u64(vshrq_n_u64(high, 2), vshlq_n_u64(low, 2));
                        }
#endif
                    }
                    if (shift & 0x1)
                    {
#ifdef __AVX2__
                        const __m256i mask_high = _mm256_set1_epi8((char)0xAA);
                        const __m256i mask_low  = _mm256_set1_epi8(0x55);
                        const __m256i high      = _mm256_and_si256(a, mask_high);
                        const __m256i low       = _mm256_and_si256(a, mask_low);
                        a                       = _mm256_or_si256(_mm256_srli_epi16(high, 1), _mm256_slli_epi16(low, 1));
#else
                        const uint64x2_t mask_high = vdupq_n_u64(0xAAAAAAAAAAAAAAAA);
                        const uint64x2_t mask_low  = vdupq_n_u64(0x5555555555555555);

                        for (u32 i = 0; i < 2; i++)
                        {
                            const uint64x2_t high = vandq_u64(a.val[i], mask_high);
                            const uint64x2_t low  = vandq_u64(a.val[i], mask_low);

                            a.val[i] = vorrq_u64(vshrq_n_u64(high, 1), vshlq_n_u64(low, 1));
                        }
#endif
                    }
                    return a;
#endif
                }

                inline smallset_t smallset_shift_union(const smallset_t& a, const u8 shift)
                {
                    smallset_t b = smallset_shift(a, shift);
                    return smallset_union(a, b);
                }

                inline std::vector<u8> smallset_get_elements(const smallset_t& a)
                {
                    std::vector<u8> e;
                    u64 chunks[4];
                    smallset_to_chunks(a, chunks);

                    for (u32 i = 0; i < 4; i++)
                    {
                        u64 current_chunk = chunks[i];
                        while (current_chunk != 0)
                        {
                            u8 idx = __builtin_ctzll(current_chunk) + i * 64;
                            e.push_back(idx);
                            current_chunk &= (current_chunk - 1);
                        }
                    }
                    return e;
                }

                inline smallset_t smallset_init_empty()
                {
#ifdef __AVX2__
                    return _mm256_setzero_si256();
#elif defined(__ARM_NEON)
                    return {vdupq_n_u64(0), vdupq_n_u64(0)};
#else
                    return smallset_t();
#endif
                }

                inline smallset_t smallset_init_full(const u32 len)
                {
#if !defined(__AVX2__) && !defined(__ARM_NEON)
                    return smallset_t(len);
#else
                    // N must be in {256, 128, 64, 32, 16, 8}
                    if (len == 256)
                    {
#ifdef __AVX2__
                        return _mm256_set_epi64x(0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF);
#else
                        return {vdupq_n_u64(0xFFFFFFFFFFFFFFFF), vdupq_n_u64(0xFFFFFFFFFFFFFFFF)};
#endif
                    }
                    else if (len == 128)
                    {
#ifdef __AVX2__
                        return _mm256_set_epi64x(0, 0, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF);
#else
                        return {vdupq_n_u64(0xFFFFFFFFFFFFFFFF), vdupq_n_u64(0)};
#endif
                    }
                    else if (len == 64)
                    {
#ifdef __AVX2__
                        return _mm256_set_epi64x(0, 0, 0, 0xFFFFFFFFFFFFFFFF);
#else
                        const uint64x2_t tmp = vdupq_n_u64(0);
                        return {vsetq_lane_u64(0xFFFFFFFFFFFFFFFF, tmp, 0), vdupq_n_u64(0)};
#endif
                    }
                    else if (len == 32)
                    {
#ifdef __AVX2__
                        return _mm256_set_epi64x(0, 0, 0, 0xFFFFFFFF);
#else
                        const uint64x2_t tmp = vdupq_n_u64(0);
                        return {vreinterpretq_u64_u32(vsetq_lane_u32(0xFFFFFFFF, vreinterpretq_u32_u64(tmp), 0)), vdupq_n_u64(0)};
#endif
                    }
                    else if (len == 16)
                    {
#ifdef __AVX2__
                        return _mm256_set_epi64x(0, 0, 0, 0xFFFF);
#else
                        const uint64x2_t tmp = vdupq_n_u64(0);
                        return {vreinterpretq_u64_u16(vsetq_lane_u16(0xFFFF, vreinterpretq_u16_u64(tmp), 0)), vdupq_n_u64(0)};
#endif
                    }
                    else if (len == 8)
                    {
#ifdef __AVX2__
                        return _mm256_set_epi64x(0, 0, 0, 0xFF);
#else
                        const uint64x2_t tmp = vdupq_n_u64(0);
                        return {vreinterpretq_u64_u8(vsetq_lane_u8(0xFF, vreinterpretq_u8_u64(tmp), 0)), vdupq_n_u64(0)};
#endif
                    }
                    else
                    {
#ifdef __AVX2__
                        return _mm256_set_epi64x(0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF);
#else
                        return {vdupq_n_u64(0xFFFFFFFFFFFFFFFF), vdupq_n_u64(0xFFFFFFFFFFFFFFFF)};
#endif
                    }
#endif
                }

                inline smallset_t smallset_invert(const smallset_t& a, const u32 len)
                {
                    const smallset_t b = smallset_init_full(len);
#ifdef __AVX2__
                    return _mm256_xor_si256(a, b);
#elif defined(__ARM_NEON)
                    return {veorq_u64(a.val[0], b.val[0]), veorq_u64(a.val[1], b.val[1])};
#else
                    return (a ^ b);
#endif
                }

                inline smallset_t smallset_setminus(const smallset_t& a, const smallset_t& b, const u32 len)
                {
                    const smallset_t b_not = smallset_invert(b, len);
                    return smallset_intersect(a, b_not);
                }

                inline bool smallset_elm_is_in_set(const u8 e, const smallset_t& a)
                {
#if !defined(__AVX2__) && !defined(__ARM_NEON)
                    return a.is_set(e);
#else
                    smallset_t b = smallset_init_empty();
                    b            = smallset_add_element(b, e);
                    b            = smallset_intersect(a, b);
                    return !smallset_is_empty(b);
#endif
                }
            }    // namespace
        }        // namespace detail
    }            // namespace hawkeye
}    // namespace hal
