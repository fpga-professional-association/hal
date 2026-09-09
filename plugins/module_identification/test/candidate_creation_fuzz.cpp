// Randomized structural exercise of functional candidate creation.
//
// module_identification has been seen to SIGSEGV inside FunctionalCandidate::create_candidates for
// constant_multiplication_offset, in create_input_extension_variants -> apply_extension, with no
// reproducer to work from. The crash class is unguarded end-of-vector access on degenerate candidate
// shapes: an operand with no bits has no sign bit to repeat, a netlist with no GND net has no zero to
// pad with, and an output width of zero underflows the "sign extend up to the second highest bit"
// arithmetic into a request for 2^32 - 1 bits.
//
// The operations that build operands are pure structure -- they permute, pad and copy vectors of nets
// and never solve anything -- so they can be driven directly with candidates assembled by hand. That
// is what this file does: it walks every checkable candidate type over randomly shaped (and
// deliberately degenerate) operands, on a netlist that has a GND net and on one that does not, and
// requires every one of those calls to come back with an answer or a clean error rather than to die.
//
// The random cases are seeded from a constant, so a failure here is reproducible; the named cases
// below them pin the specific shapes that used to be undefined behaviour.

#include "hal_core/defines.h"
#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/gate_library/gate_library.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/utilities/enums.h"
#include "gate_library_test_utils.h"
#include "module_identification/candidates/base_candidate.h"
#include "module_identification/candidates/candidate_context.h"
#include "module_identification/candidates/functional_candidate.h"
#include "module_identification/candidates/structural_candidate.h"
#include "module_identification/types/candidate_types.h"
#include "netlist_test_utils.h"
#include "test_def.h"

#include <map>
#include <memory>
#include <random>
#include <string>
#include <utility>
#include <vector>

namespace hal
{
    using module_identification::BaseCandidate;
    using module_identification::CandidateType;
    using module_identification::FunctionalCandidate;
    using module_identification::StructuralCandidate;

    namespace
    {
        /** The widest operand any of the cases below asks for, with room to spare for extensions. */
        const size_t MAX_SANE_OPERAND_SIZE = 64;

        /**
         * Names a candidate type for a failure message.
         *
         * `enum_to_string` is not used for this: the string table it reads lives in the plugin library,
         * and referring to it from here would have this test carry its own empty copy of that table.
         */
        std::string candidate_type_name(const CandidateType type)
        {
            static const std::map<CandidateType, std::string> names = {
                {CandidateType::addition, "addition"},
                {CandidateType::addition_offset, "addition_offset"},
                {CandidateType::subtraction, "subtraction"},
                {CandidateType::counter, "counter"},
                {CandidateType::negation, "negation"},
                {CandidateType::absolute, "absolute"},
                {CandidateType::constant_multiplication, "constant_multiplication"},
                {CandidateType::constant_multiplication_offset, "constant_multiplication_offset"},
                {CandidateType::equal, "equal"},
                {CandidateType::less_than, "less_than"},
                {CandidateType::less_equal, "less_equal"},
                {CandidateType::signed_less_than, "signed_less_than"},
                {CandidateType::signed_less_equal, "signed_less_equal"},
                {CandidateType::value_check, "value_check"},
                {CandidateType::none, "none"},
                {CandidateType::mixed, "mixed"},
            };

            const auto it = names.find(type);
            return (it == names.end()) ? std::to_string(static_cast<int>(type)) : it->second;
        }

        /**
         * A netlist to draw operand bits from, plus the candidate scaffolding around it.
         *
         * A functional candidate cannot exist on its own: it points at a structural candidate, which owns
         * the context that carries the netlist. All of that is set up once per netlist here so that the
         * cases below only have to fill in the operands they care about.
         */
        struct CandidateScaffold
        {
            std::unique_ptr<Netlist> netlist;
            std::vector<Net*> nets;
            std::unique_ptr<BaseCandidate> base_candidate;
            std::unique_ptr<StructuralCandidate> structural_candidate;

            /** Builds a candidate of the given type with nothing filled in yet. */
            FunctionalCandidate make_candidate(const CandidateType type) const
            {
                return FunctionalCandidate(structural_candidate.get(), 2, type);
            }

            module_identification::CandidateContext& context() const
            {
                return structural_candidate->ctx;
            }
        };

        /**
         * Builds a netlist with a handful of nets to use as operand bits.
         *
         * `with_gnd_net` decides whether the netlist has a constant-zero net, which is what operand
         * padding needs; a design without a tied-low gate has none, and that is one of the shapes this
         * file is about.
         */
        std::unique_ptr<CandidateScaffold> build_scaffold(const bool with_gnd_net)
        {
            auto scaffold     = std::make_unique<CandidateScaffold>();
            scaffold->netlist = test_utils::create_empty_netlist();

            const GateLibrary* gl = scaffold->netlist->get_gate_library();

            Gate* gate = scaffold->netlist->create_gate(gl->get_gate_type_by_name("AND2"), "candidate_gate");

            for (u32 i = 0; i < 12; i++)
            {
                scaffold->nets.push_back(scaffold->netlist->create_net("net_" + std::to_string(i)));
            }

            if (with_gnd_net)
            {
                Gate* gnd_gate = scaffold->netlist->create_gate(gl->get_gate_type_by_name("GND"), "gnd_gate");
                scaffold->netlist->mark_gnd_gate(gnd_gate);

                Net* gnd_net = scaffold->netlist->create_net("gnd_net");
                gnd_net->add_source(gnd_gate, "O");
            }

            scaffold->base_candidate       = std::make_unique<BaseCandidate>(std::vector<Gate*>({gate}));
            scaffold->structural_candidate = std::make_unique<StructuralCandidate>(scaffold->base_candidate.get(), std::vector<Gate*>({gate}));

            return scaffold;
        }

        /** Reports whether every operand of every candidate is a plausible vector of real nets. */
        void expect_sane_operands(const std::vector<FunctionalCandidate>& candidates, const std::string& what)
        {
            for (const auto& candidate : candidates)
            {
                for (u32 op_idx = 0; op_idx < candidate.m_operands.size(); op_idx++)
                {
                    const auto& operand = candidate.m_operands.at(op_idx);

                    // an operand of billions of bits is the signature of an unsigned underflow in the
                    // requested size, which used to try to allocate that many net pointers
                    EXPECT_LE(operand.size(), MAX_SANE_OPERAND_SIZE) << what << ": operand " << op_idx << " has an implausible size";

                    for (const auto& net : operand)
                    {
                        EXPECT_NE(net, nullptr) << what << ": operand " << op_idx << " contains a null net";
                    }
                }
            }
        }

        /** Runs every structural candidate operation that builds or extends operands. */
        void run_operand_operations(const CandidateScaffold& scaffold, const FunctionalCandidate& candidate, const std::string& what)
        {
            auto& ctx = scaffold.context();

            const auto sign_bit_res = FunctionalCandidate::create_sign_bit_variants(ctx, candidate);
            if (sign_bit_res.is_ok())
            {
                expect_sane_operands(sign_bit_res.get(), what + " / create_sign_bit_variants");
            }

            const auto extension_res = FunctionalCandidate::create_input_extension_variants(ctx, candidate);
            if (extension_res.is_ok())
            {
                expect_sane_operands(extension_res.get(), what + " / create_input_extension_variants");
            }

            const auto shifted_res = FunctionalCandidate::add_all_shifted_operand(ctx, candidate);
            if (shifted_res.is_ok())
            {
                expect_sane_operands(shifted_res.get(), what + " / add_all_shifted_operand");
            }

            const auto operands_res = FunctionalCandidate::build_input_operands(ctx, candidate);
            if (operands_res.is_ok())
            {
                expect_sane_operands(operands_res.get(), what + " / build_input_operands");
            }

            const auto single_input_res = FunctionalCandidate::add_single_input_signals(ctx, candidate);
            if (single_input_res.is_ok())
            {
                expect_sane_operands(single_input_res.get(), what + " / add_single_input_signals");
            }

            const auto output_variant_res = FunctionalCandidate::create_output_net_variant(ctx, candidate);
            if (output_variant_res.is_ok())
            {
                expect_sane_operands(output_variant_res.get(), what + " / create_output_net_variant");
            }
        }
    }    // namespace

    class ModuleIdentificationCandidateFuzz : public ::testing::Test
    {
    protected:
        std::unique_ptr<CandidateScaffold> m_with_gnd;
        std::unique_ptr<CandidateScaffold> m_without_gnd;

        virtual void SetUp()
        {
            NO_COUT_BLOCK;
            m_with_gnd    = build_scaffold(true);
            m_without_gnd = build_scaffold(false);
        }

        virtual void TearDown()
        {
            m_with_gnd.reset();
            m_without_gnd.reset();
        }
    };

    /**
     * Walks every checkable candidate type over randomly shaped operands, including empty operands,
     * empty output nets and a netlist without a GND net.
     *
     * Functions: create_sign_bit_variants, create_input_extension_variants, add_all_shifted_operand,
     *            build_input_operands
     */
    TEST_F(ModuleIdentificationCandidateFuzz, test_random_candidate_shapes)
    {
        TEST_START
        NO_COUT_BLOCK;

        // a fixed seed keeps a failure reproducible -- the point is coverage of odd shapes, not novelty
        std::mt19937 rng(0x5eed1234);

        std::uniform_int_distribution<u32> operand_count_dist(0, 4);
        std::uniform_int_distribution<u32> operand_size_dist(0, 5);
        std::uniform_int_distribution<u32> output_count_dist(0, 5);
        std::uniform_int_distribution<u32> bin_count_dist(0, 3);
        std::uniform_int_distribution<u32> max_operand_dist(0, 3);
        std::uniform_int_distribution<u32> net_dist(0, 11);

        for (u32 iteration = 0; iteration < 60; iteration++)
        {
            const CandidateScaffold& scaffold = ((iteration % 2) == 0) ? *m_with_gnd : *m_without_gnd;

            for (const auto& type : module_identification::all_checkable_candidate_types)
            {
                auto candidate = scaffold.make_candidate(type);

                const u32 operand_count = operand_count_dist(rng);
                for (u32 op_idx = 0; op_idx < operand_count; op_idx++)
                {
                    std::vector<Net*> operand;
                    const u32 operand_size = operand_size_dist(rng);
                    for (u32 bit = 0; bit < operand_size; bit++)
                    {
                        operand.push_back(scaffold.nets.at(net_dist(rng)));
                    }
                    candidate.m_operands.push_back(operand);
                }

                const u32 output_count = output_count_dist(rng);
                candidate.m_output_nets.clear();
                for (u32 out_idx = 0; out_idx < output_count; out_idx++)
                {
                    candidate.m_output_nets.push_back(scaffold.nets.at(net_dist(rng)));
                }

                candidate.m_input_nets.clear();
                for (const auto& operand : candidate.m_operands)
                {
                    for (const auto& net : operand)
                    {
                        candidate.m_input_nets.push_back(net);
                    }
                }

                // the input/output statistics drive operand construction, so they get shapes of their own
                const u32 bin_count = bin_count_dist(rng);
                for (u32 bin = 0; bin < bin_count; bin++)
                {
                    std::vector<Net*> bin_nets;
                    const u32 bin_size = operand_size_dist(rng);
                    for (u32 idx = 0; idx < bin_size; idx++)
                    {
                        bin_nets.push_back(scaffold.nets.at(net_dist(rng)));
                    }

                    candidate.m_influence_count_to_input_nets[bin + 1] = bin_nets;
                    candidate.m_input_count_to_output_nets[bin + 1]    = bin_nets;
                }

                // a buffered input signal is prepended to one operand and the others are padded, which is
                // the second place operand construction reaches for the netlist's constant zero
                const u32 pair_count = bin_count_dist(rng);
                for (u32 pair = 0; pair < pair_count; pair++)
                {
                    Net* input_net  = scaffold.nets.at(net_dist(rng));
                    Net* output_net = scaffold.nets.at(net_dist(rng));

                    candidate.m_single_input_to_output[input_net] = output_net;
                    candidate.m_permuted_single_pairs.push_back(std::make_pair(input_net, output_net));
                }

                candidate.m_max_operands = max_operand_dist(rng);

                run_operand_operations(scaffold, candidate, "iteration " + std::to_string(iteration) + ", type " + candidate_type_name(type));
            }
        }
        TEST_END
    }

    /**
     * An operand with no bits has no sign bit, which used to be read off the end of the empty vector.
     *
     * Functions: create_input_extension_variants, create_sign_bit_variants
     */
    TEST_F(ModuleIdentificationCandidateFuzz, test_empty_operand_is_reported)
    {
        TEST_START
        NO_COUT_BLOCK;

        for (const auto& type : module_identification::all_checkable_candidate_types)
        {
            auto candidate = m_with_gnd->make_candidate(type);

            candidate.m_operands    = {{m_with_gnd->nets.at(0), m_with_gnd->nets.at(1)}, {}};
            candidate.m_output_nets = {m_with_gnd->nets.at(2), m_with_gnd->nets.at(3), m_with_gnd->nets.at(4)};

            const auto extension_res = FunctionalCandidate::create_input_extension_variants(m_with_gnd->context(), candidate);
            EXPECT_TRUE(extension_res.is_error()) << "an empty operand has to be refused for type " << candidate_type_name(type);

            // the sign bit positions are collected from the same operands, and an empty one simply has none
            const auto sign_bit_res = FunctionalCandidate::create_sign_bit_variants(m_with_gnd->context(), candidate);
            ASSERT_TRUE(sign_bit_res.is_ok()) << "an empty operand has no sign bit but must not fail the variant creation";
            expect_sane_operands(sign_bit_res.get(), "empty operand / create_sign_bit_variants");
        }
        TEST_END
    }

    /**
     * Padding an operand with zeros needs a GND net, and a netlist is free to have none.
     *
     * Functions: create_input_extension_variants, add_all_shifted_operand
     */
    TEST_F(ModuleIdentificationCandidateFuzz, test_missing_gnd_net_is_reported)
    {
        TEST_START
        NO_COUT_BLOCK;

        auto candidate = m_without_gnd->make_candidate(CandidateType::constant_multiplication_offset);

        // an operand narrower than the output forces the extension to invent the missing high bits
        candidate.m_operands    = {{m_without_gnd->nets.at(0), m_without_gnd->nets.at(1)}};
        candidate.m_output_nets = {m_without_gnd->nets.at(2), m_without_gnd->nets.at(3), m_without_gnd->nets.at(4), m_without_gnd->nets.at(5)};

        const auto extension_res = FunctionalCandidate::create_input_extension_variants(m_without_gnd->context(), candidate);
        ASSERT_TRUE(extension_res.is_ok()) << "the variants that do not need a GND net still have to be built";

        const auto variants = extension_res.get();
        expect_sane_operands(variants, "missing gnd net / create_input_extension_variants");

        // zero extension and "sign extension up to the second highest bit" both need the constant zero;
        // only the plain sign extension can be built here
        EXPECT_EQ(variants.size(), size_t(1));
        for (const auto& variant : variants)
        {
            ASSERT_EQ(variant.m_operands.size(), size_t(1));
            EXPECT_EQ(variant.m_operands.front().size(), candidate.m_output_nets.size());
        }

        // shifting left shifts constant zeros in, so it cannot be done at all on this netlist
        const auto shifted_res = FunctionalCandidate::add_all_shifted_operand(m_without_gnd->context(), candidate);
        EXPECT_TRUE(shifted_res.is_error()) << "shifting in zeros without a GND net has to be refused";

        // ... while the same candidate on a netlist that has one is fine
        auto with_gnd_candidate           = m_with_gnd->make_candidate(CandidateType::constant_multiplication_offset);
        with_gnd_candidate.m_operands     = {{m_with_gnd->nets.at(0), m_with_gnd->nets.at(1)}};
        with_gnd_candidate.m_output_nets  = {m_with_gnd->nets.at(2), m_with_gnd->nets.at(3), m_with_gnd->nets.at(4), m_with_gnd->nets.at(5)};

        const auto with_gnd_shifted_res = FunctionalCandidate::add_all_shifted_operand(m_with_gnd->context(), with_gnd_candidate);
        ASSERT_TRUE(with_gnd_shifted_res.is_ok()) << "shifting in zeros has to work when the netlist has a GND net";
        expect_sane_operands(with_gnd_shifted_res.get(), "with gnd net / add_all_shifted_operand");
        TEST_END
    }

    /**
     * A candidate with no output nets asks for operands of zero bits, which used to underflow the
     * "sign extend up to the second highest bit" size into a request for 2^32 - 1 net pointers.
     *
     * Functions: create_input_extension_variants
     */
    TEST_F(ModuleIdentificationCandidateFuzz, test_zero_output_width_does_not_underflow)
    {
        TEST_START
        NO_COUT_BLOCK;

        for (const auto& type : module_identification::all_checkable_candidate_types)
        {
            auto candidate = m_with_gnd->make_candidate(type);

            candidate.m_operands    = {{m_with_gnd->nets.at(0), m_with_gnd->nets.at(1)}, {m_with_gnd->nets.at(2)}};
            candidate.m_output_nets = {};

            const auto extension_res = FunctionalCandidate::create_input_extension_variants(m_with_gnd->context(), candidate);
            if (extension_res.is_ok())
            {
                expect_sane_operands(extension_res.get(), "zero output width / create_input_extension_variants");
            }

            // the single operand case picks the extension schemes by comparing against the output width
            // minus one, which is the second place a candidate without output nets wraps around
            auto single_operand_candidate           = m_with_gnd->make_candidate(type);
            single_operand_candidate.m_operands     = {{m_with_gnd->nets.at(0), m_with_gnd->nets.at(1)}};
            single_operand_candidate.m_output_nets  = {};

            const auto single_operand_res = FunctionalCandidate::create_input_extension_variants(m_with_gnd->context(), single_operand_candidate);
            if (single_operand_res.is_ok())
            {
                expect_sane_operands(single_operand_res.get(), "zero output width, one operand / create_input_extension_variants");

                // nothing can be extended into an output of no bits, so the candidate comes back as it
                // was -- a constant multiplication picks its extension schemes without looking at the
                // output width and truncates the operand to the (empty) output instead
                if ((type != CandidateType::constant_multiplication) && (type != CandidateType::constant_multiplication_offset))
                {
                    for (const auto& variant : single_operand_res.get())
                    {
                        ASSERT_EQ(variant.m_operands.size(), size_t(1));
                        EXPECT_EQ(variant.m_operands.front().size(), single_operand_candidate.m_operands.front().size())
                            << "an output of no bits must not change the operand of a " << candidate_type_name(type) << " candidate";
                    }
                }
            }
        }
        TEST_END
    }

    /**
     * A candidate without operands has nothing to shift.
     *
     * Functions: add_n_shifted_operands
     */
    TEST_F(ModuleIdentificationCandidateFuzz, test_shifting_without_operands_is_reported)
    {
        TEST_START
        NO_COUT_BLOCK;

        auto candidate = m_with_gnd->make_candidate(CandidateType::constant_multiplication);
        candidate.m_operands.clear();

        const auto shifted_res = FunctionalCandidate::add_n_shifted_operands(candidate, {1});
        EXPECT_TRUE(shifted_res.is_error()) << "shifting a candidate without operands has to be refused";
        TEST_END
    }
}    // namespace hal
