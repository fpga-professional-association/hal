#include "clock_tree_extractor/clock_tree.h"

#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/gate_library/gate_library.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "netlist_test_utils.h"

#include <igraph/igraph.h>
#include <memory>
#include <set>
#include <string>
#include <vector>

namespace hal
{
    /**
     * Tests for the clock tree extractor.
     *
     * The netlists are built here rather than parsed so that the expected tree can be stated by hand: a
     * clock that arrives at a flip-flop through a known number of buffers has exactly one path, and the
     * vertices on it as well as the edges between them are the answer.
     *
     * Both tests below are about the *edges*. A clock tree with the right vertices and no edges looks
     * healthy to `get_nets`/`get_gates` and is useless to every traversal accessor, which is precisely
     * what a clock arriving straight from a port used to produce.
     */
    class ClockTreeExtractorTest : public ::testing::Test
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
            std::unique_ptr<Netlist> netlist;
            Net* clock = nullptr;
            Gate* buffer = nullptr;
            std::vector<Gate*> flip_flops;
        };

        /**
         * `num_ffs` flip-flops on one clock. With `buffered`, the clock reaches them through a buffer, so
         * the tree is clk -> buffer -> ff; without it, the global input net drives every clock pin
         * directly and the tree is clk -> ff.
         */
        Fixture build(u32 num_ffs, bool buffered)
        {
            Fixture fixture;
            fixture.netlist       = test_utils::create_empty_netlist();
            const GateLibrary* gl = fixture.netlist->get_gate_library();

            fixture.clock = fixture.netlist->create_net("clk");
            fixture.clock->mark_global_input_net();

            Net* clock_pin_net = fixture.clock;
            if (buffered)
            {
                fixture.buffer = fixture.netlist->create_gate(gl->get_gate_type_by_name("BUF"), "clk_buffer");
                fixture.clock->add_destination(fixture.buffer, "I");
                clock_pin_net = fixture.netlist->create_net("clk_buffered");
                clock_pin_net->add_source(fixture.buffer, "O");
            }

            for (u32 i = 0; i < num_ffs; i++)
            {
                Gate* ff = fixture.netlist->create_gate(gl->get_gate_type_by_name("DFF"), "ff_" + std::to_string(i));
                clock_pin_net->add_destination(ff, "CLK");
                fixture.flip_flops.push_back(ff);
            }

            return fixture;
        }

        /** The names of the gates and nets a (sub)tree contains, so that a tree can be compared by hand. */
        std::set<std::string> names_of(const cte::ClockTree* tree)
        {
            std::set<std::string> names;
            for (const Gate* gate : tree->get_gates())
            {
                names.insert(gate->get_name());
            }
            for (const Net* net : tree->get_nets())
            {
                names.insert(net->get_name());
            }
            return names;
        }

        /** The names of the neighbours of `ptr` in the given direction. */
        std::set<std::string> neighbors_of(const cte::ClockTree* tree, const void* ptr, igraph_neimode_t direction)
        {
            std::set<std::string> names;
            auto res = tree->get_neighbors(ptr, direction);
            if (res.is_error())
            {
                return names;
            }
            for (const auto& [neighbor, type] : res.get())
            {
                names.insert(type == cte::PtrType::GATE ? ((const Gate*)neighbor)->get_name() : ((const Net*)neighbor)->get_name());
            }
            return names;
        }
    };

    /**
     * A clock that arrives straight from a port is connected to the flip-flops it clocks: the net is the
     * root, every flip-flop is one of its children, and no flip-flop has a child of its own. The tree used
     * to hold all of those vertices and not a single edge.
     *
     * Functions: from_netlist, get_neighbors
     */
    TEST_F(ClockTreeExtractorTest, check_direct_clock_input_is_connected)
    {
        TEST_START
        {
            Fixture fixture = build(3, false);

            auto res = cte::ClockTree::from_netlist(fixture.netlist.get());
            ASSERT_TRUE(res.is_ok());
            std::unique_ptr<cte::ClockTree> tree = res.get();

            EXPECT_EQ(names_of(tree.get()), (std::set<std::string>{"clk", "ff_0", "ff_1", "ff_2"}));
            EXPECT_EQ(tree->get_netlist(), fixture.netlist.get());

            EXPECT_EQ(neighbors_of(tree.get(), fixture.clock, IGRAPH_OUT), (std::set<std::string>{"ff_0", "ff_1", "ff_2"}));
            EXPECT_TRUE(neighbors_of(tree.get(), fixture.clock, IGRAPH_IN).empty());

            for (const Gate* ff : fixture.flip_flops)
            {
                EXPECT_EQ(neighbors_of(tree.get(), ff, IGRAPH_IN), (std::set<std::string>{"clk"}));
                EXPECT_TRUE(neighbors_of(tree.get(), ff, IGRAPH_OUT).empty());
            }
        }
        TEST_END
    }

    /**
     * A subtree holds the vertices reachable from its root, not the ones that are not. `get_subtree` read
     * the forward map of igraph's induced subgraph as if a vertex ID of zero meant "not contained", which
     * is how igraph reported it before 1.0; since then a vertex that is not part of the subgraph is marked
     * with -1 and vertex 0 is a perfectly ordinary vertex. Reading it the old way dropped the root and kept
     * exactly the vertices that had been excluded, so the assertions below are inverted by that bug rather
     * than merely off by one.
     *
     * Functions: get_subtree
     */
    TEST_F(ClockTreeExtractorTest, check_subtree_contains_the_reachable_vertices)
    {
        TEST_START
        {
            Fixture fixture = build(3, true);

            auto res = cte::ClockTree::from_netlist(fixture.netlist.get());
            ASSERT_TRUE(res.is_ok());
            std::unique_ptr<cte::ClockTree> tree = res.get();

            EXPECT_EQ(names_of(tree.get()), (std::set<std::string>{"clk", "clk_buffer", "ff_0", "ff_1", "ff_2"}));
            EXPECT_EQ(neighbors_of(tree.get(), fixture.clock, IGRAPH_OUT), (std::set<std::string>{"clk_buffer"}));
            EXPECT_EQ(neighbors_of(tree.get(), fixture.buffer, IGRAPH_OUT), (std::set<std::string>{"ff_0", "ff_1", "ff_2"}));

            // the whole tree is reachable from the clock net
            auto from_root = tree->get_subtree(fixture.clock, false);
            ASSERT_TRUE(from_root.is_ok());
            EXPECT_EQ(names_of(from_root.get().get()), (std::set<std::string>{"clk", "clk_buffer", "ff_0", "ff_1", "ff_2"}));

            // everything below the buffer, i.e. the clock net is not part of it
            auto from_buffer = tree->get_subtree(fixture.buffer, false);
            ASSERT_TRUE(from_buffer.is_ok());
            EXPECT_EQ(names_of(from_buffer.get().get()), (std::set<std::string>{"clk_buffer", "ff_0", "ff_1", "ff_2"}));

            // a leaf is a subtree of one vertex, and asking for its parent walks one edge up
            auto from_leaf = tree->get_subtree(fixture.flip_flops.at(0), false);
            ASSERT_TRUE(from_leaf.is_ok());
            EXPECT_EQ(names_of(from_leaf.get().get()), (std::set<std::string>{"ff_0"}));

            auto from_leaf_parent = tree->get_subtree(fixture.flip_flops.at(0), true);
            ASSERT_TRUE(from_leaf_parent.is_ok());
            EXPECT_EQ(names_of(from_leaf_parent.get().get()), (std::set<std::string>{"clk_buffer", "ff_0", "ff_1", "ff_2"}));
        }
        {
            // an object that is not part of the tree is an error, not a lookup that lands anywhere
            Fixture fixture = build(1, false);

            auto res = cte::ClockTree::from_netlist(fixture.netlist.get());
            ASSERT_TRUE(res.is_ok());
            std::unique_ptr<cte::ClockTree> tree = res.get();

            Net* stray = fixture.netlist->create_net("not_in_the_tree");
            EXPECT_TRUE(tree->get_subtree(stray, false).is_error());
            EXPECT_TRUE(tree->get_vertex_from_ptr(stray).is_error());
        }
        TEST_END
    }

    /**
     * A null netlist is an error, not a crash.
     *
     * Functions: from_netlist
     */
    TEST_F(ClockTreeExtractorTest, check_nullptr_netlist)
    {
        TEST_START
        {
            EXPECT_TRUE(cte::ClockTree::from_netlist(nullptr).is_error());
        }
        TEST_END
    }
}    // namespace hal
