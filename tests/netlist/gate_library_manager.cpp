#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/gate_library/gate_library.h"
#include "hal_core/netlist/gate_library/gate_library_manager.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/netlist/netlist_factory.h"
#include "hal_core/netlist/netlist_parser/netlist_parser_manager.h"
#include "hal_core/plugin_system/plugin_manager.h"
#include "hal_core/utilities/log.h"
#include "hal_core/utilities/utils.h"
#include "netlist_test_utils.h"

#include "gtest/gtest.h"
#include <algorithm>
#include <fstream>
#include <iostream>

namespace hal
{
    class GateLibraryManagerTest : public ::testing::Test
    {
    protected:
        // The path, where the library is temporary stored
        std::filesystem::path m_test_lib_path;

        virtual void SetUp()
        {
            NO_COUT_BLOCK;
            test_utils::init_log_channels();
            plugin_manager::load_all_plugins();
            m_test_lib_path = (utils::get_gate_library_directories()[0]) / "test1.lib";
        }

        virtual void TearDown()
        {
            std::filesystem::remove(m_test_lib_path);
            plugin_manager::unload_all_plugins();
        }

        /**
         * Creates a minimal custom Gate library used for testing the Gate library manager
         */
        void create_test_lib()
        {
            std::ofstream test_lib(m_test_lib_path.string());
            test_lib << "/* This file only exists for testing purposes and should be already destroyed*/\n"
                        "library ("
                     << "example_lib"
                     << ") {\n"
                        "    define(cell);\n"
                        "    cell(GND) {\n"
                        "        pin(O) {\n"
                        "            direction: output;\n"
                        "            function: \"0\";\n"
                        "        }\n"
                        "    }\n"
                        "    cell(VCC) {\n"
                        "        pin(O) {\n"
                        "            direction: output;\n"
                        "            function: \"1\";\n"
                        "        }\n"
                        "    }\n"
                        "}";

            test_lib.close();
        }
    };

    /**
     * Testing the access on a single Gate library via the get_gate_library function.
     *
     * Functions: get_gate_library, get_gate_libraries
     */
    TEST_F(GateLibraryManagerTest, check_get_gate_library)
    {
        TEST_START
        NO_COUT_TEST_BLOCK;
        create_test_lib();
        // Load the Gate library twice by its filename
        GateLibrary* test_lib_0 = gate_library_manager::get_gate_library(m_test_lib_path);
        GateLibrary* test_lib_1 = gate_library_manager::get_gate_library(m_test_lib_path);
        EXPECT_NE(test_lib_0, nullptr);
        EXPECT_NE(test_lib_1, nullptr);

        // Check that the test library can be found in the get_gate_libraries vector
        bool found_test_lib = false;
        for (GateLibrary* gl : gate_library_manager::get_gate_libraries())
        {
            if (gl->get_name() == "example_lib")
            {
                found_test_lib = true;
                break;
            }
        }
        EXPECT_TRUE(found_test_lib);
        TEST_END
    }

    /**
     * Testing the load_all function that loads all gate_libraries found in the directories got by
     * utils::get_gate_library_directories().
     *
     * Functions: get_gate_library, get_gate_libraries
     */
    TEST_F(GateLibraryManagerTest, check_load_all)
    {
        TEST_START
        // Check that load_all also loads the test Gate library
        NO_COUT_TEST_BLOCK;
        create_test_lib();
        gate_library_manager::load_all();

        // Check that the test library can be found in the get_gate_libraries vector
        bool found_test_lib = false;
        for (GateLibrary* gl : gate_library_manager::get_gate_libraries())
        {
            if (gl->get_name() == "example_lib")
            {
                found_test_lib = true;
                break;
            }
        }
        EXPECT_TRUE(found_test_lib);
        TEST_END
    }

    /**
     * Testing whether GND and VCC gate type are marked or even added to the gate library.
     *
     * Functions: get_gate_library, get_gate_libraries
     */
    TEST_F(GateLibraryManagerTest, check_prepare_library)
    {
        TEST_START
        {
            // Parse a file that does contain a GND or VCC Gate type (constant 0 / constant 1)
            NO_COUT_TEST_BLOCK;
            create_test_lib();
            GateLibrary* test_lib = gate_library_manager::get_gate_library(m_test_lib_path);
            ASSERT_NE(test_lib, nullptr);

            // check GND gate type
            auto gnd_types = test_lib->get_gnd_gate_types();
            ASSERT_TRUE(gnd_types.size() == 1);
            ASSERT_TRUE(gnd_types.find("GND") != gnd_types.end());
            EXPECT_TRUE(gnd_types.at("GND")->has_property(GateTypeProperty::ground));
            auto gnd_bf = gnd_types.at("GND")->get_boolean_functions();
            ASSERT_TRUE(gnd_bf.find("O") != gnd_bf.end());
            EXPECT_TRUE(gnd_bf.at("O").has_constant_value(0));

            // check VCC gate type
            auto vcc_types = test_lib->get_vcc_gate_types();
            ASSERT_TRUE(vcc_types.size() == 1);
            ASSERT_TRUE(vcc_types.find("VCC") != vcc_types.end());
            EXPECT_TRUE(vcc_types.at("VCC")->has_property(GateTypeProperty::power));
            auto vcc_bf = vcc_types.at("VCC")->get_boolean_functions();
            ASSERT_TRUE(vcc_bf.find("O") != vcc_bf.end());
            EXPECT_TRUE(vcc_bf.at("O").has_constant_value(1));
        }
        {
            // Parse a file that does not contain a GND or VCC Gate type (constant 0 / constant 1)
            NO_COUT_TEST_BLOCK;
            m_test_lib_path = (utils::get_gate_library_directories()[0]) / "test2.lib";
            std::ofstream test_lib(m_test_lib_path.string());
            test_lib << "/* This file only exists for testing purposes and should be already destroyed*/\n"
                        "library (check_prepare_library_1) {\n"
                        "    define(cell);\n"
                        "}";

            test_lib.close();
            GateLibrary* empty_lib = gate_library_manager::get_gate_library(m_test_lib_path);
            ASSERT_NE(empty_lib, nullptr);

            // check GND gate type
            auto gnd_types = empty_lib->get_gnd_gate_types();
            ASSERT_TRUE(gnd_types.size() == 1);
            ASSERT_TRUE(gnd_types.find("HAL_GND") != gnd_types.end());
            EXPECT_TRUE(gnd_types.at("HAL_GND")->has_property(GateTypeProperty::ground));
            auto gnd_bf = gnd_types.at("HAL_GND")->get_boolean_functions();
            ASSERT_TRUE(gnd_bf.find("O") != gnd_bf.end());
            EXPECT_TRUE(gnd_bf.at("O").has_constant_value(0));

            // check VCC gate type
            auto vcc_types = empty_lib->get_vcc_gate_types();
            ASSERT_TRUE(vcc_types.size() == 1);
            ASSERT_TRUE(vcc_types.find("HAL_VDD") != vcc_types.end());
            EXPECT_TRUE(vcc_types.at("HAL_VDD")->has_property(GateTypeProperty::power));
            auto vcc_bf = vcc_types.at("HAL_VDD")->get_boolean_functions();
            ASSERT_TRUE(vcc_bf.find("O") != vcc_bf.end());
            EXPECT_TRUE(vcc_bf.at("O").has_constant_value(1));
        }
        std::filesystem::remove(m_test_lib_path);
        TEST_END
    }

    /**
    * Testing the handling of various invalid inputs.
    *
    * Functions: get_gate_library, get_gate_libraries
    */
    TEST_F(GateLibraryManagerTest, check_invalid)
    {
        TEST_START
        {
            // The file path does not exist
            NO_COUT_TEST_BLOCK;
            GateLibrary* test_lib = gate_library_manager::get_gate_library("/non/existing/path.lib");
            EXPECT_EQ(test_lib, nullptr);
        }
        TEST_END
    }

    /**
     * Tests for netlists that reference cells from more than one gate library file.
     */
    class GateLibraryManagerMultiTest : public ::testing::Test
    {
    protected:
        std::filesystem::path m_lib_a_path;
        std::filesystem::path m_lib_b_path;
        std::filesystem::path m_netlist_path;

        virtual void SetUp()
        {
            NO_COUT_BLOCK;
            test_utils::init_log_channels();
            plugin_manager::load_all_plugins();

            const std::filesystem::path lib_dir = utils::get_gate_library_directories()[0];
            m_lib_a_path                        = lib_dir / "multi_test_stdcells.lib";
            m_lib_b_path                        = lib_dir / "multi_test_macros.lib";
            m_netlist_path                      = lib_dir / "multi_test_netlist.v";

            // the standard cells: a buffer plus constants, and a cell that both libraries define
            std::ofstream lib_a(m_lib_a_path.string());
            lib_a << "/* only exists for testing purposes and is destroyed afterwards */\n"
                     "library (multi_test_a) {\n"
                     "    define(cell);\n"
                     "    cell(GND) {\n"
                     "        pin(O) { direction: output; function: \"0\"; }\n"
                     "    }\n"
                     "    cell(VCC) {\n"
                     "        pin(O) { direction: output; function: \"1\"; }\n"
                     "    }\n"
                     "    cell(BUF_A) {\n"
                     "        pin(I) { direction: input; }\n"
                     "        pin(O) { direction: output; function: \"I\"; }\n"
                     "    }\n"
                     "    cell(SHARED) {\n"
                     "        pin(FROM_A) { direction: input; }\n"
                     "        pin(O) { direction: output; function: \"FROM_A\"; }\n"
                     "    }\n"
                     "}";
            lib_a.close();

            // the macros: a cell only this library defines, and a different definition of the shared cell
            std::ofstream lib_b(m_lib_b_path.string());
            lib_b << "/* only exists for testing purposes and is destroyed afterwards */\n"
                     "library (multi_test_b) {\n"
                     "    define(cell);\n"
                     "    cell(MACRO_B) {\n"
                     "        pin(I) { direction: input; }\n"
                     "        pin(O) { direction: output; function: \"I\"; }\n"
                     "    }\n"
                     "    cell(SHARED) {\n"
                     "        pin(FROM_B) { direction: input; }\n"
                     "        pin(O) { direction: output; function: \"FROM_B\"; }\n"
                     "    }\n"
                     "}";
            lib_b.close();

            // a netlist that mixes cells of both libraries, which neither of them can be loaded with on its own
            std::ofstream netlist(m_netlist_path.string());
            netlist << "module top (net_in, net_out);\n"
                       "  input net_in;\n"
                       "  output net_out;\n"
                       "  wire net_0;\n"
                       "BUF_A gate_a ( .I (net_in), .O (net_0) );\n"
                       "MACRO_B gate_b ( .I (net_0), .O (net_out) );\n"
                       "endmodule";
            netlist.close();
        }

        virtual void TearDown()
        {
            NO_COUT_BLOCK;

            // the gate library manager outlives a single test, so the fixture libraries are dropped again rather than
            // left behind for whatever test runs next to match a netlist against
            for (const auto& key : m_loaded_lib_keys)
            {
                gate_library_manager::remove(key);
            }
            gate_library_manager::remove(std::filesystem::absolute(m_lib_a_path));
            gate_library_manager::remove(std::filesystem::absolute(m_lib_b_path));

            std::filesystem::remove(m_lib_a_path);
            std::filesystem::remove(m_lib_b_path);
            std::filesystem::remove(m_netlist_path);
            plugin_manager::unload_all_plugins();
        }

        /**
         * Remember a composite gate library so that it is dropped from the manager again, as its key is not a file
         * path that the fixture knows in advance.
         */
        void remember_composite(const GateLibrary* composite)
        {
            if (composite != nullptr)
            {
                m_loaded_lib_keys.push_back(composite->get_path());
            }
        }

    private:
        std::vector<std::filesystem::path> m_loaded_lib_keys;
    };

    /**
     * Testing that multiple gate library files can be loaded into a single composite gate library and that the
     * order of the files decides which definition of a gate type wins.
     *
     * Functions: load_multiple
     */
    TEST_F(GateLibraryManagerMultiTest, check_load_multiple)
    {
        TEST_START
        {
            NO_COUT_TEST_BLOCK;

            GateLibrary* composite = gate_library_manager::load_multiple({m_lib_a_path, m_lib_b_path});
            ASSERT_NE(composite, nullptr);
            remember_composite(composite);

            // the composite library knows the gate types of both files
            EXPECT_TRUE(composite->contains_gate_type_by_name("BUF_A"));
            EXPECT_TRUE(composite->contains_gate_type_by_name("MACRO_B"));
            EXPECT_TRUE(composite->contains_gate_type_by_name("SHARED"));

            // the gate library that comes first wins the name collision
            GateType* shared = composite->get_gate_type_by_name("SHARED");
            ASSERT_NE(shared, nullptr);
            EXPECT_NE(shared->get_pin_by_name("FROM_A"), nullptr);
            EXPECT_EQ(shared->get_pin_by_name("FROM_B"), nullptr);

            // provenance is the ordered list of files it was assembled from
            EXPECT_TRUE(composite->is_composite());
            ASSERT_EQ(composite->get_source_paths().size(), 2);
            EXPECT_EQ(composite->get_source_paths().at(0), std::filesystem::absolute(m_lib_a_path));
            EXPECT_EQ(composite->get_source_paths().at(1), std::filesystem::absolute(m_lib_b_path));

            // GND and VCC of the first library are carried over rather than auto-generated
            EXPECT_TRUE(composite->get_gnd_gate_types().find("GND") != composite->get_gnd_gate_types().end());
            EXPECT_TRUE(composite->get_vcc_gate_types().find("VCC") != composite->get_vcc_gate_types().end());

            // loading the same ordered list again does not build a second library
            EXPECT_EQ(gate_library_manager::load_multiple({m_lib_a_path, m_lib_b_path}), composite);

            // the individual libraries are untouched by the composition
            GateLibrary* lib_a = gate_library_manager::get_gate_library(m_lib_a_path.string());
            ASSERT_NE(lib_a, nullptr);
            EXPECT_NE(lib_a, composite);
            EXPECT_TRUE(lib_a->contains_gate_type_by_name("BUF_A"));
            EXPECT_FALSE(lib_a->contains_gate_type_by_name("MACRO_B"));
            EXPECT_FALSE(lib_a->is_composite());
        }
        {
            NO_COUT_TEST_BLOCK;

            // the other order yields a different library in which the other definition wins
            GateLibrary* composite = gate_library_manager::load_multiple({m_lib_b_path, m_lib_a_path});
            ASSERT_NE(composite, nullptr);
            remember_composite(composite);

            GateType* shared = composite->get_gate_type_by_name("SHARED");
            ASSERT_NE(shared, nullptr);
            EXPECT_NE(shared->get_pin_by_name("FROM_B"), nullptr);
            EXPECT_EQ(shared->get_pin_by_name("FROM_A"), nullptr);
        }
        {
            NO_COUT_TEST_BLOCK;

            // a list of one is just a plain gate library
            GateLibrary* single = gate_library_manager::load_multiple({m_lib_a_path});
            ASSERT_NE(single, nullptr);
            EXPECT_FALSE(single->is_composite());

            // invalid input
            EXPECT_EQ(gate_library_manager::load_multiple({}), nullptr);
            EXPECT_EQ(gate_library_manager::load_multiple({m_lib_a_path, "/non/existing/path.lib"}), nullptr);
        }
        TEST_END
    }

    /**
     * Testing the resolution of an ordered gate library search list into gate library files.
     *
     * Functions: resolve_search_list, split_search_list
     */
    TEST_F(GateLibraryManagerMultiTest, check_resolve_search_list)
    {
        TEST_START
        {
            NO_COUT_TEST_BLOCK;

            // paths are taken as is, in the order given, and duplicates are dropped
            const auto resolved = gate_library_manager::resolve_search_list({m_lib_b_path.string(), m_lib_a_path.string(), m_lib_b_path.string()});
            ASSERT_EQ(resolved.size(), 2);
            EXPECT_EQ(resolved.at(0), std::filesystem::absolute(m_lib_b_path));
            EXPECT_EQ(resolved.at(1), std::filesystem::absolute(m_lib_a_path));
        }
        {
            NO_COUT_TEST_BLOCK;

            // bare file names are looked up in the default gate library directories
            const auto resolved = gate_library_manager::resolve_search_list({"multi_test_macros.lib"});
            ASSERT_EQ(resolved.size(), 1);
            EXPECT_EQ(resolved.at(0), std::filesystem::absolute(m_lib_b_path));
        }
        {
            NO_COUT_TEST_BLOCK;

            // a directory is expanded into the gate library files it contains, and the netlist file is not one
            const auto resolved = gate_library_manager::resolve_search_list({utils::get_gate_library_directories()[0].string()});
            EXPECT_TRUE(std::find(resolved.begin(), resolved.end(), std::filesystem::absolute(m_lib_a_path)) != resolved.end());
            EXPECT_TRUE(std::find(resolved.begin(), resolved.end(), std::filesystem::absolute(m_lib_b_path)) != resolved.end());
            EXPECT_TRUE(std::find(resolved.begin(), resolved.end(), std::filesystem::absolute(m_netlist_path)) == resolved.end());
        }
        {
            NO_COUT_TEST_BLOCK;

            // entries that cannot be resolved are skipped
            EXPECT_TRUE(gate_library_manager::resolve_search_list({"definitely_not_a_gate_library.lib"}).empty());
            EXPECT_TRUE(gate_library_manager::resolve_search_list({}).empty());
        }
        {
            // a search list given as a single string is split on commas, ignoring surrounding whitespace
            EXPECT_EQ(gate_library_manager::split_search_list("a.lib, b.hgl ,, c.lib"), std::vector<std::string>({"a.lib", "b.hgl", "c.lib"}));
            EXPECT_TRUE(gate_library_manager::split_search_list("  ").empty());
        }
        TEST_END
    }

    /**
     * Testing that a netlist referencing cells from two gate library files can be loaded against both of them.
     *
     * Functions: load_netlist
     */
    TEST_F(GateLibraryManagerMultiTest, check_netlist_with_multiple_gate_libraries)
    {
        TEST_START
        if (netlist_parser_manager::can_parse(m_netlist_path))
        {
            {
                NO_COUT_TEST_BLOCK;

                // neither library on its own can instantiate the netlist
                EXPECT_EQ(netlist_factory::load_netlist(m_netlist_path, std::vector<std::string>({m_lib_a_path.string()})), nullptr);
                EXPECT_EQ(netlist_factory::load_netlist(m_netlist_path, std::vector<std::string>({m_lib_b_path.string()})), nullptr);
            }
            {
                NO_COUT_TEST_BLOCK;

                // both of them together can
                std::unique_ptr<Netlist> nl = netlist_factory::load_netlist(m_netlist_path, std::vector<std::string>({m_lib_a_path.string(), m_lib_b_path.string()}));
                ASSERT_NE(nl, nullptr);
                remember_composite(nl->get_gate_library());
                ASSERT_EQ(nl->get_gates().size(), 2);

                const auto gates_a = nl->get_gates(test_utils::gate_type_filter("BUF_A"));
                const auto gates_b = nl->get_gates(test_utils::gate_type_filter("MACRO_B"));
                ASSERT_EQ(gates_a.size(), 1);
                ASSERT_EQ(gates_b.size(), 1);

                // the gate from the second library is driven by the gate from the first one
                Net* net_0 = gates_a.at(0)->get_fan_out_net("O");
                ASSERT_NE(net_0, nullptr);
                EXPECT_EQ(gates_b.at(0)->get_fan_in_net("I"), net_0);
            }
        }
        TEST_END
    }
}    //namespace hal
