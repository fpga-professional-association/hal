#include "gate_library_test_utils.h"
#include "graph_algorithm/algorithms/components.h"
#include "graph_algorithm/netlist_graph.h"
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
     * Tests of the netlist graph and of the connected-component analysis running on it.
     *
     * The netlists are built in-test from the example gate library so that the expected components can be
     * stated by hand: every feedback path in them is deliberate, which is what makes a strongly connected
     * component of a given size the correct answer rather than an observed one.
     */
    class GraphAlgorithmTest : public ::testing::Test
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
         * Resolve the vertices of every component to gate names.
         *
         * Vertex numbering follows the order in which the netlist hands out its gates, so it is not part of
         * the contract under test; the names are. The inner sets are sorted by name and the outer vector by
         * size and then content, which makes two runs comparable irrespective of the order igraph reports
         * the components in.
         */
        std::vector<std::set<std::string>> components_as_names(const graph_algorithm::NetlistGraph* graph, const std::vector<std::vector<u32>>& components)
        {
            std::vector<std::set<std::string>> res;
            for (const auto& component : components)
            {
                auto gates = graph->get_gates_from_vertices(component);
                if (gates.is_error())
                {
                    return {};
                }

                std::set<std::string> names;
                for (const auto* g : gates.get())
                {
                    names.insert(g == nullptr ? "<dummy>" : g->get_name());
                }
                res.push_back(names);
            }

            std::sort(res.begin(), res.end(), [](const std::set<std::string>& a, const std::set<std::string>& b) { return a.size() != b.size() ? a.size() < b.size() : a < b; });
            return res;
        }

        std::vector<u32> component_sizes(const std::vector<std::vector<u32>>& components)
        {
            std::vector<u32> sizes;
            for (const auto& component : components)
            {
                sizes.push_back(component.size());
            }
            std::sort(sizes.begin(), sizes.end());
            return sizes;
        }

        /**
         * A 3-bit ripple counter: each bit is a flip-flop whose inverted output is fed back to its own data
         * input, and each bit clocks the next one. The feedback loops are per-bit and the ripple connection
         * only ever points forward, so the toggle loops must not merge into a larger component.
         *
         * The `and_tap` gate reads two of the counter outputs and drives nothing that returns, so it is a
         * component of size one and is what the `min_size` filter has to remove.
         */
        std::unique_ptr<Netlist> build_ripple_counter()
        {
            auto nl               = test_utils::create_empty_netlist();
            const GateLibrary* gl = nl->get_gate_library();

            Net* clk = nl->create_net("clk");
            clk->mark_global_input_net();

            std::vector<Net*> outputs;
            for (u32 i = 0; i < 3; i++)
            {
                const std::string idx = std::to_string(i);
                Gate* ff              = nl->create_gate(gl->get_gate_type_by_name("DFF"), "ff_" + idx);
                Gate* inv             = nl->create_gate(gl->get_gate_type_by_name("INV"), "inv_" + idx);

                Net* q = test_utils::connect(nl.get(), ff, "Q", inv, "I", "q_" + idx);
                test_utils::connect(nl.get(), inv, "O", ff, "D", "d_" + idx);
                outputs.push_back(q);

                if (i == 0)
                {
                    clk->add_destination(ff, "CLK");
                }
                else
                {
                    outputs.at(i - 1)->add_destination(ff, "CLK");
                }
            }

            Gate* tap = nl->create_gate(gl->get_gate_type_by_name("AND2"), "and_tap");
            outputs.at(0)->add_destination(tap, "I0");
            outputs.at(1)->add_destination(tap, "I1");
            test_utils::connect_global_out(nl.get(), tap, "O", "tap_out");

            return nl;
        }

        /**
         * A shift register of `num_flip_flops` flip-flops whose last output is inverted back into the first
         * data input, i.e., a ring counter. Every gate lies on the one cycle, so the whole ring is a single
         * strongly connected component of `num_flip_flops + 1` gates.
         */
        std::unique_ptr<Netlist> build_ring(u32 num_flip_flops)
        {
            auto nl               = test_utils::create_empty_netlist();
            const GateLibrary* gl = nl->get_gate_library();

            Net* clk = nl->create_net("clk");
            clk->mark_global_input_net();

            std::vector<Gate*> ffs;
            for (u32 i = 0; i < num_flip_flops; i++)
            {
                Gate* ff = nl->create_gate(gl->get_gate_type_by_name("DFF"), "ff_" + std::to_string(i));
                clk->add_destination(ff, "CLK");
                ffs.push_back(ff);
            }

            for (u32 i = 0; i + 1 < num_flip_flops; i++)
            {
                test_utils::connect(nl.get(), ffs.at(i), "Q", ffs.at(i + 1), "D", "stage_" + std::to_string(i));
            }

            Gate* inv = nl->create_gate(gl->get_gate_type_by_name("INV"), "feedback_inv");
            test_utils::connect(nl.get(), ffs.back(), "Q", inv, "I", "ring_out");
            test_utils::connect(nl.get(), inv, "O", ffs.front(), "D", "ring_feedback");

            return nl;
        }

        /**
         * A purely combinational netlist: two inverters feeding an AND gate. Nothing feeds back, so no
         * strongly connected component can hold more than one gate.
         */
        std::unique_ptr<Netlist> build_combinational()
        {
            auto nl               = test_utils::create_empty_netlist();
            const GateLibrary* gl = nl->get_gate_library();

            Gate* inv_0 = nl->create_gate(gl->get_gate_type_by_name("INV"), "inv_0");
            Gate* inv_1 = nl->create_gate(gl->get_gate_type_by_name("INV"), "inv_1");
            Gate* and_0 = nl->create_gate(gl->get_gate_type_by_name("AND2"), "and_0");

            test_utils::connect_global_in(nl.get(), inv_0, "I", "in_0");
            test_utils::connect_global_in(nl.get(), inv_1, "I", "in_1");
            test_utils::connect(nl.get(), inv_0, "O", and_0, "I0", "n_0");
            test_utils::connect(nl.get(), inv_1, "O", and_0, "I1", "n_1");
            test_utils::connect_global_out(nl.get(), and_0, "O", "out");

            return nl;
        }
    };

    /**
     * A graph built from a netlist has one vertex per gate and one edge per source/destination pair of every
     * net, and no dummy vertices unless they were asked for.
     *
     * Functions: from_netlist
     */
    TEST_F(GraphAlgorithmTest, check_from_netlist)
    {
        TEST_START
        {
            auto nl = build_ripple_counter();
            ASSERT_NE(nl, nullptr);

            auto graph_res = graph_algorithm::NetlistGraph::from_netlist(nl.get());
            ASSERT_TRUE(graph_res.is_ok());
            auto graph = graph_res.get();

            EXPECT_EQ(graph->get_netlist(), nl.get());
            EXPECT_EQ(graph->get_num_vertices(), nl->get_gates().size());

            // three toggle loops of two edges each, two ripple edges, and two edges into the tap gate
            EXPECT_EQ(graph->get_num_edges(), 10);
        }
        {
            // the clock and the counter outputs have no driver resp. no reader in the netlist, so asking for
            // dummy vertices adds vertices that do not resolve to a gate
            auto nl = build_ripple_counter();
            ASSERT_NE(nl, nullptr);

            auto plain_res = graph_algorithm::NetlistGraph::from_netlist(nl.get(), false);
            ASSERT_TRUE(plain_res.is_ok());
            auto dummy_res = graph_algorithm::NetlistGraph::from_netlist(nl.get(), true);
            ASSERT_TRUE(dummy_res.is_ok());

            EXPECT_GT(dummy_res.get()->get_num_vertices(), plain_res.get()->get_num_vertices());
        }
        {
            auto res = graph_algorithm::NetlistGraph::from_netlist(nullptr);
            EXPECT_TRUE(res.is_error());
        }
        TEST_END
    }

    /**
     * Every bit of a ripple counter forms a strongly connected component of its own, holding the flip-flop
     * and the inverter in its feedback path. The ripple connection between the bits is unidirectional and
     * must not join them.
     *
     * Functions: get_connected_components
     */
    TEST_F(GraphAlgorithmTest, check_strongly_connected_components_of_ripple_counter)
    {
        TEST_START
        {
            auto nl = build_ripple_counter();
            ASSERT_NE(nl, nullptr);

            auto graph_res = graph_algorithm::NetlistGraph::from_netlist(nl.get());
            ASSERT_TRUE(graph_res.is_ok());
            auto graph = graph_res.get();

            auto components_res = graph_algorithm::get_connected_components(graph.get(), true, 0);
            ASSERT_TRUE(components_res.is_ok());
            const auto components = components_res.get();

            // the three toggle loops plus the tap gate on its own
            EXPECT_EQ(component_sizes(components), std::vector<u32>({1, 2, 2, 2}));

            const std::vector<std::set<std::string>> expected = {
                {"and_tap"},
                {"ff_0", "inv_0"},
                {"ff_1", "inv_1"},
                {"ff_2", "inv_2"},
            };
            EXPECT_EQ(components_as_names(graph.get(), components), expected);
        }
        TEST_END
    }

    /**
     * A ring counter is one cycle through all of its gates, so it is a single strongly connected component
     * that spans the flip-flops and the inverter closing the ring.
     *
     * Functions: get_connected_components
     */
    TEST_F(GraphAlgorithmTest, check_strongly_connected_component_of_ring)
    {
        TEST_START
        {
            const u32 num_flip_flops = 5;

            auto nl = build_ring(num_flip_flops);
            ASSERT_NE(nl, nullptr);

            auto graph_res = graph_algorithm::NetlistGraph::from_netlist(nl.get());
            ASSERT_TRUE(graph_res.is_ok());
            auto graph = graph_res.get();

            auto components_res = graph_algorithm::get_connected_components(graph.get(), true, 0);
            ASSERT_TRUE(components_res.is_ok());
            const auto components = components_res.get();

            ASSERT_EQ(components.size(), 1);
            EXPECT_EQ(components.front().size(), num_flip_flops + 1);

            const std::vector<std::set<std::string>> expected = {{"feedback_inv", "ff_0", "ff_1", "ff_2", "ff_3", "ff_4"}};
            EXPECT_EQ(components_as_names(graph.get(), components), expected);
        }
        TEST_END
    }

    /**
     * `min_size` drops the components below the given size and leaves the rest untouched.
     *
     * Functions: get_connected_components
     */
    TEST_F(GraphAlgorithmTest, check_connected_components_min_size)
    {
        TEST_START
        {
            auto nl = build_ripple_counter();
            ASSERT_NE(nl, nullptr);

            auto graph_res = graph_algorithm::NetlistGraph::from_netlist(nl.get());
            ASSERT_TRUE(graph_res.is_ok());
            auto graph = graph_res.get();

            auto unfiltered_res = graph_algorithm::get_connected_components(graph.get(), true, 0);
            ASSERT_TRUE(unfiltered_res.is_ok());
            EXPECT_EQ(component_sizes(unfiltered_res.get()), std::vector<u32>({1, 2, 2, 2}));

            auto filtered_res = graph_algorithm::get_connected_components(graph.get(), true, 2);
            ASSERT_TRUE(filtered_res.is_ok());
            EXPECT_EQ(component_sizes(filtered_res.get()), std::vector<u32>({2, 2, 2}));

            auto empty_res = graph_algorithm::get_connected_components(graph.get(), true, 3);
            ASSERT_TRUE(empty_res.is_ok());
            EXPECT_TRUE(empty_res.get().empty());
        }
        {
            // weak components ignore edge direction, so the whole ripple counter is one of them
            auto nl = build_ripple_counter();
            ASSERT_NE(nl, nullptr);

            auto graph_res = graph_algorithm::NetlistGraph::from_netlist(nl.get());
            ASSERT_TRUE(graph_res.is_ok());
            auto graph = graph_res.get();

            auto components_res = graph_algorithm::get_connected_components(graph.get(), false, 0);
            ASSERT_TRUE(components_res.is_ok());
            ASSERT_EQ(components_res.get().size(), 1);
            EXPECT_EQ(components_res.get().front().size(), nl->get_gates().size());
        }
        TEST_END
    }

    /**
     * A netlist without feedback has no strongly connected component larger than a single gate, and a
     * netlist without gates yields no components at all instead of failing.
     *
     * Functions: get_connected_components
     */
    TEST_F(GraphAlgorithmTest, check_connected_components_without_feedback)
    {
        TEST_START
        {
            auto nl = build_combinational();
            ASSERT_NE(nl, nullptr);

            auto graph_res = graph_algorithm::NetlistGraph::from_netlist(nl.get());
            ASSERT_TRUE(graph_res.is_ok());
            auto graph = graph_res.get();

            auto components_res = graph_algorithm::get_connected_components(graph.get(), true, 0);
            ASSERT_TRUE(components_res.is_ok());
            EXPECT_EQ(components_res.get().size(), nl->get_gates().size());
            for (const auto& component : components_res.get())
            {
                EXPECT_EQ(component.size(), 1);
            }

            auto filtered_res = graph_algorithm::get_connected_components(graph.get(), true, 2);
            ASSERT_TRUE(filtered_res.is_ok());
            EXPECT_TRUE(filtered_res.get().empty());
        }
        {
            auto nl = test_utils::create_empty_netlist();
            ASSERT_NE(nl, nullptr);
            ASSERT_TRUE(nl->get_gates().empty());

            auto graph_res = graph_algorithm::NetlistGraph::from_netlist(nl.get());
            ASSERT_TRUE(graph_res.is_ok());
            auto graph = graph_res.get();
            EXPECT_EQ(graph->get_num_vertices(), 0);

            auto components_res = graph_algorithm::get_connected_components(graph.get(), true, 0);
            ASSERT_TRUE(components_res.is_ok());
            EXPECT_TRUE(components_res.get().empty());
        }
        {
            auto res = graph_algorithm::get_connected_components(nullptr, true, 0);
            EXPECT_TRUE(res.is_error());
        }
        TEST_END
    }

    /**
     * Vertices and gates map onto each other in both directions, which is what makes a component of vertex
     * numbers usable as a statement about the netlist.
     *
     * Functions: get_vertices_from_gates, get_gates_from_vertices, get_vertex_from_gate, get_gate_from_vertex
     */
    TEST_F(GraphAlgorithmTest, check_vertex_gate_mapping)
    {
        TEST_START
        {
            auto nl = build_ripple_counter();
            ASSERT_NE(nl, nullptr);

            auto graph_res = graph_algorithm::NetlistGraph::from_netlist(nl.get());
            ASSERT_TRUE(graph_res.is_ok());
            auto graph = graph_res.get();

            const auto gates = nl->get_gates();

            auto vertices_res = graph->get_vertices_from_gates(gates);
            ASSERT_TRUE(vertices_res.is_ok());
            const auto vertices = vertices_res.get();
            ASSERT_EQ(vertices.size(), gates.size());
            EXPECT_EQ(std::set<u32>(vertices.begin(), vertices.end()).size(), gates.size());

            auto round_trip_res = graph->get_gates_from_vertices(vertices);
            ASSERT_TRUE(round_trip_res.is_ok());
            EXPECT_TRUE(test_utils::vectors_have_same_content(round_trip_res.get(), gates));

            for (auto* g : gates)
            {
                auto vertex_res = graph->get_vertex_from_gate(g);
                ASSERT_TRUE(vertex_res.is_ok());
                auto gate_res = graph->get_gate_from_vertex(vertex_res.get());
                ASSERT_TRUE(gate_res.is_ok());
                EXPECT_EQ(gate_res.get(), g);
            }

            // a vertex that was never created has no gate behind it
            EXPECT_TRUE(graph->get_gate_from_vertex(graph->get_num_vertices() + 1).is_error());
        }
        {
            // the mapping is per graph, so a gate of another netlist is not part of it
            auto nl    = build_ripple_counter();
            auto other = build_combinational();
            ASSERT_NE(nl, nullptr);
            ASSERT_NE(other, nullptr);

            auto graph_res = graph_algorithm::NetlistGraph::from_netlist(nl.get());
            ASSERT_TRUE(graph_res.is_ok());

            EXPECT_TRUE(graph_res.get()->get_vertex_from_gate(other->get_gates().front()).is_error());
        }
        TEST_END
    }
}    // namespace hal
