#include "hal_core/netlist/gate_library/gate_library.h"

#include "hal_core/netlist/gate_library/gate_type.h"
#include "hal_core/utilities/log.h"
#include "netlist_test_utils.h"

#include "gtest/gtest.h"
#include <iostream>

namespace hal
{
    /**
     * Tests for class GateLibrary.
     */

    class GateLibraryTest : public ::testing::Test
    {
    protected:
        virtual void SetUp()
        {
            test_utils::init_log_channels();
        }

        virtual void TearDown()
        {
        }
    };

    /**
     * Testing the creation of a new GateLibrary and the addition of Gate types and includes to it
     *
     * Functions: constructor, create_gate_type, get_name, get_gate_types, get_vcc_gate_types, get_gnd_gate_types,
     *            add_include, get_includes
     */
    TEST_F(GateLibraryTest, check_library)
    {
        TEST_START
        {
            auto gl = std::make_unique<GateLibrary>("imaginary_path", "gl");
            auto other_gl = std::make_unique<GateLibrary>("imaginary_path", "other_gl");

            // check name and path
            EXPECT_EQ(gl->get_name(), "gl");
            EXPECT_EQ(other_gl->get_name(), "other_gl");
            EXPECT_EQ(gl->get_path(), "imaginary_path");
            EXPECT_EQ(other_gl->get_path(), "imaginary_path");

            // create gate types
            // AND gate type
            auto gt_and = gl->create_gate_type("gt_and");
            ASSERT_TRUE(gt_and != nullptr);
            ASSERT_TRUE(gt_and->create_pin("I0", PinDirection::input).is_ok());
            ASSERT_TRUE(gt_and->create_pin("I1", PinDirection::input).is_ok());
            ASSERT_TRUE(gt_and->create_pin("O", PinDirection::output).is_ok());
            gt_and->add_boolean_function("O", BooleanFunction::from_string("I0 & I1").get());

            // OR gate type
            auto gt_or = gl->create_gate_type("gt_or", {GateTypeProperty::combinational});
            ASSERT_TRUE(gt_or != nullptr);
            ASSERT_TRUE(gt_or->create_pin("I0", PinDirection::input).is_ok());
            ASSERT_TRUE(gt_or->create_pin("I1", PinDirection::input).is_ok());
            ASSERT_TRUE(gt_or->create_pin("O", PinDirection::output).is_ok());
            gt_or->add_boolean_function("O", BooleanFunction::from_string("I0 | I1").get());

            // GND gate type
            auto gt_gnd = gl->create_gate_type("gt_gnd");
            ASSERT_TRUE(gt_gnd != nullptr);
            ASSERT_TRUE(gt_gnd->create_pin("O", PinDirection::output).is_ok());
            gt_gnd->add_boolean_function("O", BooleanFunction::Const(BooleanFunction::Value::ZERO));
            gl->mark_gnd_gate_type(gt_gnd);

            // VCC gate type
            auto gt_vcc = gl->create_gate_type("gt_vcc");
            ASSERT_TRUE(gt_vcc != nullptr);
            ASSERT_TRUE(gt_vcc->create_pin("O", PinDirection::output).is_ok());
            gt_vcc->add_boolean_function("O",  BooleanFunction::Const(BooleanFunction::Value::ONE));
            gl->mark_vcc_gate_type(gt_vcc);

            // FF gate type
            auto gt_ff = gl->create_gate_type("gt_ff", {GateTypeProperty::ff});
            ASSERT_TRUE(gt_ff != nullptr);
            

            // Latch gate type
            auto gt_latch = gl->create_gate_type("gt_latch", {GateTypeProperty::latch});
            ASSERT_TRUE(gt_latch != nullptr);

            // LUT gate type
            auto gt_lut = gl->create_gate_type("gt_lut", {GateTypeProperty::c_lut});
            ASSERT_TRUE(gt_lut != nullptr);

            // check if all gate types contained in library
            EXPECT_EQ(gl->get_gate_types(),(std::unordered_map<std::string, GateType*>({{"gt_and", gt_and}, {"gt_gnd", gt_gnd}, {"gt_vcc", gt_vcc}, {"gt_or", gt_or}, {"gt_ff", gt_ff}, {"gt_latch", gt_latch}, {"gt_lut", gt_lut}})));
            EXPECT_EQ(gl->get_vcc_gate_types(), (std::unordered_map<std::string, GateType*>({{"gt_vcc", gt_vcc}})));
            EXPECT_EQ(gl->get_gnd_gate_types(), (std::unordered_map<std::string, GateType*>({{"gt_gnd", gt_gnd}})));

            // check base types
            EXPECT_EQ(gt_and->get_properties(), std::set<GateTypeProperty>({GateTypeProperty::combinational}));
            EXPECT_EQ(gt_or->get_properties(), std::set<GateTypeProperty>({GateTypeProperty::combinational}));
            EXPECT_EQ(gt_ff->get_properties(), std::set<GateTypeProperty>({GateTypeProperty::ff}));
            EXPECT_EQ(gt_latch->get_properties(), std::set<GateTypeProperty>({GateTypeProperty::latch}));
            EXPECT_EQ(gt_lut->get_properties(), std::set<GateTypeProperty>({GateTypeProperty::c_lut}));

            // check contains_gate_type and contains_gate_type_by_name
            EXPECT_TRUE(gl->contains_gate_type(gt_and));
            EXPECT_FALSE(gl->contains_gate_type(nullptr));
            GateType* gt_nil = other_gl->create_gate_type("not_in_library", {GateTypeProperty::combinational});
            EXPECT_FALSE(gl->contains_gate_type(gt_nil));

            EXPECT_TRUE(gl->contains_gate_type_by_name(gt_and->get_name()));
            EXPECT_FALSE(gl->contains_gate_type_by_name(""));
            EXPECT_FALSE(gl->contains_gate_type_by_name("not_in_library"));

            // check get_gate_type_by_name
            EXPECT_EQ(gl->get_gate_type_by_name("gt_and"), gt_and);
            EXPECT_EQ(gl->get_gate_type_by_name(""), nullptr);
            EXPECT_EQ(gl->get_gate_type_by_name("not_in_library"), nullptr);

            // Check the addition of includes
            gl->add_include("in.clu.de");
            gl->add_include("another.include");
            gl->add_include("last.include");
            EXPECT_EQ(gl->get_includes(), std::vector<std::string>({"in.clu.de", "another.include", "last.include"}));

            // Replace gate type
            u32 repl_id = gt_and->get_id();
            EXPECT_EQ(gl->replace_gate_type(repl_id, "gt_or"), nullptr); // must avoid name collision
            gt_and = gl->replace_gate_type(repl_id, "gt_and");           // same name as before is ok
            ASSERT_TRUE(gt_and != nullptr);
            gt_and = gl->replace_gate_type(repl_id, "gt_and_repl");      // new name also ok
            ASSERT_TRUE(gt_and != nullptr);
            EXPECT_EQ(gl->get_gate_type_by_name("gt_and"), nullptr);     // no longer in gate library
            EXPECT_EQ(gl->get_gate_type_by_name("gt_and_repl"), gt_and); // replaced by type with new name
        }
        TEST_END
    }

    /**
     * Testing the provenance of a gate library, i.e., the files it has been assembled from.
     *
     * Functions: get_source_paths, is_composite, set_path
     */
    TEST_F(GateLibraryTest, check_source_paths)
    {
        TEST_START
        {
            auto gl = std::make_unique<GateLibrary>("some_path.hgl", "gl");
            EXPECT_EQ(gl->get_source_paths(), std::vector<std::filesystem::path>({"some_path.hgl"}));
            EXPECT_FALSE(gl->is_composite());

            // a single-source library follows its file when that is moved
            gl->set_path("other_path.hgl");
            EXPECT_EQ(gl->get_source_paths(), std::vector<std::filesystem::path>({"other_path.hgl"}));
            EXPECT_FALSE(gl->is_composite());
        }
        {
            // a library without a file has no provenance at all
            auto gl = std::make_unique<GateLibrary>(std::filesystem::path(), "gl");
            EXPECT_TRUE(gl->get_source_paths().empty());
            EXPECT_FALSE(gl->is_composite());
        }
        TEST_END
    }

    /**
     * Testing that a gate library can take over the gate types of other gate libraries, which is what netlists
     * referencing cells from more than one gate library file rely on.
     *
     * Functions: absorb, get_source_paths, is_composite
     */
    TEST_F(GateLibraryTest, check_absorb)
    {
        TEST_START
        {
            NO_COUT_TEST_BLOCK;

            auto gl_a = std::make_unique<GateLibrary>("a.hgl", "gl_a");
            GateType* a_buf = gl_a->create_gate_type("BUF");
            ASSERT_NE(a_buf, nullptr);
            ASSERT_TRUE(a_buf->create_pin("I", PinDirection::input).is_ok());
            ASSERT_TRUE(a_buf->create_pin("O", PinDirection::output).is_ok());
            GateType* a_shared = gl_a->create_gate_type("SHARED");
            ASSERT_NE(a_shared, nullptr);
            ASSERT_TRUE(a_shared->create_pin("FROM_A", PinDirection::input).is_ok());
            GateType* a_gnd = gl_a->create_gate_type("GND", {GateTypeProperty::ground});
            ASSERT_NE(a_gnd, nullptr);
            ASSERT_TRUE(a_gnd->create_pin("O", PinDirection::output, PinType::ground).is_ok());
            a_gnd->add_boolean_function("O", BooleanFunction::Const(BooleanFunction::Value::ZERO));
            ASSERT_TRUE(gl_a->mark_gnd_gate_type(a_gnd));
            gl_a->add_include("a.include");
            gl_a->set_gate_location_data_category("attribute");
            gl_a->set_gate_location_data_identifiers("X", "Y");

            auto gl_b = std::make_unique<GateLibrary>("b.hgl", "gl_b");
            GateType* b_ram = gl_b->create_gate_type("RAM");
            ASSERT_NE(b_ram, nullptr);
            ASSERT_TRUE(b_ram->create_pin("D", PinDirection::input).is_ok());
            GateType* b_shared = gl_b->create_gate_type("SHARED");
            ASSERT_NE(b_shared, nullptr);
            ASSERT_TRUE(b_shared->create_pin("FROM_B", PinDirection::input).is_ok());
            gl_b->add_include("b.include");

            auto composite = std::make_unique<GateLibrary>(std::filesystem::path(), "composite");
            ASSERT_TRUE(composite->absorb(gl_a.get()).is_ok());
            ASSERT_TRUE(composite->absorb(gl_b.get()).is_ok());

            // all gate types are now part of the composite library
            EXPECT_EQ(composite->get_gate_types().size(), 4);
            ASSERT_TRUE(composite->contains_gate_type_by_name("BUF"));
            ASSERT_TRUE(composite->contains_gate_type_by_name("RAM"));
            ASSERT_TRUE(composite->contains_gate_type_by_name("GND"));
            ASSERT_TRUE(composite->contains_gate_type_by_name("SHARED"));

            // the library absorbed first wins the name collision
            GateType* shared = composite->get_gate_type_by_name("SHARED");
            ASSERT_NE(shared, nullptr);
            EXPECT_EQ(shared, a_shared);
            EXPECT_NE(shared->get_pin_by_name("FROM_A"), nullptr);
            EXPECT_EQ(shared->get_pin_by_name("FROM_B"), nullptr);

            // the absorbed gate types belong to the composite library now
            for (const auto& [name, gt] : composite->get_gate_types())
            {
                EXPECT_EQ(gt->get_gate_library(), composite.get()) << "gate type '" << name << "' still points to its original library";
                EXPECT_TRUE(composite->contains_gate_type(gt));
            }

            // gate type IDs are unique within the composite library
            std::set<u32> ids;
            for (const auto& [name, gt] : composite->get_gate_types())
            {
                UNUSED(name);
                EXPECT_TRUE(ids.insert(gt->get_id()).second);
            }

            // GND markings and includes are carried over, provenance lists both files in order
            EXPECT_EQ(composite->get_gnd_gate_types().size(), 1);
            EXPECT_TRUE(composite->get_gnd_gate_types().find("GND") != composite->get_gnd_gate_types().end());
            EXPECT_EQ(composite->get_includes(), std::vector<std::string>({"a.include", "b.include"}));

            // the library absorbed first also decides how gate locations are stored
            EXPECT_EQ(composite->get_gate_location_data_category(), "attribute");
            EXPECT_EQ(composite->get_gate_location_data_identifiers(), std::make_pair(std::string("X"), std::string("Y")));

            EXPECT_EQ(composite->get_source_paths(), std::vector<std::filesystem::path>({"a.hgl", "b.hgl"}));
            EXPECT_TRUE(composite->is_composite());

            // the absorbed libraries have been emptied out
            EXPECT_TRUE(gl_a->get_gate_types().empty());
            EXPECT_TRUE(gl_b->get_gate_types().empty());
        }
        {
            NO_COUT_TEST_BLOCK;

            // the same libraries in the other order let the other definition win
            auto gl_a = std::make_unique<GateLibrary>("a.hgl", "gl_a");
            ASSERT_NE(gl_a->create_gate_type("SHARED"), nullptr);
            ASSERT_TRUE(gl_a->get_gate_type_by_name("SHARED")->create_pin("FROM_A", PinDirection::input).is_ok());

            auto gl_b = std::make_unique<GateLibrary>("b.hgl", "gl_b");
            ASSERT_NE(gl_b->create_gate_type("SHARED"), nullptr);
            ASSERT_TRUE(gl_b->get_gate_type_by_name("SHARED")->create_pin("FROM_B", PinDirection::input).is_ok());

            auto composite = std::make_unique<GateLibrary>(std::filesystem::path(), "composite");
            ASSERT_TRUE(composite->absorb(gl_b.get()).is_ok());
            ASSERT_TRUE(composite->absorb(gl_a.get()).is_ok());

            GateType* shared = composite->get_gate_type_by_name("SHARED");
            ASSERT_NE(shared, nullptr);
            EXPECT_NE(shared->get_pin_by_name("FROM_B"), nullptr);
            EXPECT_EQ(shared->get_pin_by_name("FROM_A"), nullptr);
            EXPECT_EQ(composite->get_source_paths(), std::vector<std::filesystem::path>({"b.hgl", "a.hgl"}));
        }
        {
            NO_COUT_TEST_BLOCK;

            // explicitly asking for the opposite lets the later definition replace the earlier one
            auto gl_a = std::make_unique<GateLibrary>("a.hgl", "gl_a");
            ASSERT_NE(gl_a->create_gate_type("SHARED"), nullptr);
            ASSERT_TRUE(gl_a->get_gate_type_by_name("SHARED")->create_pin("FROM_A", PinDirection::input).is_ok());

            auto gl_b = std::make_unique<GateLibrary>("b.hgl", "gl_b");
            ASSERT_NE(gl_b->create_gate_type("SHARED"), nullptr);
            ASSERT_TRUE(gl_b->get_gate_type_by_name("SHARED")->create_pin("FROM_B", PinDirection::input).is_ok());

            auto composite = std::make_unique<GateLibrary>(std::filesystem::path(), "composite");
            ASSERT_TRUE(composite->absorb(gl_a.get()).is_ok());
            ASSERT_TRUE(composite->absorb(gl_b.get(), true).is_ok());

            EXPECT_EQ(composite->get_gate_types().size(), 1);
            GateType* shared = composite->get_gate_type_by_name("SHARED");
            ASSERT_NE(shared, nullptr);
            EXPECT_NE(shared->get_pin_by_name("FROM_B"), nullptr);
        }
        {
            NO_COUT_TEST_BLOCK;

            // invalid input
            auto gl = std::make_unique<GateLibrary>("a.hgl", "gl");
            EXPECT_TRUE(gl->absorb(nullptr).is_error());
            EXPECT_TRUE(gl->absorb(gl.get()).is_error());
        }
        TEST_END
    }

    /**
     * Testing the creation of black box gate types standing in for cells that no gate library defines.
     *
     * Functions: create_black_box_gate_type, is_black_box_gate_type, get_black_box_gate_types
     */
    TEST_F(GateLibraryTest, check_black_box_gate_type)
    {
        TEST_START
        {
            NO_COUT_TEST_BLOCK;

            auto gl = std::make_unique<GateLibrary>("a.hgl", "gl");

            auto res = gl->create_black_box_gate_type("MY_RAM", {{"CLK", 1}, {"D", 4}, {"Q", 1}});
            ASSERT_TRUE(res.is_ok());
            GateType* bb = res.get();
            ASSERT_NE(bb, nullptr);

            // known to the library, and known to be a black box
            EXPECT_TRUE(gl->contains_gate_type(bb));
            EXPECT_TRUE(gl->is_black_box_gate_type(bb));
            EXPECT_EQ(gl->get_black_box_gate_types().size(), 1);
            EXPECT_TRUE(gl->get_black_box_gate_types().find("MY_RAM") != gl->get_black_box_gate_types().end());

            // nothing is known about the internals of a black box
            EXPECT_TRUE(bb->get_properties().empty());
            EXPECT_TRUE(bb->get_boolean_functions().empty());

            // single-bit ports become single pins, multi-bit ports become pin groups ordered from the highest bit down
            EXPECT_EQ(bb->get_pin_names(), std::vector<std::string>({"CLK", "D(3)", "D(2)", "D(1)", "D(0)", "Q"}));
            for (auto* pin : bb->get_pins())
            {
                EXPECT_EQ(pin->get_direction(), PinDirection::inout) << "pin '" << pin->get_name() << "' does not have unknown direction";
            }
            auto* group = bb->get_pin_group_by_name("D");
            ASSERT_NE(group, nullptr);
            EXPECT_EQ(group->get_pins().size(), 4);
            // the bits are indexed from 0 regardless of the order the pins are listed in
            EXPECT_EQ(bb->get_pin_by_name("D(0)")->get_group().second, 0);
            EXPECT_EQ(bb->get_pin_by_name("D(3)")->get_group().second, 3);

            // a gate type that is not a black box is not reported as one
            GateType* regular = gl->create_gate_type("BUF");
            ASSERT_NE(regular, nullptr);
            EXPECT_FALSE(gl->is_black_box_gate_type(regular));
            EXPECT_FALSE(gl->is_black_box_gate_type(nullptr));

            // no black box may shadow an existing gate type
            EXPECT_TRUE(gl->create_black_box_gate_type("BUF", {}).is_error());
            EXPECT_TRUE(gl->create_black_box_gate_type("MY_RAM", {}).is_error());

            // removing a black box takes its marking with it, so the name is free again
            gl->remove_gate_type("MY_RAM");
            EXPECT_FALSE(gl->is_black_box_gate_type(bb));
            EXPECT_TRUE(gl->get_black_box_gate_types().empty());
            EXPECT_TRUE(gl->create_black_box_gate_type("MY_RAM", {{"CLK", 1}}).is_ok());
        }
        {
            NO_COUT_TEST_BLOCK;

            // black box markings survive being absorbed into another library
            auto gl = std::make_unique<GateLibrary>("a.hgl", "gl");
            ASSERT_TRUE(gl->create_black_box_gate_type("MY_PAD", {{"PORT_0", 1}}).is_ok());

            auto composite = std::make_unique<GateLibrary>(std::filesystem::path(), "composite");
            ASSERT_TRUE(composite->absorb(gl.get()).is_ok());

            GateType* bb = composite->get_gate_type_by_name("MY_PAD");
            ASSERT_NE(bb, nullptr);
            EXPECT_TRUE(composite->is_black_box_gate_type(bb));
        }
        TEST_END
    }
}    //namespace hal
