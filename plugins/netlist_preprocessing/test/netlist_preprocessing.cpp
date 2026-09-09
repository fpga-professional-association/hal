#include "netlist_preprocessing/netlist_preprocessing.h"

#include "hal_core/netlist/boolean_function/solver.h"
#include "netlist_test_utils.h"
#include "gate_library_test_utils.h"

namespace hal {

    class NetlistPreprocessingTest : public ::testing::Test {
    protected:
        virtual void SetUp() 
        {
            NO_COUT_BLOCK;
            test_utils::init_log_channels();
            test_utils::create_sandbox_directory();
        }

        virtual void TearDown() 
        {
            test_utils::remove_sandbox_directory();
        }
    };

    /**
     * Test the deletion of LUT fan-in endpoints that are not present within the LUT's Boolean function.
     *
     * Functions: remove_unused_lut_inputs
     */
    TEST_F(NetlistPreprocessingTest, check_remove_unused_lut_inputs) 
    {
        TEST_START
        {
            std::unique_ptr<Netlist> nl = test_utils::create_empty_netlist();
            ASSERT_NE(nl, nullptr);
            const GateLibrary* gl = nl->get_gate_library();
            ASSERT_NE(gl, nullptr);

            Gate* gnd_gate = nl->create_gate(gl->get_gate_type_by_name("GND"), "gnd");
            nl->mark_gnd_gate(gnd_gate);
            Net* gnd_net = nl->create_net("gnd");
            gnd_net->add_source(gnd_gate, "O");

            GateType* lut4 = gl->get_gate_type_by_name("LUT4");

            Gate* l0 = nl->create_gate(lut4, "l0");
            Gate* l1 = nl->create_gate(lut4, "l1");
            Gate* l2 = nl->create_gate(lut4, "l2");
            Gate* l3 = nl->create_gate(lut4, "l3");
            Gate* l4 = nl->create_gate(lut4, "l4");
            Gate* l5 = nl->create_gate(lut4, "l5");
            l4->add_boolean_function("O", BooleanFunction::from_string("I0 & I1 & I2 & I3").get());
            l5->add_boolean_function("O", BooleanFunction::Var("I2"));

            test_utils::connect(nl.get(), l0, "O", l4, "I0");
            test_utils::connect(nl.get(), l1, "O", l4, "I1");
            test_utils::connect(nl.get(), l2, "O", l4, "I2");
            test_utils::connect(nl.get(), l3, "O", l4, "I3");

            test_utils::connect(nl.get(), l0, "O", l5, "I0");
            test_utils::connect(nl.get(), l1, "O", l5, "I1");
            test_utils::connect(nl.get(), l2, "O", l5, "I2");
            test_utils::connect(nl.get(), l3, "O", l5, "I3");

            EXPECT_EQ(l4->get_predecessor("I0")->get_gate(), l0);
            EXPECT_EQ(l4->get_predecessor("I1")->get_gate(), l1);
            EXPECT_EQ(l4->get_predecessor("I2")->get_gate(), l2);
            EXPECT_EQ(l4->get_predecessor("I3")->get_gate(), l3);

            EXPECT_EQ(l5->get_predecessor("I0")->get_gate(), l0);
            EXPECT_EQ(l5->get_predecessor("I1")->get_gate(), l1);
            EXPECT_EQ(l5->get_predecessor("I2")->get_gate(), l2);
            EXPECT_EQ(l5->get_predecessor("I3")->get_gate(), l3);

            auto res = netlist_preprocessing::remove_unused_lut_inputs(nl.get());
            ASSERT_TRUE(res.is_ok());
            EXPECT_EQ(res.get(), 3);

            EXPECT_EQ(l4->get_predecessor("I0")->get_gate(), l0);
            EXPECT_EQ(l4->get_predecessor("I1")->get_gate(), l1);
            EXPECT_EQ(l4->get_predecessor("I2")->get_gate(), l2);
            EXPECT_EQ(l4->get_predecessor("I3")->get_gate(), l3);

            EXPECT_EQ(l5->get_predecessor("I0")->get_gate(), gnd_gate);
            EXPECT_EQ(l5->get_predecessor("I1")->get_gate(), gnd_gate);
            EXPECT_EQ(l5->get_predecessor("I2")->get_gate(), l2);
            EXPECT_EQ(l5->get_predecessor("I3")->get_gate(), gnd_gate);
        }
        TEST_END
    }

    /**
     * Test the deletion of buffer gates.
     *
     * Functions: remove_buffers
     */
    TEST_F(NetlistPreprocessingTest, check_remove_buffers)
    {
        TEST_START
        {
            std::unique_ptr<Netlist> nl = test_utils::create_empty_netlist();
            ASSERT_NE(nl, nullptr);
            const GateLibrary* gl = nl->get_gate_library();
            ASSERT_NE(gl, nullptr);

            Gate* gnd_gate = nl->create_gate(gl->get_gate_type_by_name("GND"), "gnd");
            nl->mark_gnd_gate(gnd_gate);
            Net* gnd_net = nl->create_net("gnd");
            gnd_net->add_source(gnd_gate, "O");
            Gate* vcc_gate = nl->create_gate(gl->get_gate_type_by_name("VCC"), "vcc");
            nl->mark_vcc_gate(vcc_gate);
            Net* vcc_net = nl->create_net("vcc");
            vcc_net->add_source(vcc_gate, "O");

            Gate* g0 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "g0");
            Gate* g1 = nl->create_gate(gl->get_gate_type_by_name("BUF"), "g1");
            Gate* g2 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "g2");

            Net* n0 = nl->create_net("n0");
            n0->add_destination(g0, "I0");
            n0->mark_global_input_net();

            Net* n1 = nl->create_net("n1");
            n1->add_destination(g0, "I1");
            n1->mark_global_input_net();

            Net* n2 = nl->create_net("n2");
            n2->add_destination(g2, "I1");
            n2->mark_global_input_net();

            Net* n3 = test_utils::connect(nl.get(), g0, "O", g1, "I");
            Net* n4 = test_utils::connect(nl.get(), g1, "O", g2, "I0");

            auto res = netlist_preprocessing::remove_buffers(nl.get());
            ASSERT_TRUE(res.is_ok());
            EXPECT_EQ(res.get(), 1);

            ASSERT_EQ(nl->get_gates().size(), 4);
            ASSERT_EQ(nl->get_nets().size(), 6);

            EXPECT_EQ(g0->get_successor("O")->get_gate(), g2);
        }
        {
            std::unique_ptr<Netlist> nl = test_utils::create_empty_netlist();
            ASSERT_NE(nl, nullptr);
            const GateLibrary* gl = nl->get_gate_library();
            ASSERT_NE(gl, nullptr);

            Gate* gnd_gate = nl->create_gate(gl->get_gate_type_by_name("GND"), "gnd");
            nl->mark_gnd_gate(gnd_gate);
            Net* gnd_net = nl->create_net("gnd");
            gnd_net->add_source(gnd_gate, "O");
            Gate* vcc_gate = nl->create_gate(gl->get_gate_type_by_name("VCC"), "vcc");
            nl->mark_vcc_gate(vcc_gate);
            Net* vcc_net = nl->create_net("vcc");
            vcc_net->add_source(vcc_gate, "O");

            Gate* g0 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "g0");
            Gate* g1 = nl->create_gate(gl->get_gate_type_by_name("LUT2"), "g1");
            g1->add_boolean_function("O", BooleanFunction::Var("I1"));
            Gate* g2 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "g2");

            Net* n0 = nl->create_net("n0");
            n0->add_destination(g0, "I0");
            n0->mark_global_input_net();

            Net* n1 = nl->create_net("n1");
            n1->add_destination(g0, "I1");
            n1->mark_global_input_net();

            Net* n2 = nl->create_net("n2");
            n2->add_destination(g2, "I1");
            n2->mark_global_input_net();

            gnd_net->add_destination(g1, "I0");

            Net* n3 = test_utils::connect(nl.get(), g0, "O", g1, "I1");
            Net* n4 = test_utils::connect(nl.get(), g1, "O", g2, "I0");

            auto res = netlist_preprocessing::remove_buffers(nl.get());
            ASSERT_TRUE(res.is_ok());
            EXPECT_EQ(res.get(), 1);

            ASSERT_EQ(nl->get_gates().size(), 4);
            ASSERT_EQ(nl->get_nets().size(), 6);

            EXPECT_EQ(g0->get_successor("O")->get_gate(), g2);
        }
        {
            std::unique_ptr<Netlist> nl = test_utils::create_empty_netlist();
            ASSERT_NE(nl, nullptr);
            const GateLibrary* gl       = nl->get_gate_library();
            ASSERT_NE(gl, nullptr);

            Gate* gnd_gate = nl->create_gate(gl->get_gate_type_by_name("GND"), "gnd");
            nl->mark_gnd_gate(gnd_gate);
            Net* gnd_net = nl->create_net("gnd");
            gnd_net->add_source(gnd_gate, "O");
            Gate* vcc_gate = nl->create_gate(gl->get_gate_type_by_name("VCC"), "vcc");
            nl->mark_vcc_gate(vcc_gate);
            Net* vcc_net = nl->create_net("vcc");
            vcc_net->add_source(vcc_gate, "O");

            Gate* g0 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "g0");
            Gate* g1 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "g1");
            Gate* g2 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "g2");

            Net* n0 = nl->create_net("n0");
            n0->add_destination(g0, "I0");
            n0->mark_global_input_net();

            Net* n1 = nl->create_net("n1");
            n1->add_destination(g0, "I1");
            n1->mark_global_input_net();

            Net* n2 = nl->create_net("n2");
            n2->add_destination(g2, "I1");
            n2->mark_global_input_net();

            vcc_net->add_destination(g1, "I0");

            Net* n3 = test_utils::connect(nl.get(), g0, "O", g1, "I1");
            Net* n4 = test_utils::connect(nl.get(), g1, "O", g2, "I0");

            auto res = netlist_preprocessing::remove_buffers(nl.get());
            ASSERT_TRUE(res.is_ok());
            EXPECT_EQ(res.get(), 1);

            ASSERT_EQ(nl->get_gates().size(), 4);
            ASSERT_EQ(nl->get_nets().size(), 6);

            EXPECT_EQ(g0->get_successor("O")->get_gate(), g2);
        }

        TEST_END
    }

    /**
     * Test the deletion of redundant logic gates.
     *
     * Functions: remove_redundant_gates
     */
    TEST_F(NetlistPreprocessingTest, check_remove_redundant_gates)
    {
        TEST_START
        {
            std::unique_ptr<Netlist> nl = test_utils::create_empty_netlist();
            ASSERT_NE(nl, nullptr);
            const GateLibrary* gl = nl->get_gate_library();
            ASSERT_NE(gl, nullptr);

            Gate* g0 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "g0");
            Gate* g1 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "g1");
            Gate* g2 = nl->create_gate(gl->get_gate_type_by_name("DFF"), "g2");
            Gate* g3 = nl->create_gate(gl->get_gate_type_by_name("DFF"), "g3");
            Gate* g4 = nl->create_gate(gl->get_gate_type_by_name("XOR2"), "g4");

            Net* n0 = nl->create_net("n0");
            n0->add_destination(g0, "I0");
            n0->add_destination(g1, "I0");
            n0->mark_global_input_net();

            Net* n1 = nl->create_net("n1");
            n1->add_destination(g0, "I1");
            n1->add_destination(g1, "I1");
            n1->mark_global_input_net();

            Net* n2 = nl->create_net("clk");
            n2->add_destination(g2, "CLK");
            n2->add_destination(g3, "CLK");
            n2->mark_global_input_net();

            Net* n3 = test_utils::connect(nl.get(), g0, "O", g2, "D");
            Net* n4 = test_utils::connect(nl.get(), g1, "O", g3, "D");
            
            Net* n5 = test_utils::connect(nl.get(), g2, "Q", g4, "I0");
            Net* n6 = test_utils::connect(nl.get(), g3, "QN", g4, "I1");

            auto res = netlist_preprocessing::remove_redundant_gates(nl.get());
            ASSERT_TRUE(res.is_ok());
            EXPECT_EQ(res.get(), 2);

            ASSERT_EQ(nl->get_gates().size(), 3);
            ASSERT_EQ(nl->get_nets().size(), 6);

            auto and2_gates = nl->get_gates([](const auto* g){ return g->get_type()->get_name() == "AND2"; });
            ASSERT_EQ(and2_gates.size(), 1);
            auto and2 = and2_gates.front();
            auto dff_gates = nl->get_gates([](const auto* g){ return g->get_type()->get_name() == "DFF"; });
            ASSERT_EQ(dff_gates.size(), 1);
            auto dff = dff_gates.front();
            auto xor2_gates = nl->get_gates([](const auto* g){ return g->get_type()->get_name() == "XOR2"; });
            ASSERT_EQ(xor2_gates.size(), 1);
            auto xor2 = xor2_gates.front();

            auto and2_suc = and2->get_successor("O");
            ASSERT_NE(and2_suc, nullptr);
            EXPECT_EQ(and2_suc->get_gate(), dff);
            EXPECT_EQ(and2_suc->get_pin()->get_name(), "D");

            auto dff_suc_0 = dff->get_successor("Q");
            ASSERT_NE(dff_suc_0, nullptr);
            EXPECT_EQ(dff_suc_0->get_gate(), xor2);
            EXPECT_EQ(dff_suc_0->get_pin()->get_name(), "I0");

            auto dff_suc_1 = dff->get_successor("QN");
            ASSERT_NE(dff_suc_1, nullptr);
            EXPECT_EQ(dff_suc_1->get_gate(), xor2);
            EXPECT_EQ(dff_suc_1->get_pin()->get_name(), "I1");
        }
        TEST_END
    }

    /**
     * Test that two flip-flops which differ only in the value they start out at are not treated as
     * duplicates of one another.
     *
     * Functions: remove_redundant_gates
     */
    TEST_F(NetlistPreprocessingTest, check_remove_redundant_gates_keeps_differing_initial_values)
    {
        TEST_START
        {
            // Two flip-flops of the same type, driven by the same clock and the same data net, so that
            // everything the fan-in can tell them apart by is identical.
            const auto build = [](const std::string& init_0, const std::string& init_1) {
                std::unique_ptr<Netlist> nl = test_utils::create_empty_netlist();
                const GateLibrary* gl       = nl->get_gate_library();

                Gate* ff_0 = nl->create_gate(gl->get_gate_type_by_name("DFF"), "ff_0");
                Gate* ff_1 = nl->create_gate(gl->get_gate_type_by_name("DFF"), "ff_1");

                Net* clk = nl->create_net("clk");
                clk->add_destination(ff_0, "CLK");
                clk->add_destination(ff_1, "CLK");
                clk->mark_global_input_net();

                Net* data = nl->create_net("data");
                data->add_destination(ff_0, "D");
                data->add_destination(ff_1, "D");
                data->mark_global_input_net();

                ff_0->set_data("generic", "INIT", "bit_vector", init_0);
                ff_1->set_data("generic", "INIT", "bit_vector", init_1);
                return nl;
            };

            {
                // The same initial value: the two really are interchangeable and one may go.
                std::unique_ptr<Netlist> nl = build("0", "0");
                ASSERT_NE(nl, nullptr);

                auto res = netlist_preprocessing::remove_redundant_gates(nl.get());
                ASSERT_TRUE(res.is_ok());
                EXPECT_EQ(res.get(), 1);
                EXPECT_EQ(nl->get_gates().size(), 1);
            }
            {
                // Different initial values: one starts at 0 and the other at 1, so neither can stand in
                // for the other and both have to survive.
                std::unique_ptr<Netlist> nl = build("0", "1");
                ASSERT_NE(nl, nullptr);

                auto res = netlist_preprocessing::remove_redundant_gates(nl.get());
                ASSERT_TRUE(res.is_ok());
                EXPECT_EQ(res.get(), 0);
                EXPECT_EQ(nl->get_gates().size(), 2);
            }
        }
        TEST_END
    }

    /**
     * check_remove_redundant_gates proves two combinational gates equivalent through an SMT solver, and
     * remove_redundant_gates treats a solver that fails to answer as "not equivalent" -- so a missing
     * solver does not fail loudly, it silently removes nothing. That is exactly how it presented itself
     * in a container whose dependencies were installed by the HAL_DOCKER branch of
     * install_dependencies.sh, which used to install libz3-dev but not the z3 binary the default
     * QueryConfig shells out to (see issue #30).
     *
     * This test states the prerequisite instead of leaving the next person to rediscover it: if it
     * fails, the environment lacks a working SMT solver and every equivalence-based check in this suite
     * is meaningless.
     *
     * Functions: SMT::Solver::query
     */
    TEST_F(NetlistPreprocessingTest, check_smt_solver_is_available)
    {
        TEST_START
        {
            // the configuration remove_redundant_gates queries with
            auto config = SMT::QueryConfig();
#ifdef BITWUZLA_LIBRARY
            config = config.with_solver(SMT::SolverType::Bitwuzla).with_call(SMT::SolverCall::Library);
#endif

            ASSERT_TRUE(SMT::Solver::has_local_solver_for(config.solver, config.call))
                << "no local SMT solver available -- install the 'z3' package (install_dependencies.sh), not just libz3-dev";

            // 'a != a' is unsatisfiable, which is the shape of query remove_redundant_gates uses to
            // decide that two gates compute the same function.
            auto eq_res = BooleanFunction::Eq(BooleanFunction::Var("a", 1), BooleanFunction::Var("a", 1), 1);
            ASSERT_TRUE(eq_res.is_ok());
            auto unsat_res = BooleanFunction::Not(eq_res.get(), 1);
            ASSERT_TRUE(unsat_res.is_ok());

            auto res = SMT::Solver({SMT::Constraint(unsat_res.get())}).query(config);
            ASSERT_TRUE(res.is_ok()) << "SMT query failed: " << res.get_error().get();
            EXPECT_TRUE(res.get().is_unsat());
        }
        TEST_END
    }

    /**
     * The resynthesis plugin is an optional build dependency: with -DPL_RESYNTHESIS=OFF the
     * netlist_preprocessing plugin is built without it (see issue #38). The API stays the same in both
     * configurations, so this test pins down what each one promises -- the reduced build has to report
     * a usable error rather than silently doing nothing or not compiling at all.
     *
     * Functions: manual_mux_optimizations
     */
    TEST_F(NetlistPreprocessingTest, check_manual_mux_optimizations_resynthesis_dependency)
    {
        TEST_START
        {
            std::unique_ptr<Netlist> nl = test_utils::create_empty_netlist();
            ASSERT_NE(nl, nullptr);
            // manual_mux_optimizations writes the library out as a genlib file, hence the non-const handle
            GateLibrary* gl = const_cast<GateLibrary*>(nl->get_gate_library());
            ASSERT_NE(gl, nullptr);

            // arguments are validated in both configurations
            EXPECT_TRUE(netlist_preprocessing::manual_mux_optimizations(nl.get(), nullptr).is_error());
            EXPECT_TRUE(netlist_preprocessing::manual_mux_optimizations(nullptr, gl).is_error());

            auto res = netlist_preprocessing::manual_mux_optimizations(nl.get(), gl);
#ifdef HAL_WITH_RESYNTHESIS
            // The call reaches the real implementation. Whether it gets all the way through depends on
            // the gate library being writable as genlib and on the external tools resynthesis drives,
            // neither of which this test is about -- what it pins down is that the feature is there.
            if (res.is_error())
            {
                EXPECT_EQ(res.get_error().get().find("built without resynthesis support"), std::string::npos);
            }
#else
            ASSERT_TRUE(res.is_error());
            EXPECT_NE(res.get_error().get().find("built without resynthesis support"), std::string::npos);
#endif
        }
        TEST_END
    }
} // namespace hal
