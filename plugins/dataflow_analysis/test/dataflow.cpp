#include "dataflow_analysis/api/configuration.h"
#include "dataflow_analysis/api/dataflow.h"
#include "dataflow_analysis/api/result.h"
#include "gate_library_test_utils.h"
#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "netlist_test_utils.h"

#include <algorithm>
#include <set>
#include <string>
#include <vector>

namespace hal
{
    /**
     * Tests of the dataflow analysis, i.e., of the reconstruction of registers from a gate-level netlist.
     *
     * The netlists are built in-test so that the register boundaries are known up front: flip-flops that
     * belong to different registers are given different enable signals, which is a hard constraint for the
     * analysis -- two groups whose control signals differ can never be merged.
     */
    class DataflowTest : public ::testing::Test
    {
    protected:
        virtual void SetUp()
        {
            NO_COUT_BLOCK;
            test_utils::init_log_channels();
        }

        virtual void TearDown()
        {
        }

        /**
         * The sizes of the identified groups, ascending. Group IDs are assigned by the analysis and carry no
         * meaning of their own, so the sizes are what a test can state.
         */
        std::vector<u32> group_sizes(const dataflow::Result& result)
        {
            std::vector<u32> sizes;
            for (const auto& [_, gates] : result.get_groups())
            {
                sizes.push_back(gates.size());
            }
            std::sort(sizes.begin(), sizes.end());
            return sizes;
        }

        std::set<std::string> gate_names(const std::unordered_set<Gate*>& gates)
        {
            std::set<std::string> names;
            for (const auto* g : gates)
            {
                names.insert(g->get_name());
            }
            return names;
        }

        /**
         * A bank of `width` enabled flip-flops sharing one clock and one enable net, i.e., one register.
         *
         * Data inputs are left to the caller: passing `nullptr` for `data` drives every flip-flop from a
         * global input of its own, which leaves the bank without predecessors.
         */
        std::vector<Gate*> build_register(Netlist* nl, const std::string& name, u32 width, Net* clk, Net* enable, const std::vector<Net*>* data = nullptr)
        {
            const GateLibrary* gl = nl->get_gate_library();

            std::vector<Gate*> ffs;
            for (u32 i = 0; i < width; i++)
            {
                const std::string gate_name = name + "_" + std::to_string(i);
                Gate* ff                    = nl->create_gate(gl->get_gate_type_by_name("DFFE"), gate_name);
                clk->add_destination(ff, "CLK");
                enable->add_destination(ff, "EN");

                if (data == nullptr)
                {
                    test_utils::connect_global_in(nl, ff, "D", "in_" + gate_name);
                }
                else
                {
                    data->at(i)->add_destination(ff, "D");
                }

                ffs.push_back(ff);
            }

            return ffs;
        }

        std::vector<Net*> outputs_of(Netlist* nl, const std::vector<Gate*>& ffs)
        {
            std::vector<Net*> nets;
            for (auto* ff : ffs)
            {
                nets.push_back(test_utils::connect_global_out(nl, ff, "Q", "out_" + ff->get_name()));
            }
            return nets;
        }

        Net* global_input(Netlist* nl, const std::string& name)
        {
            Net* n = nl->create_net(name);
            n->mark_global_input_net();
            return n;
        }

        /**
         * Two 4-bit registers on the same clock but with enables of their own, neither of them connected to
         * the other. Only the enable nets tell them apart.
         */
        std::unique_ptr<Netlist> build_two_registers()
        {
            auto nl = test_utils::create_empty_netlist();

            Net* clk  = global_input(nl.get(), "clk");
            Net* en_a = global_input(nl.get(), "en_a");
            Net* en_b = global_input(nl.get(), "en_b");

            outputs_of(nl.get(), build_register(nl.get(), "reg_a", 4, clk, en_a));
            outputs_of(nl.get(), build_register(nl.get(), "reg_b", 4, clk, en_b));

            return nl;
        }

        /**
         * Eight source flip-flops that share one clock and one enable, feeding two 4-bit destination
         * registers: the lower four bits drive `reg_x`, the upper four drive `reg_y`.
         *
         * Control signals alone would make the source flip-flops one register of eight bits, while their
         * successors split them into two registers of four. Both readings therefore reach the evaluation as
         * candidates, and which of them is picked is decided by the size knobs of the configuration.
         */
        std::unique_ptr<Netlist> build_split_register()
        {
            auto nl = test_utils::create_empty_netlist();

            Net* clk    = global_input(nl.get(), "clk");
            Net* en_src = global_input(nl.get(), "en_src");
            Net* en_x   = global_input(nl.get(), "en_x");
            Net* en_y   = global_input(nl.get(), "en_y");

            const auto src      = build_register(nl.get(), "reg_src", 8, clk, en_src);
            const auto src_nets = outputs_of(nl.get(), src);

            const std::vector<Net*> lower(src_nets.begin(), src_nets.begin() + 4);
            const std::vector<Net*> upper(src_nets.begin() + 4, src_nets.end());

            outputs_of(nl.get(), build_register(nl.get(), "reg_x", 4, clk, en_x, &lower));
            outputs_of(nl.get(), build_register(nl.get(), "reg_y", 4, clk, en_y, &upper));

            return nl;
        }
    };

    /**
     * Two registers that differ only in their enable signal are reported as two groups of their respective
     * width, and every flip-flop of the netlist ends up in exactly one of them.
     *
     * Functions: analyze, get_groups, get_gates, get_group_id_of_gate
     */
    TEST_F(DataflowTest, check_two_independent_registers)
    {
        NO_COUT_TEST_BLOCK;
        TEST_START
        {
            auto nl = build_two_registers();
            ASSERT_NE(nl, nullptr);

            dataflow::Configuration config(nl.get());
            config.with_flip_flops().with_min_group_size(4);

            auto res = dataflow::analyze(config);
            ASSERT_TRUE(res.is_ok());
            const auto result = res.get();

            EXPECT_EQ(group_sizes(result), std::vector<u32>({4, 4}));
            EXPECT_EQ(result.get_netlist(), nl.get());
            EXPECT_EQ(result.get_gates().size(), 8);

            // the two groups are the two registers, not some mixture of them
            std::set<std::set<std::string>> groups;
            for (const auto& [_, gates] : result.get_groups())
            {
                groups.insert(gate_names(gates));
            }
            const std::set<std::set<std::string>> expected = {
                {"reg_a_0", "reg_a_1", "reg_a_2", "reg_a_3"},
                {"reg_b_0", "reg_b_1", "reg_b_2", "reg_b_3"},
            };
            EXPECT_EQ(groups, expected);

            for (auto* g : nl->get_gates())
            {
                EXPECT_TRUE(result.get_group_id_of_gate(g).is_ok());
            }
        }
        TEST_END
    }

    /**
     * Eight flip-flops that share their control signals but drive two different destination registers can be
     * read either as one register of eight bits or as two of four. Both readings reach the evaluation, and
     * the expected sizes decide which of them is reported.
     *
     * Functions: analyze, with_expected_sizes, with_min_group_size
     */
    TEST_F(DataflowTest, check_group_size_configuration)
    {
        NO_COUT_TEST_BLOCK;
        TEST_START
        {
            // without a stated expectation the majority vote wins, and that is the eight-bit reading
            auto nl = build_split_register();
            ASSERT_NE(nl, nullptr);

            dataflow::Configuration config(nl.get());
            config.with_flip_flops();
            ASSERT_TRUE(config.expected_sizes.empty());
            ASSERT_EQ(config.min_group_size, 8);

            auto res = dataflow::analyze(config);
            ASSERT_TRUE(res.is_ok());

            EXPECT_EQ(group_sizes(res.get()), std::vector<u32>({4, 4, 8}));
        }
        {
            // an expected size of four outranks the vote and picks the split reading of the source bank
            auto nl = build_split_register();
            ASSERT_NE(nl, nullptr);

            dataflow::Configuration config(nl.get());
            config.with_flip_flops().with_expected_sizes({4});

            auto res = dataflow::analyze(config);
            ASSERT_TRUE(res.is_ok());

            EXPECT_EQ(group_sizes(res.get()), std::vector<u32>({4, 4, 4, 4}));
        }
        {
            // it also outranks the minimum group size, which is lowered to four here
            auto nl = build_split_register();
            ASSERT_NE(nl, nullptr);

            dataflow::Configuration config(nl.get());
            config.with_flip_flops().with_min_group_size(4).with_expected_sizes({8});

            auto res = dataflow::analyze(config);
            ASSERT_TRUE(res.is_ok());

            EXPECT_EQ(group_sizes(res.get()), std::vector<u32>({4, 4, 8}));
        }
        {
            // the minimum group size on its own only sinks the groups below it behind the ones above it in
            // the candidate ranking; where the candidates are ranked by vote alone, as they are here, it
            // does not change the outcome
            auto nl = build_split_register();
            ASSERT_NE(nl, nullptr);

            dataflow::Configuration config(nl.get());
            config.with_flip_flops().with_min_group_size(4);

            auto res = dataflow::analyze(config);
            ASSERT_TRUE(res.is_ok());

            EXPECT_EQ(group_sizes(res.get()), std::vector<u32>({4, 4, 8}));
        }
        TEST_END
    }

    /**
     * A netlist without flip-flops is analyzed without failing and yields no groups. A configuration without
     * target gate types or without control pin types is rejected instead, as there would be nothing to group
     * by.
     *
     * Functions: analyze
     */
    TEST_F(DataflowTest, check_empty_netlist)
    {
        NO_COUT_TEST_BLOCK;
        TEST_START
        {
            auto nl = test_utils::create_empty_netlist();
            ASSERT_NE(nl, nullptr);
            ASSERT_TRUE(nl->get_gates().empty());

            dataflow::Configuration config(nl.get());
            config.with_flip_flops();
            ASSERT_FALSE(config.gate_types.empty());

            auto res = dataflow::analyze(config);
            ASSERT_TRUE(res.is_ok());

            const auto result = res.get();
            EXPECT_TRUE(result.get_groups().empty());
            EXPECT_TRUE(result.get_gates().empty());
        }
        {
            // a netlist holding only combinational gates has no target gates either
            auto nl               = test_utils::create_empty_netlist();
            const GateLibrary* gl = nl->get_gate_library();
            Gate* inv             = nl->create_gate(gl->get_gate_type_by_name("INV"), "inv_0");
            test_utils::connect_global_in(nl.get(), inv, "I", "in");
            test_utils::connect_global_out(nl.get(), inv, "O", "out");

            dataflow::Configuration config(nl.get());
            config.with_flip_flops();

            auto res = dataflow::analyze(config);
            ASSERT_TRUE(res.is_ok());
            EXPECT_TRUE(res.get().get_groups().empty());
        }
        {
            dataflow::Configuration config(nullptr);
            EXPECT_TRUE(dataflow::analyze(config).is_error());
        }
        {
            auto nl = build_two_registers();
            ASSERT_NE(nl, nullptr);

            dataflow::Configuration config(nl.get());
            EXPECT_TRUE(config.gate_types.empty());
            EXPECT_TRUE(dataflow::analyze(config).is_error());
        }
        {
            auto nl = build_two_registers();
            ASSERT_NE(nl, nullptr);

            dataflow::Configuration config(nl.get());
            config.with_gate_types({GateTypeProperty::ff});
            EXPECT_TRUE(config.control_pin_types.empty());
            EXPECT_TRUE(dataflow::analyze(config).is_error());
        }
        TEST_END
    }
}    // namespace hal
