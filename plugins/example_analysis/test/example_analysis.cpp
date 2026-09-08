#include "example_analysis/example_analysis.h"

#include "example_analysis/plugin_example_analysis.h"
#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/gate_library/gate_library.h"
#include "hal_core/netlist/gate_library/gate_type.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/netlist/netlist_factory.h"
#include "netlist_test_utils.h"

#include <memory>

namespace hal
{
    /**
     * Tests for the example_analysis plugin.
     *
     * The interesting cases are the negative ones: a netlist this analysis cannot look at must say
     * so, and a gate type it cannot evaluate must be named rather than dropped. An analysis that
     * returns an empty result for both "nothing there" and "cannot look" is worse than no analysis.
     */
    class ExampleAnalysisTest : public ::testing::Test
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

        /** A netlist with `count` DFFs clocked by one buffer, plus the buffer itself. */
        struct Fixture
        {
            std::unique_ptr<Netlist> netlist;
            std::vector<Gate*> flip_flops;
            Gate* clock_buffer = nullptr;
        };

        Fixture build_clocked(u32 count, const std::string& prefix, const std::string& clock_net_name)
        {
            Fixture fixture;
            fixture.netlist       = test_utils::create_empty_netlist();
            const GateLibrary* gl = fixture.netlist->get_gate_library();

            fixture.clock_buffer = fixture.netlist->create_gate(gl->get_gate_type_by_name("BUF"), prefix + "_clock_buffer");
            for (u32 i = 0; i < count; i++)
            {
                Gate* flip_flop = fixture.netlist->create_gate(gl->get_gate_type_by_name("DFF"), prefix + "_ff_" + std::to_string(i));
                test_utils::connect(fixture.netlist.get(), fixture.clock_buffer, "O", flip_flop, "CLK", clock_net_name);
                fixture.flip_flops.push_back(flip_flop);
            }
            return fixture;
        }
    };

    /**
     * Flip-flops driven by the same clock net end up in one domain, and the domains come back
     * largest first.
     *
     * Functions: analyze
     */
    TEST_F(ExampleAnalysisTest, check_groups_by_clock_net)
    {
        TEST_START
        {
            Fixture fixture       = build_clocked(3, "a", "clk_a");
            const GateLibrary* gl = fixture.netlist->get_gate_library();

            // A second, smaller clock domain in the same netlist.
            Gate* second_buffer = fixture.netlist->create_gate(gl->get_gate_type_by_name("BUF"), "b_clock_buffer");
            Gate* other_ff      = fixture.netlist->create_gate(gl->get_gate_type_by_name("DFF"), "b_ff_0");
            test_utils::connect(fixture.netlist.get(), second_buffer, "O", other_ff, "CLK", "clk_b");

            auto result = example_analysis::analyze(fixture.netlist.get());
            ASSERT_TRUE(result.is_ok());
            const example_analysis::Report report = result.get();

            EXPECT_EQ(report.sequential_gate_count, 4);
            ASSERT_EQ(report.domains.size(), 2);
            EXPECT_EQ(report.domains.at(0).gates.size(), 3);
            EXPECT_EQ(report.domains.at(0).clock_net->get_name(), "clk_a");
            EXPECT_EQ(report.domains.at(1).gates.size(), 1);
            EXPECT_EQ(report.domains.at(1).clock_net->get_name(), "clk_b");
            EXPECT_TRUE(report.unresolved_gates.empty());
            EXPECT_TRUE(report.unsupported.empty());
        }
        TEST_END
    }

    /**
     * A flip-flop whose clock pin is driven by nothing is reported as unresolved instead of being
     * counted into some arbitrary domain.
     *
     * Functions: analyze
     */
    TEST_F(ExampleAnalysisTest, check_undriven_clock_pin_is_unresolved)
    {
        TEST_START
        {
            Fixture fixture       = build_clocked(1, "a", "clk_a");
            const GateLibrary* gl = fixture.netlist->get_gate_library();
            Gate* dangling        = fixture.netlist->create_gate(gl->get_gate_type_by_name("DFF"), "dangling_ff");

            auto result = example_analysis::analyze(fixture.netlist.get());
            ASSERT_TRUE(result.is_ok());
            const example_analysis::Report report = result.get();

            EXPECT_EQ(report.sequential_gate_count, 2);
            ASSERT_EQ(report.domains.size(), 1);
            ASSERT_EQ(report.unresolved_gates.size(), 1);
            EXPECT_EQ(report.unresolved_gates.at(0), dangling);
        }
        TEST_END
    }

    /**
     * A sequential gate type without a clock pin is named as unsupported, with a reason, rather
     * than silently skipped.
     *
     * Functions: analyze
     */
    TEST_F(ExampleAnalysisTest, check_sequential_type_without_clock_pin_is_reported)
    {
        TEST_START
        {
            auto library     = std::make_unique<GateLibrary>("example_analysis_test.hgl", "ExampleAnalysisTestLibrary");
            GateType* latch  = library->create_gate_type("LATCH_NO_CLOCK", {GateTypeProperty::sequential, GateTypeProperty::latch});
            ASSERT_NE(latch, nullptr);
            ASSERT_TRUE(latch->create_pin("D", PinDirection::input, PinType::data).is_ok());
            ASSERT_TRUE(latch->create_pin("Q", PinDirection::output, PinType::state).is_ok());

            std::unique_ptr<Netlist> netlist = netlist_factory::create_netlist(library.get());
            ASSERT_NE(netlist, nullptr);
            netlist->create_gate(latch, "latch_0");
            netlist->create_gate(latch, "latch_1");

            auto result = example_analysis::analyze(netlist.get());
            ASSERT_TRUE(result.is_ok());
            const example_analysis::Report report = result.get();

            EXPECT_TRUE(report.domains.empty());
            ASSERT_EQ(report.unsupported.size(), 1);
            EXPECT_EQ(report.unsupported.at(0).gate_type, latch);
            EXPECT_EQ(report.unsupported.at(0).count, 2);
            EXPECT_TRUE(test_utils::string_contains_substring(report.unsupported.at(0).reason, "LATCH_NO_CLOCK"));
            EXPECT_TRUE(test_utils::string_contains_substring(report.unsupported.at(0).reason, "clock"));
        }
        TEST_END
    }

    /**
     * A netlist without a single sequential gate is an error that names the missing property, not
     * an empty result that reads like "no registers found".
     *
     * Functions: analyze
     */
    TEST_F(ExampleAnalysisTest, check_combinational_netlist_is_an_actionable_error)
    {
        TEST_START
        {
            std::unique_ptr<Netlist> netlist = test_utils::create_empty_netlist();
            netlist->create_gate(netlist->get_gate_library()->get_gate_type_by_name("BUF"), "buffer_0");

            auto result = example_analysis::analyze(netlist.get());
            ASSERT_TRUE(result.is_error());
            const std::string message = result.get_error().get();
            EXPECT_TRUE(test_utils::string_contains_substring(message, "sequential"));
            EXPECT_TRUE(test_utils::string_contains_substring(message, "hal_capabilities"));
        }
        TEST_END
    }

    /**
     * A null netlist is an error, not a crash.
     *
     * Functions: analyze
     */
    TEST_F(ExampleAnalysisTest, check_nullptr_netlist)
    {
        TEST_START
        {
            auto result = example_analysis::analyze(nullptr);
            EXPECT_TRUE(result.is_error());
        }
        TEST_END
    }

    /**
     * The capability declaration compiled into the plugin is the one from the source tree, and it
     * describes this plugin. `python tools/hal_capabilities list --probe` checks the same thing
     * against a built HAL; this checks it without one.
     *
     * Functions: get_capabilities, get_name, get_dependencies
     */
    TEST_F(ExampleAnalysisTest, check_capability_declaration_is_compiled_in)
    {
        TEST_START
        {
            const std::string capabilities = ExampleAnalysisPlugin::get_capabilities();
            EXPECT_FALSE(capabilities.empty());
            EXPECT_TRUE(test_utils::string_contains_substring(capabilities, "\"capabilities_version\""));
            EXPECT_TRUE(test_utils::string_contains_substring(capabilities, "\"name\": \"example_analysis\""));

            ExampleAnalysisPlugin plugin;
            EXPECT_EQ(plugin.get_name(), "example_analysis");
            EXPECT_TRUE(plugin.get_dependencies().empty());
        }
        TEST_END
    }
}    // namespace hal
