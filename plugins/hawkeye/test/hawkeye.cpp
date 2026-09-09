#include "hawkeye/sbox_database.h"
#include "smallset.h"
#include "test_def.h"

#include <algorithm>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <vector>

namespace hal
{
    namespace hawkeye
    {
        /**
         * Tests for the S-box canonical form search and for the set of small integers it is built on.
         *
         * The set has one implementation per instruction set (AVX2, NEON, plain 64-bit words), selected at
         * compile time, so a mistake in one backend is invisible on every machine that does not select it.
         * The first half of this suite therefore runs each operation on the backend the build picked and on
         * the portable implementation and compares the resulting bit patterns: on an x86 build that checks
         * AVX2 against the reference, on an ARM build the NEON intrinsics, and it is the only place where a
         * wrong `vreinterpretq` or shuffle constant shows up as a failure rather than as a slightly wrong
         * S-box lookup. The second half checks the database on top of it.
         */
        class HawkeyeSBoxTest : public ::testing::Test
        {
        protected:
            using smallset_t          = detail::smallset_t;
            using smallset_portable_t = detail::smallset_portable_t;

            /** Deterministic pseudo-random element sets, plus the edge cases around the 64-bit lane borders. */
            static std::vector<std::vector<u8>> sample_element_sets()
            {
                std::vector<std::vector<u8>> samples = {
                    {},
                    {0},
                    {1},
                    {31},
                    {32},
                    {63},
                    {64},
                    {127},
                    {128},
                    {191},
                    {192},
                    {255},
                    {0, 1, 2, 3},
                    {0, 63, 64, 127, 128, 191, 192, 255},
                    {7, 8, 15, 16, 23, 24, 39, 40, 71, 72, 135, 136, 199, 200},
                };

                // a linear congruential generator keeps the patterns reproducible across machines and runs
                u32 state = 0x12345678;
                for (u32 set = 0; set < 8; set++)
                {
                    std::vector<u8> elements;
                    for (u32 i = 0; i < 40; i++)
                    {
                        state = state * 1103515245u + 12345u;
                        elements.push_back((u8)((state >> 16) & 0xFF));
                    }
                    std::sort(elements.begin(), elements.end());
                    elements.erase(std::unique(elements.begin(), elements.end()), elements.end());
                    samples.push_back(elements);
                }

                return samples;
            }

            static smallset_t make_backend_set(const std::vector<u8>& elements)
            {
                smallset_t s = detail::smallset_init_empty();
                for (const u8 e : elements)
                {
                    s = detail::smallset_add_element(s, e);
                }
                return s;
            }

            static smallset_portable_t make_portable_set(const std::vector<u8>& elements)
            {
                smallset_portable_t s;
                for (const u8 e : elements)
                {
                    s.set(e);
                }
                return s;
            }

            /** The four 64-bit words of a set, word 0 holding the elements 0 to 63. */
            static std::vector<u64> chunks_of(const smallset_t& s)
            {
                u64 chunks[4];
                detail::smallset_to_chunks(s, chunks);
                return std::vector<u64>(chunks, chunks + 4);
            }

            static std::vector<u64> chunks_of_portable(const smallset_portable_t& s)
            {
                u64 chunks[4];
                s.to_array(chunks);
                return std::vector<u64>(chunks, chunks + 4);
            }

            /** Applies a GF(2)-linear map given as the images of the basis vectors. */
            static std::vector<u8> apply_linear(const std::vector<u8>& basis_images, u32 bits, const std::vector<u8>& values)
            {
                std::vector<u8> res;
                for (const u8 v : values)
                {
                    u8 mapped = 0;
                    for (u32 b = 0; b < bits; b++)
                    {
                        if ((v >> b) & 1)
                        {
                            mapped ^= basis_images.at(b);
                        }
                    }
                    res.push_back(mapped);
                }
                return res;
            }

            /** The 4-bit S-box of PRESENT. */
            static std::vector<u8> present_sbox()
            {
                return {0xC, 0x5, 0x6, 0xB, 0x9, 0x0, 0xA, 0xD, 0x3, 0xE, 0xF, 0x8, 0x4, 0x7, 0x1, 0x2};
            }

            /** The 4-bit S-box of SKINNY-64. */
            static std::vector<u8> skinny64_sbox()
            {
                return {0xC, 0x6, 0x9, 0x0, 0x1, 0xA, 0x2, 0xB, 0x3, 0x8, 0x5, 0xD, 0x4, 0xE, 0x7, 0xF};
            }
        };

        /**
         * Every set operation has to produce the same bits on the selected backend as on the portable
         * implementation. Union, intersection, set minus and complement are the operations the search
         * spends its time in, and on ARM they are the ones that read the vectors back out lane by lane.
         *
         * Functions: smallset_union, smallset_intersect, smallset_setminus, smallset_invert
         */
        TEST_F(HawkeyeSBoxTest, backend_boolean_operations_match_portable)
        {
            TEST_START
            {
                const auto samples = sample_element_sets();
                for (u32 i = 0; i < samples.size(); i++)
                {
                    for (u32 j = 0; j < samples.size(); j++)
                    {
                        const smallset_t a          = make_backend_set(samples[i]);
                        const smallset_t b          = make_backend_set(samples[j]);
                        const smallset_portable_t p = make_portable_set(samples[i]);
                        const smallset_portable_t q = make_portable_set(samples[j]);

                        ASSERT_EQ(chunks_of(a), chunks_of_portable(p)) << "backend " << detail::SMALLSET_BACKEND << ", sample " << i;

                        EXPECT_EQ(chunks_of(detail::smallset_union(a, b)), chunks_of_portable(p | q)) << "union of samples " << i << " and " << j;
                        EXPECT_EQ(chunks_of(detail::smallset_intersect(a, b)), chunks_of_portable(p & q)) << "intersection of samples " << i << " and " << j;

                        for (const u32 len : {8u, 16u, 32u, 64u, 128u, 256u})
                        {
                            const smallset_portable_t full((int)len);
                            EXPECT_EQ(chunks_of(detail::smallset_invert(a, len)), chunks_of_portable(p ^ full)) << "complement of sample " << i << " within " << len;
                            EXPECT_EQ(chunks_of(detail::smallset_setminus(a, b, len)), chunks_of_portable(p & (q ^ full))) << "difference of samples " << i << " and " << j << " within " << len;
                        }
                    }
                }
            }
            TEST_END
        }

        /**
         * `smallset_shift` XORs a constant onto every element of the set, which the vector backends do as a
         * cascade of lane reversals and nibble swaps rather than as arithmetic. The cascade is where the
         * NEON path reinterprets 64-bit lanes as 32-, 16- and 8-bit ones, so every one of the 256 shifts is
         * compared against the portable implementation, and separately against the definition of the
         * operation, which is what makes this more than a comparison of two transcriptions of the same idea.
         *
         * Functions: smallset_shift, smallset_shift_union
         */
        TEST_F(HawkeyeSBoxTest, backend_shift_matches_portable_and_definition)
        {
            TEST_START
            {
                for (const auto& elements : sample_element_sets())
                {
                    const smallset_t a          = make_backend_set(elements);
                    const smallset_portable_t p = make_portable_set(elements);

                    for (u32 shift = 0; shift < 256; shift++)
                    {
                        const smallset_t shifted = detail::smallset_shift(a, (u8)shift);
                        ASSERT_EQ(chunks_of(shifted), chunks_of_portable(p.shuffle((u8)shift))) << "backend " << detail::SMALLSET_BACKEND << " disagrees for shift " << shift;

                        // the definition: the shifted set holds exactly the elements of the original XOR shift
                        std::vector<u8> expected;
                        for (const u8 e : elements)
                        {
                            expected.push_back(e ^ (u8)shift);
                        }
                        std::sort(expected.begin(), expected.end());
                        ASSERT_EQ(detail::smallset_get_elements(shifted), expected) << "shift " << shift << " is not an XOR by " << shift;

                        std::vector<u8> expected_union(elements);
                        expected_union.insert(expected_union.end(), expected.begin(), expected.end());
                        std::sort(expected_union.begin(), expected_union.end());
                        expected_union.erase(std::unique(expected_union.begin(), expected_union.end()), expected_union.end());
                        ASSERT_EQ(detail::smallset_get_elements(detail::smallset_shift_union(a, (u8)shift)), expected_union) << "shift union " << shift;
                    }
                }
            }
            TEST_END
        }

        /**
         * The full sets are built by writing a lane of a zeroed vector, one intrinsic per width, and the
         * search starts from them, so a wrong one silently searches the wrong domain. Emptiness, size,
         * membership and the least element are checked on the same sets because they are the queries the
         * search makes on every step.
         *
         * Functions: smallset_init_full, smallset_init_empty, smallset_is_empty, smallset_size, smallset_least_element, smallset_elm_is_in_set, smallset_get_elements
         */
        TEST_F(HawkeyeSBoxTest, backend_queries_match_portable)
        {
            TEST_START
            {
                for (const u32 len : {8u, 16u, 32u, 64u, 128u, 256u})
                {
                    const smallset_t full = detail::smallset_init_full(len);
                    EXPECT_EQ(chunks_of(full), chunks_of_portable(smallset_portable_t((int)len))) << "full set of " << len << " elements";
                    EXPECT_EQ(detail::smallset_size(full), len);
                    EXPECT_EQ(detail::smallset_least_element(full), 0);
                    EXPECT_FALSE(detail::smallset_is_empty(full));
                }

                const smallset_t empty = detail::smallset_init_empty();
                EXPECT_EQ(chunks_of(empty), chunks_of_portable(smallset_portable_t()));
                EXPECT_TRUE(detail::smallset_is_empty(empty));
                EXPECT_EQ(detail::smallset_size(empty), 0);
                EXPECT_TRUE(detail::smallset_get_elements(empty).empty());

                for (const auto& elements : sample_element_sets())
                {
                    const smallset_t a          = make_backend_set(elements);
                    const smallset_portable_t p = make_portable_set(elements);

                    EXPECT_EQ(detail::smallset_is_empty(a), p.empty()) << "emptiness of a set of " << elements.size() << " elements";
                    EXPECT_EQ(detail::smallset_size(a), (u16)p.size());
                    EXPECT_EQ(detail::smallset_get_elements(a), elements);
                    if (!elements.empty())
                    {
                        EXPECT_EQ(detail::smallset_least_element(a), elements.front());
                    }

                    for (u32 e = 0; e < 256; e++)
                    {
                        ASSERT_EQ(detail::smallset_elm_is_in_set((u8)e, a), p.is_set((u8)e)) << "membership of " << e;
                    }
                }
            }
            TEST_END
        }

        /**
         * The linear representative is the canonical form of an S-box under linear equivalence, so composing
         * the S-box with invertible linear maps on either side must not change it. This is the property the
         * database is indexed by, and it exercises the set operations through the actual search rather than
         * through the unit tests above.
         *
         * Functions: compute_linear_representative
         */
        TEST_F(HawkeyeSBoxTest, linear_representative_is_invariant_under_linear_equivalence)
        {
            TEST_START
            {
                const std::vector<u8> sbox = present_sbox();
                const std::vector<u8> rep  = SBoxDatabase::compute_linear_representative(sbox);

                ASSERT_EQ(rep.size(), sbox.size());
                std::vector<u8> sorted_rep(rep);
                std::sort(sorted_rep.begin(), sorted_rep.end());
                for (u32 i = 0; i < sorted_rep.size(); i++)
                {
                    // the representative is a permutation of the same domain
                    EXPECT_EQ(sorted_rep[i], (u8)i);
                }

                // a rotation of the four input bits and a different one of the output bits, both invertible
                const std::vector<u8> input_map  = {0x2, 0x4, 0x8, 0x1};
                const std::vector<u8> output_map = {0x8, 0x1, 0x2, 0x4};

                std::vector<u8> composed(sbox.size());
                const std::vector<u8> permuted_inputs = apply_linear(input_map, 4, {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15});
                for (u32 x = 0; x < sbox.size(); x++)
                {
                    composed[x] = sbox.at(permuted_inputs.at(x));
                }
                composed = apply_linear(output_map, 4, composed);

                EXPECT_EQ(SBoxDatabase::compute_linear_representative(composed), rep);

                // a different S-box has a different canonical form, or the invariance above would be vacuous
                EXPECT_NE(SBoxDatabase::compute_linear_representative(skinny64_sbox()), rep);
            }
            TEST_END
        }

        /**
         * A lookup has to recognize an S-box that was XORed with a constant on either side, which is what the
         * database stores the per-constant variants for, and it has to say so rather than guess when the
         * table is not in the database at all.
         *
         * Functions: add, lookup
         */
        TEST_F(HawkeyeSBoxTest, lookup_finds_stored_sbox_and_its_variants)
        {
            TEST_START
            {
                SBoxDatabase db;
                ASSERT_TRUE(db.add("PRESENT", present_sbox()).is_ok());
                ASSERT_TRUE(db.add("SKINNY64", skinny64_sbox()).is_ok());

                auto res = db.lookup(present_sbox());
                ASSERT_TRUE(res.is_ok()) << res.get_error().get();
                EXPECT_EQ(res.get(), "PRESENT");

                res = db.lookup(skinny64_sbox());
                ASSERT_TRUE(res.is_ok()) << res.get_error().get();
                EXPECT_EQ(res.get(), "SKINNY64");

                // the round constant a round function XORs onto the S-box outputs, which is what the database
                // stores one entry per constant for
                for (u8 constant = 1; constant < 16; constant++)
                {
                    std::vector<u8> output_xored(16);
                    for (u8 x = 0; x < 16; x++)
                    {
                        output_xored[x] = present_sbox().at(x) ^ constant;
                    }

                    auto out_res = db.lookup(output_xored);
                    ASSERT_TRUE(out_res.is_ok()) << "output constant " << (u32)constant << ": " << out_res.get_error().get();
                    EXPECT_EQ(out_res.get(), "PRESENT");
                }

                // an S-box that is not in the database is not silently matched against the nearest entry
                SBoxDatabase present_only;
                ASSERT_TRUE(present_only.add("PRESENT", present_sbox()).is_ok());
                EXPECT_TRUE(present_only.lookup(skinny64_sbox()).is_error());

                // neither is a table of a width the database holds nothing of
                std::vector<u8> wide(256);
                for (u32 x = 0; x < 256; x++)
                {
                    wide[x] = (u8)(x ^ 0xA5);
                }
                EXPECT_TRUE(db.lookup(wide).is_error());
            }
            TEST_END
        }

        /**
         * The database is shipped as a JSON file, so what `store` writes has to be what `load` reads back,
         * down to the lookups the reloaded database answers.
         *
         * Functions: store, load, from_file
         */
        TEST_F(HawkeyeSBoxTest, database_survives_a_file_round_trip)
        {
            TEST_START
            {
                SBoxDatabase db;
                ASSERT_TRUE(db.add("PRESENT", present_sbox()).is_ok());

                const std::filesystem::path path = std::filesystem::temp_directory_path() / "hal_hawkeye_sbox_database_test.json";
                std::filesystem::remove(path);
                ASSERT_TRUE(db.store(path).is_ok());

                auto loaded = SBoxDatabase::from_file(path);
                ASSERT_TRUE(loaded.is_ok()) << loaded.get_error().get();

                auto res = loaded.get().lookup(present_sbox());
                ASSERT_TRUE(res.is_ok()) << res.get_error().get();
                EXPECT_EQ(res.get(), "PRESENT");

                std::filesystem::remove(path);
            }
            TEST_END
        }
    }    // namespace hawkeye
}    // namespace hal
