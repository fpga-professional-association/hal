// Guards the shipped sky130 gate library, plugins/gate_libraries/definitions/SKY130_FD_SC_HD.hgl.
//
// That library is generated rather than hand-written, so the thing worth testing is not its text
// but whether the cell shapes an extracted sky130 netlist depends on still arrive intact on the
// other side of the HGL parser: the flip-flop configurations, the tie cell's constants, the
// inverted-input naming, and the compound-gate polarities. The fixture it is checked against,
// tests/fixtures/sky130_cells/sky130_cells.v, is original content written from the library's own
// cell definitions; its documented expected structure lives next to it in README.md.

#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/gate_library/gate_library.h"
#include "hal_core/netlist/gate_library/gate_library_manager.h"
#include "hal_core/netlist/gate_library/gate_type.h"
#include "hal_core/netlist/gate_library/gate_type_component/ff_component.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/netlist/netlist_parser/netlist_parser_manager.h"
#include "hal_core/plugin_system/plugin_manager.h"
#include "hal_core/utilities/utils.h"
#include "netlist_test_utils.h"

#include "gtest/gtest.h"

#include <filesystem>
#include <functional>
#include <map>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

namespace hal
{
    namespace
    {
        const std::string library_file_name = "SKY130_FD_SC_HD.hgl";
        const std::string library_name      = "SKY130_FD_SC_HD";

        /**
         * Locate the shipped gate library in the standard gate library directories.
         *
         * @returns The path to the library file, or an empty path if it is not installed.
         */
        std::filesystem::path find_library_file()
        {
            for (const auto& dir : utils::get_gate_library_directories())
            {
                const std::filesystem::path candidate = dir / library_file_name;
                if (std::filesystem::exists(candidate))
                {
                    return candidate;
                }
            }
            return {};
        }

        /**
         * Exhaustively compare a gate type's Boolean function against an expected truth table.
         *
         * The expected value is computed from the input assignment by `expected`, which receives the
         * inputs in the order in which they are named in `inputs` (`inputs[0]` is the least significant
         * bit of the mask). This checks the library's own expression rather than the fixture, so the
         * assertion does not move when the fixture does.
         *
         * @param[in] gt - The gate type.
         * @param[in] output_pin - The name of the output pin whose function is checked.
         * @param[in] inputs - The input pin names, least significant first.
         * @param[in] expected - The expected truth table, indexed by the input assignment.
         */
        void expect_truth_table(const GateType* gt, const std::string& output_pin, const std::vector<std::string>& inputs, const std::function<bool(u32)>& expected)
        {
            ASSERT_NE(gt, nullptr);
            const BooleanFunction bf = gt->get_boolean_function(output_pin);
            ASSERT_FALSE(bf.is_empty()) << "gate type '" << gt->get_name() << "' has no function for pin '" << output_pin << "'";
            EXPECT_EQ(bf.get_variable_names(), std::set<std::string>(inputs.begin(), inputs.end()));

            for (u32 assignment = 0; assignment < (u32(1) << inputs.size()); assignment++)
            {
                std::unordered_map<std::string, BooleanFunction::Value> values;
                for (u32 i = 0; i < inputs.size(); i++)
                {
                    values[inputs.at(i)] = ((assignment >> i) & 1) ? BooleanFunction::Value::ONE : BooleanFunction::Value::ZERO;
                }

                const auto res = bf.evaluate(values);
                ASSERT_TRUE(res.is_ok()) << "could not evaluate '" << gt->get_name() << "." << output_pin << "' for assignment " << assignment;
                EXPECT_EQ(res.get(), expected(assignment) ? BooleanFunction::Value::ONE : BooleanFunction::Value::ZERO)
                    << "'" << gt->get_name() << "." << output_pin << "' disagrees with the expected truth table for assignment " << assignment;
            }
        }

        /**
         * Get the flip-flop component of a gate type.
         *
         * @param[in] gt - The gate type.
         * @returns The flip-flop component, or a `nullptr` if the gate type is not a flip-flop.
         */
        FFComponent* get_ff_component(const GateType* gt)
        {
            if (gt == nullptr)
            {
                return nullptr;
            }
            return gt->get_component_as<FFComponent>([](const GateTypeComponent* c) { return FFComponent::is_class_of(c); });
        }
    }    // namespace

    class GateLibrarySky130Test : public ::testing::Test
    {
    protected:
        GateLibrary* m_gl = nullptr;

        virtual void SetUp()
        {
            NO_COUT_BLOCK;
            test_utils::init_log_channels();
            plugin_manager::load_all_plugins();

            const std::filesystem::path lib_path = find_library_file();
            if (!lib_path.empty())
            {
                m_gl = gate_library_manager::load(lib_path);
            }
        }

        virtual void TearDown()
        {
            NO_COUT_BLOCK;
            plugin_manager::unload_all_plugins();
        }

        /**
         * Get a gate type of the sky130 library by name.
         *
         * @param[in] name - The name of the gate type.
         * @returns The gate type, or a `nullptr` if the library does not define it.
         */
        const GateType* get_type(const std::string& name) const
        {
            if (m_gl == nullptr)
            {
                return nullptr;
            }
            const auto& types = m_gl->get_gate_types();
            if (const auto it = types.find(name); it != types.end())
            {
                return it->second;
            }
            return nullptr;
        }
    };

    /**
     * Testing that the shipped sky130 gate library is installed and parses.
     *
     * Functions: load
     */
    TEST_F(GateLibrarySky130Test, check_library_loads)
    {
        TEST_START
        {
            const std::filesystem::path lib_path = find_library_file();
            ASSERT_FALSE(lib_path.empty()) << library_file_name << " is not installed in any gate library directory";

            ASSERT_NE(m_gl, nullptr);
            EXPECT_EQ(m_gl->get_name(), library_name);
            // the library covers the whole sky130_fd_sc_hd cell set, so this is a "did it truncate" check
            EXPECT_GT(m_gl->get_gate_types().size(), 400);
        }
        TEST_END
    }

    /**
     * Testing the flip-flop configurations of the sky130 cells: an async-reset-low flop, an
     * async-set-low flop, and a plain flop that must report neither.
     *
     * Functions: get_component_as, get_async_reset_function, get_async_set_function
     */
    TEST_F(GateLibrarySky130Test, check_flip_flop_types)
    {
        TEST_START
        {
            ASSERT_NE(m_gl, nullptr);

            {
                // dfrtp_2: asynchronous reset, active low
                const GateType* gt = get_type("sky130_fd_sc_hd__dfrtp_2");
                ASSERT_NE(gt, nullptr);
                EXPECT_TRUE(gt->has_property(GateTypeProperty::sequential));
                EXPECT_TRUE(gt->has_property(GateTypeProperty::ff));

                FFComponent* ff = get_ff_component(gt);
                ASSERT_NE(ff, nullptr);
                EXPECT_EQ(ff->get_clock_function(), BooleanFunction::Var("CLK"));
                EXPECT_EQ(ff->get_next_state_function(), BooleanFunction::Var("D"));
                EXPECT_EQ(ff->get_async_reset_function(), BooleanFunction::Not(BooleanFunction::Var("RESET_B"), 1).get());
                EXPECT_TRUE(ff->get_async_set_function().is_empty());

                const GatePin* reset_pin = gt->get_pin_by_name("RESET_B");
                ASSERT_NE(reset_pin, nullptr);
                EXPECT_EQ(reset_pin->get_direction(), PinDirection::input);
                EXPECT_EQ(reset_pin->get_type(), PinType::reset);

                const GatePin* clock_pin = gt->get_pin_by_name("CLK");
                ASSERT_NE(clock_pin, nullptr);
                EXPECT_EQ(clock_pin->get_type(), PinType::clock);

                const GatePin* state_pin = gt->get_pin_by_name("Q");
                ASSERT_NE(state_pin, nullptr);
                EXPECT_EQ(state_pin->get_direction(), PinDirection::output);
                EXPECT_EQ(state_pin->get_type(), PinType::state);
            }
            {
                // dfstp_2: asynchronous set, active low
                const GateType* gt = get_type("sky130_fd_sc_hd__dfstp_2");
                ASSERT_NE(gt, nullptr);
                EXPECT_TRUE(gt->has_property(GateTypeProperty::ff));

                FFComponent* ff = get_ff_component(gt);
                ASSERT_NE(ff, nullptr);
                EXPECT_EQ(ff->get_clock_function(), BooleanFunction::Var("CLK"));
                EXPECT_EQ(ff->get_next_state_function(), BooleanFunction::Var("D"));
                EXPECT_EQ(ff->get_async_set_function(), BooleanFunction::Not(BooleanFunction::Var("SET_B"), 1).get());
                EXPECT_TRUE(ff->get_async_reset_function().is_empty());

                const GatePin* set_pin = gt->get_pin_by_name("SET_B");
                ASSERT_NE(set_pin, nullptr);
                EXPECT_EQ(set_pin->get_direction(), PinDirection::input);
                EXPECT_EQ(set_pin->get_type(), PinType::set);
            }
            {
                // dfxtp_2: neither set nor reset -- the negative case of the two above
                const GateType* gt = get_type("sky130_fd_sc_hd__dfxtp_2");
                ASSERT_NE(gt, nullptr);
                EXPECT_TRUE(gt->has_property(GateTypeProperty::ff));

                FFComponent* ff = get_ff_component(gt);
                ASSERT_NE(ff, nullptr);
                EXPECT_EQ(ff->get_clock_function(), BooleanFunction::Var("CLK"));
                EXPECT_EQ(ff->get_next_state_function(), BooleanFunction::Var("D"));
                EXPECT_TRUE(ff->get_async_reset_function().is_empty());
                EXPECT_TRUE(ff->get_async_set_function().is_empty());

                {
                    // querying a pin that does not exist is the point here, so keep its warning out of the log
                    NO_COUT_BLOCK;
                    EXPECT_EQ(gt->get_pin_by_name("RESET_B"), nullptr);
                    EXPECT_EQ(gt->get_pin_by_name("SET_B"), nullptr);
                }
            }
        }
        TEST_END
    }

    /**
     * Testing the Boolean functions of the compound and inverted-input cells against their full
     * truth tables, and the constants of the tie cell.
     *
     * Functions: get_boolean_function, evaluate
     */
    TEST_F(GateLibrarySky130Test, check_boolean_functions)
    {
        TEST_START
        {
            ASSERT_NE(m_gl, nullptr);

            {
                // a21oi_1: Y = !((A1 & A2) | B1)
                const GateType* gt = get_type("sky130_fd_sc_hd__a21oi_1");
                ASSERT_NE(gt, nullptr);
                EXPECT_TRUE(gt->has_property(GateTypeProperty::combinational));
                EXPECT_TRUE(gt->has_property(GateTypeProperty::c_aoi));
                expect_truth_table(gt, "Y", {"A1", "A2", "B1"}, [](u32 a) {
                    const bool a1 = (a >> 0) & 1, a2 = (a >> 1) & 1, b1 = (a >> 2) & 1;
                    return !((a1 && a2) || b1);
                });
            }
            {
                // o21ai_1: Y = !((A1 | A2) & B1)
                const GateType* gt = get_type("sky130_fd_sc_hd__o21ai_1");
                ASSERT_NE(gt, nullptr);
                EXPECT_TRUE(gt->has_property(GateTypeProperty::c_oai));
                expect_truth_table(gt, "Y", {"A1", "A2", "B1"}, [](u32 a) {
                    const bool a1 = (a >> 0) & 1, a2 = (a >> 1) & 1, b1 = (a >> 2) & 1;
                    return !((a1 || a2) && b1);
                });
            }
            {
                // nand2b_1: Y = !(!A_N & B) -- the inverted-input naming convention
                const GateType* gt = get_type("sky130_fd_sc_hd__nand2b_1");
                ASSERT_NE(gt, nullptr);
                EXPECT_TRUE(gt->has_property(GateTypeProperty::c_nand));
                expect_truth_table(gt, "Y", {"A_N", "B"}, [](u32 a) {
                    const bool a_n = (a >> 0) & 1, b = (a >> 1) & 1;
                    return !((!a_n) && b);
                });
            }
            {
                // xor2_1 drives X, xnor2_1 drives Y -- both output pin names have to survive
                const GateType* gt_xor = get_type("sky130_fd_sc_hd__xor2_1");
                ASSERT_NE(gt_xor, nullptr);
                expect_truth_table(gt_xor, "X", {"A", "B"}, [](u32 a) { return (((a >> 0) & 1) != ((a >> 1) & 1)); });

                const GateType* gt_xnor = get_type("sky130_fd_sc_hd__xnor2_1");
                ASSERT_NE(gt_xnor, nullptr);
                expect_truth_table(gt_xnor, "Y", {"A", "B"}, [](u32 a) { return (((a >> 0) & 1) == ((a >> 1) & 1)); });
            }
            {
                // conb_1: the tie cell, HI is a constant one and LO a constant zero
                const GateType* gt = get_type("sky130_fd_sc_hd__conb_1");
                ASSERT_NE(gt, nullptr);
                EXPECT_TRUE(gt->has_property(GateTypeProperty::power));
                EXPECT_TRUE(gt->has_property(GateTypeProperty::ground));

                const BooleanFunction hi = gt->get_boolean_function("HI");
                ASSERT_FALSE(hi.is_empty());
                EXPECT_TRUE(hi.get_variable_names().empty());
                const auto hi_res = hi.evaluate(std::unordered_map<std::string, BooleanFunction::Value>());
                ASSERT_TRUE(hi_res.is_ok());
                EXPECT_EQ(hi_res.get(), BooleanFunction::Value::ONE);

                const BooleanFunction lo = gt->get_boolean_function("LO");
                ASSERT_FALSE(lo.is_empty());
                EXPECT_TRUE(lo.get_variable_names().empty());
                const auto lo_res = lo.evaluate(std::unordered_map<std::string, BooleanFunction::Value>());
                ASSERT_TRUE(lo_res.is_ok());
                EXPECT_EQ(lo_res.get(), BooleanFunction::Value::ZERO);

                const GatePin* hi_pin = gt->get_pin_by_name("HI");
                ASSERT_NE(hi_pin, nullptr);
                EXPECT_EQ(hi_pin->get_type(), PinType::power);

                const GatePin* lo_pin = gt->get_pin_by_name("LO");
                ASSERT_NE(lo_pin, nullptr);
                EXPECT_EQ(lo_pin->get_type(), PinType::ground);
            }
        }
        TEST_END
    }

    /**
     * Testing that the sky130 fixture netlist parses against the shipped library and has the
     * structure documented in tests/fixtures/sky130_cells/README.md.
     *
     * Functions: parse
     */
    TEST_F(GateLibrarySky130Test, check_fixture_netlist)
    {
        TEST_START
        {
            ASSERT_NE(m_gl, nullptr);

            const std::filesystem::path netlist_file = utils::get_base_directory() / "bin" / "test-files" / "sky130_cells" / "sky130_cells.v";
            ASSERT_TRUE(std::filesystem::exists(netlist_file)) << "fixture missing at " << netlist_file.string();

            std::unique_ptr<Netlist> nl;
            {
                NO_COUT_BLOCK;
                nl = netlist_parser_manager::parse(netlist_file, m_gl);
            }
            ASSERT_NE(nl, nullptr);

            // 15 gates, see the table in the fixture README
            EXPECT_EQ(nl->get_gates().size(), 15);
            const std::map<std::string, u32> expected_counts = {{"sky130_fd_sc_hd__conb_1", 1},
                                                                {"sky130_fd_sc_hd__nand2b_1", 2},
                                                                {"sky130_fd_sc_hd__inv_1", 1},
                                                                {"sky130_fd_sc_hd__and2_1", 1},
                                                                {"sky130_fd_sc_hd__dfrtp_2", 2},
                                                                {"sky130_fd_sc_hd__dfstp_2", 1},
                                                                {"sky130_fd_sc_hd__dfxtp_2", 1},
                                                                {"sky130_fd_sc_hd__xor2_1", 1},
                                                                {"sky130_fd_sc_hd__xnor2_1", 1},
                                                                {"sky130_fd_sc_hd__a21oi_1", 1},
                                                                {"sky130_fd_sc_hd__o21ai_1", 1},
                                                                {"sky130_fd_sc_hd__buf_1", 2}};
            u32 counted = 0;
            for (const auto& [type_name, count] : expected_counts)
            {
                EXPECT_EQ(nl->get_gates(test_utils::gate_type_filter(type_name)).size(), count) << "wrong number of gates of type '" << type_name << "'";
                counted += count;
            }
            EXPECT_EQ(counted, 15);

            // 4 global inputs + 8 bus bits + 8 internal wires
            EXPECT_EQ(nl->get_nets().size(), 20);
            EXPECT_EQ(nl->get_global_input_nets().size(), 4);
            EXPECT_EQ(nl->get_global_output_nets().size(), 8);
            for (const std::string& name : {"clk", "rst_n", "set_n", "din"})
            {
                ASSERT_EQ(nl->get_nets(test_utils::net_name_filter(name)).size(), 1) << "missing input net '" << name << "'";
            }
            for (const std::string& name : {"tie_hi", "tie_lo", "n_nandb", "n_inv", "n_en", "n_and", "n_aoi", "n_oai"})
            {
                ASSERT_EQ(nl->get_nets(test_utils::net_name_filter(name)).size(), 1) << "missing internal net '" << name << "'";
            }
            for (u32 i = 0; i < 8; i++)
            {
                // HAL renames bus bits from '[i]' to '(i)'
                const std::string name = "dout(" + std::to_string(i) + ")";
                ASSERT_EQ(nl->get_nets(test_utils::net_name_filter(name)).size(), 1) << "missing bus net '" << name << "'";
                Net* net = *nl->get_nets(test_utils::net_name_filter(name)).begin();
                EXPECT_EQ(net->get_sources().size(), 1) << "bus net '" << name << "' is not driven exactly once";
            }

            // escaped identifiers lose their backslash, bus-indexed instance names keep their brackets
            for (const std::string& name : {"tie_cell", "u_din_nand", "u_tie_nand", "u_inv", "u_and", "u_xor", "u_xnor", "u_aoi", "u_oai", "buf_aoi_x", "buf_oai_x"})
            {
                EXPECT_NE(nl->get_gate_by_name(name), nullptr) << "missing gate '" << name << "'";
            }
            for (u32 i = 0; i < 4; i++)
            {
                const std::string name = "state_reg[" + std::to_string(i) + "]";
                EXPECT_NE(nl->get_gate_by_name(name), nullptr) << "missing gate '" << name << "'";
            }

            // the asynchronous control pins are connected the way the fixture declares them
            {
                Gate* ff_r = nl->get_gate_by_name("state_reg[0]");
                ASSERT_NE(ff_r, nullptr);
                Net* reset_net = ff_r->get_fan_in_net("RESET_B");
                ASSERT_NE(reset_net, nullptr);
                EXPECT_EQ(reset_net->get_name(), "rst_n");

                Gate* ff_s = nl->get_gate_by_name("state_reg[2]");
                ASSERT_NE(ff_s, nullptr);
                Net* set_net = ff_s->get_fan_in_net("SET_B");
                ASSERT_NE(set_net, nullptr);
                EXPECT_EQ(set_net->get_name(), "set_n");

                Gate* ff_x = nl->get_gate_by_name("state_reg[3]");
                ASSERT_NE(ff_x, nullptr);
                {
                    NO_COUT_BLOCK;
                    EXPECT_EQ(ff_x->get_fan_in_net("RESET_B"), nullptr);
                    EXPECT_EQ(ff_x->get_fan_in_net("SET_B"), nullptr);
                }
            }

            // both legs of the tie cell drive a real consumer
            {
                Gate* tie = nl->get_gate_by_name("tie_cell");
                ASSERT_NE(tie, nullptr);
                Net* hi = tie->get_fan_out_net("HI");
                ASSERT_NE(hi, nullptr);
                EXPECT_EQ(hi->get_name(), "tie_hi");
                EXPECT_EQ(hi->get_destinations().size(), 2);    // both inputs of u_tie_nand

                Net* lo = tie->get_fan_out_net("LO");
                ASSERT_NE(lo, nullptr);
                EXPECT_EQ(lo->get_name(), "tie_lo");
                EXPECT_EQ(lo->get_destinations().size(), 1);    // u_xnor.B
            }

            // no power pin of any cell is connected -- sky130 extraction output has none
            for (const Gate* g : nl->get_gates())
            {
                for (const std::string& pin : {"VPWR", "VGND", "VPB", "VNB"})
                {
                    EXPECT_EQ(g->get_fan_in_net(pin), nullptr) << "gate '" << g->get_name() << "' unexpectedly connects '" << pin << "'";
                }
            }
        }
        TEST_END
    }
}    // namespace hal
