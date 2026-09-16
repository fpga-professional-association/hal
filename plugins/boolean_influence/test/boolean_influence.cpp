#include "boolean_influence/boolean_influence.h"

#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/gate_library/gate_library.h"
#include "hal_core/netlist/gate_library/gate_type.h"
#include "hal_core/netlist/gate_library/gate_type_component/gate_type_component.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/netlist/netlist_factory.h"
#include "netlist_test_utils.h"

#include <map>
#include <memory>
#include <string>
#include <vector>

namespace hal
{
    /**
     * Tests for how `boolean_influence` picks the data input pin of a flip-flop.
     *
     * The flip-flop type below declares two pins of type `data`, `D` and `AD`, which is the shape of the
     * Agilex `tennm_ff` (`d` plus the secondary `asdata`). Asking the gate *type* for its data pins and
     * refusing unless there is exactly one -- what this used to do -- rejects every netlist built from
     * such a library, whether or not the design uses the second pin for anything.
     */
    class BooleanInfluenceTest : public ::testing::Test
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

        struct Fixture
        {
            std::unique_ptr<GateLibrary> library;
            std::unique_ptr<Netlist> netlist;
            Gate* flip_flop = nullptr;
            Net* data_source = nullptr;
        };

        std::unique_ptr<GateLibrary> build_library()
        {
            auto lib = std::make_unique<GateLibrary>("boolean_influence_test.hgl", "BooleanInfluenceTestLibrary");

            GateType* vcc = lib->create_gate_type("VCC", {GateTypeProperty::combinational, GateTypeProperty::power});
            if (vcc == nullptr || vcc->create_pin("O", PinDirection::output, PinType::power).is_error())
            {
                return nullptr;
            }
            vcc->add_boolean_function("O", BooleanFunction::from_string("1").get());
            if (!lib->mark_vcc_gate_type(vcc))
            {
                return nullptr;
            }

            GateType* inv = lib->create_gate_type("INV", {GateTypeProperty::combinational, GateTypeProperty::c_inverter});
            if (inv == nullptr || inv->create_pin("I", PinDirection::input).is_error() || inv->create_pin("O", PinDirection::output).is_error())
            {
                return nullptr;
            }
            inv->add_boolean_function("O", BooleanFunction::from_string("!I").get());

            GateType* ff = lib->create_gate_type("DFF_TWO_DATA",
                                                 {GateTypeProperty::sequential, GateTypeProperty::ff},
                                                 GateTypeComponent::create_ff_component(GateTypeComponent::create_state_component(GateTypeComponent::create_init_component("generic", {"INIT"}), "IQ", "IQN"),
                                                                                        BooleanFunction::from_string("D").get(),
                                                                                        BooleanFunction::from_string("CLK").get()));
            if (ff == nullptr || ff->create_pin("CLK", PinDirection::input, PinType::clock).is_error() || ff->create_pin("D", PinDirection::input, PinType::data).is_error()
                || ff->create_pin("AD", PinDirection::input, PinType::data).is_error() || ff->create_pin("Q", PinDirection::output, PinType::state).is_error())
            {
                return nullptr;
            }
            ff->add_boolean_function("Q", BooleanFunction::from_string("IQ").get());

            return lib;
        }

        /**
         * A flip-flop whose data input is the inverse of the primary input `a`. With
         * `drive_second_data_pin` the secondary data pin is driven by an inverter of its own instead of
         * being tied to VCC, which makes the data input genuinely ambiguous.
         */
        Fixture build(bool drive_second_data_pin)
        {
            Fixture fixture;
            fixture.library = build_library();
            if (fixture.library == nullptr)
            {
                return fixture;
            }

            const GateLibrary* gl = fixture.library.get();
            fixture.netlist       = netlist_factory::create_netlist(gl);
            if (fixture.netlist == nullptr)
            {
                return fixture;
            }
            Netlist* nl = fixture.netlist.get();

            fixture.flip_flop = nl->create_gate(gl->get_gate_type_by_name("DFF_TWO_DATA"), "state_ff");
            Gate* inv         = nl->create_gate(gl->get_gate_type_by_name("INV"), "data_logic");

            Net* clk = nl->create_net("clk");
            clk->mark_global_input_net();
            clk->add_destination(fixture.flip_flop, "CLK");

            fixture.data_source = nl->create_net("a");
            fixture.data_source->mark_global_input_net();
            fixture.data_source->add_destination(inv, "I");

            Net* next = nl->create_net("next");
            next->add_source(inv, "O");
            next->add_destination(fixture.flip_flop, "D");

            Net* q = nl->create_net("q");
            q->add_source(fixture.flip_flop, "Q");
            q->mark_global_output_net();

            if (drive_second_data_pin)
            {
                Gate* other_inv = nl->create_gate(gl->get_gate_type_by_name("INV"), "second_data_logic");
                Net* other_in   = nl->create_net("b");
                other_in->mark_global_input_net();
                other_in->add_destination(other_inv, "I");
                Net* other = nl->create_net("other");
                other->add_source(other_inv, "O");
                other->add_destination(fixture.flip_flop, "AD");
            }
            else
            {
                Gate* vcc_gate = nl->create_gate(gl->get_gate_type_by_name("VCC"), "vcc_gate");
                nl->mark_vcc_gate(vcc_gate);
                Net* one = nl->create_net("'1'");
                one->add_source(vcc_gate, "O");
                one->add_destination(fixture.flip_flop, "AD");
            }

            return fixture;
        }
    };

    /**
     * A flip-flop type with a second, tied-off data pin gets its influence computed from the data pin
     * that carries logic. The constant one is not a reason to refuse the gate.
     *
     * Functions: get_boolean_influences_of_gate_deterministic
     */
    TEST_F(BooleanInfluenceTest, check_tied_off_second_data_pin_is_ignored)
    {
        TEST_START
        {
            Fixture fixture = build(false);
            ASSERT_NE(fixture.netlist, nullptr);
            ASSERT_NE(fixture.flip_flop, nullptr);

            auto res = boolean_influence::get_boolean_influences_of_gate_deterministic(fixture.flip_flop);
            ASSERT_TRUE(res.is_ok()) << res.get_error().get();
            const std::map<Net*, double> influences = res.get();

            // The next state is the inverse of `a` and depends on nothing else, and inverting a variable
            // flips the output for every assignment of the others.
            ASSERT_EQ(influences.size(), 1);
            ASSERT_EQ(influences.count(fixture.data_source), 1);
            EXPECT_DOUBLE_EQ(influences.at(fixture.data_source), 1.0);
        }
        TEST_END
    }

    /**
     * Two data pins that both carry logic really is ambiguous, and the error says which pins it could not
     * choose between rather than only how many there are.
     *
     * Functions: get_boolean_influences_of_gate_deterministic
     */
    TEST_F(BooleanInfluenceTest, check_two_driven_data_pins_are_an_informative_error)
    {
        TEST_START
        {
            Fixture fixture = build(true);
            ASSERT_NE(fixture.netlist, nullptr);
            ASSERT_NE(fixture.flip_flop, nullptr);

            auto res = boolean_influence::get_boolean_influences_of_gate_deterministic(fixture.flip_flop);
            ASSERT_TRUE(res.is_error());

            const std::string message = res.get_error().get();
            EXPECT_TRUE(test_utils::string_contains_substring(message, "DFF_TWO_DATA"));
            EXPECT_TRUE(test_utils::string_contains_substring(message, "data"));
            EXPECT_TRUE(test_utils::string_contains_substring(message, "D"));
            EXPECT_TRUE(test_utils::string_contains_substring(message, "AD"));
        }
        TEST_END
    }
}    // namespace hal
