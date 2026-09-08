#include "example_analysis/example_analysis.h"

#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/gate_library/gate_library.h"
#include "hal_core/netlist/gate_library/gate_type.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/netlist/pins/gate_pin.h"

#include <algorithm>
#include <map>

namespace hal
{
    namespace example_analysis
    {
        namespace
        {
            /**
             * The input pins of `gate_type` that carry `PinType::clock`.
             *
             * Asking the gate type for the *semantics* of its pins is the whole point: matching pin
             * names ("CLK", "C", "CK", ...) is what makes an analysis silently library specific.
             */
            std::vector<GatePin*> input_clock_pins(const GateType* gate_type)
            {
                return gate_type->get_pins([](GatePin* pin) {
                    return pin->get_type() == PinType::clock && pin->get_direction() == PinDirection::input;
                });
            }
        }    // namespace

        Result<Report> analyze(Netlist* netlist)
        {
            if (netlist == nullptr)
            {
                return ERR("cannot analyze: netlist is a nullptr");
            }

            const std::vector<Gate*> sequential_gates = netlist->get_gates([](const Gate* gate) {
                const GateType* gate_type = gate->get_type();
                return gate_type != nullptr && gate_type->has_property(GateTypeProperty::sequential);
            });

            if (sequential_gates.empty())
            {
                const GateLibrary* library = netlist->get_gate_library();
                return ERR("cannot analyze netlist '" + netlist->get_design_name() + "': not one of its " + std::to_string(netlist->get_gates().size())
                           + " gates has a type carrying the 'sequential' gate type property, and this analysis groups sequential gates by their clock net. "
                             "Gate library: '"
                           + (library != nullptr ? library->get_name() : std::string("<none>"))
                           + "'. Either the design really is purely combinational, or its gate library does not declare the property - check it with "
                             "'python tools/hal_capabilities check example_analysis --netlist <path>', which reports the same requirement from "
                             "plugins/example_analysis/capabilities.json without running anything.");
            }

            // Keyed by ID rather than by pointer: a std::map over pointers orders by address, which
            // would make the output depend on the allocator and stop two runs from being comparable.
            std::map<u32, ClockDomain> domains_by_clock_net;
            std::map<std::string, UnsupportedGateType> unsupported_by_type_name;
            std::vector<Gate*> unresolved;

            for (Gate* gate : sequential_gates)
            {
                const GateType* gate_type                = gate->get_type();
                const std::vector<GatePin*> clock_pins   = input_clock_pins(gate_type);

                if (clock_pins.empty())
                {
                    auto it = unsupported_by_type_name.find(gate_type->get_name());
                    if (it == unsupported_by_type_name.end())
                    {
                        UnsupportedGateType entry;
                        entry.gate_type = gate_type;
                        entry.count     = 1;
                        entry.reason    = "gate type '" + gate_type->get_name()
                                       + "' carries the 'sequential' property but exposes no input pin of type 'clock', so this analysis cannot tell which "
                                         "clock its gates belong to. Level-sensitive latches and gate types whose library entry omits the pin type both land "
                                         "here; fix the library entry, or treat these gates separately.";
                        unsupported_by_type_name.emplace(gate_type->get_name(), entry);
                    }
                    else
                    {
                        it->second.count++;
                    }
                    continue;
                }

                Net* clock_net = nullptr;
                for (const GatePin* pin : clock_pins)
                {
                    Net* net = gate->get_fan_in_net(pin);
                    if (net != nullptr)
                    {
                        clock_net = net;
                        break;
                    }
                }

                if (clock_net == nullptr)
                {
                    unresolved.push_back(gate);
                    continue;
                }

                ClockDomain& domain = domains_by_clock_net[clock_net->get_id()];
                domain.clock_net    = clock_net;
                domain.gates.push_back(gate);
            }

            Report report;
            report.sequential_gate_count = static_cast<u32>(sequential_gates.size());

            for (auto& entry : domains_by_clock_net)
            {
                ClockDomain& domain = entry.second;
                std::sort(domain.gates.begin(), domain.gates.end(), [](const Gate* lhs, const Gate* rhs) { return lhs->get_id() < rhs->get_id(); });
                report.domains.push_back(domain);
            }

            // The map already orders by clock net ID, so a stable sort by size leaves ties in that
            // order and the whole report is reproducible.
            std::stable_sort(report.domains.begin(), report.domains.end(), [](const ClockDomain& lhs, const ClockDomain& rhs) {
                return lhs.gates.size() > rhs.gates.size();
            });

            std::sort(unresolved.begin(), unresolved.end(), [](const Gate* lhs, const Gate* rhs) { return lhs->get_id() < rhs->get_id(); });
            report.unresolved_gates = unresolved;

            for (const auto& entry : unsupported_by_type_name)
            {
                report.unsupported.push_back(entry.second);
            }

            return OK(report);
        }
    }    // namespace example_analysis
}    // namespace hal
