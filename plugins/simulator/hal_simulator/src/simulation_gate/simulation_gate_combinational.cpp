#include "netlist_simulator/netlist_simulator.h"

#include <algorithm>
#include <string>

namespace hal
{
    NetlistSimulator::SimulationGateCombinational::SimulationGateCombinational(const Gate* gate) : SimulationGate(gate)
    {
    }

    Result<std::monostate> NetlistSimulator::SimulationGateCombinational::initialize_functions()
    {
        const Gate* gate = m_gate;

        const std::string context = "cannot simulate gate '" + gate->get_name() + "' with ID " + std::to_string(gate->get_id()) + " of type '" + gate->get_type()->get_name() + "'";

        const std::unordered_map<std::string, BooleanFunction> functions = gate->get_boolean_functions();
        const std::vector<GatePin*> declared_output_pins                 = gate->get_type()->get_output_pins();

        for (GatePin* pin : declared_output_pins)
        {
            const Net* out_net = gate->get_fan_out_net(pin);
            if (out_net == nullptr)
            {
                // An output pin that drives nothing cannot influence the simulation, and there is no net to
                // record a value for -- the result map is keyed by net, so keeping it would put a null key in
                // there that the read-back dereferences. Skipping it is also what lets a gate type with more
                // output pins than the instance configures be simulated at all: an Agilex ALM declares four
                // (combout, sumout, cout, shareout) and any one configuration defines at most two.
                continue;
            }

            const auto func_it = functions.find(pin->get_name());
            if (func_it == functions.end())
            {
                return ERR(context + ": output pin '" + pin->get_name() + "' drives net '" + out_net->get_name() + "' with ID " + std::to_string(out_net->get_id())
                           + " but the gate has no Boolean function for it.");
            }

            BooleanFunction func = func_it->second;

            // resolve recursion within output functions
            while (true)
            {
                const auto vars = func.get_variable_names();
                bool exit       = true;
                for (const GatePin* other_pin : declared_output_pins)
                {
                    const std::string& other_pin_name = other_pin->get_name();
                    if (std::find(vars.begin(), vars.end(), other_pin_name) == vars.end())
                    {
                        continue;
                    }

                    const auto other_it = functions.find(other_pin_name);
                    if (other_it == functions.end())
                    {
                        return ERR(context + ": the Boolean function of output pin '" + pin->get_name() + "' refers to output pin '" + other_pin_name
                                   + "', for which the gate has no Boolean function.");
                    }

                    auto substituted = func.substitute(other_pin_name, other_it->second);
                    if (substituted.is_error())
                    {
                        return ERR_APPEND(substituted.get_error(), context + ": failed to resolve output pin '" + other_pin_name + "' within the function of output pin '" + pin->get_name() + "'.");
                    }

                    func = substituted.get();
                    exit = false;
                }
                if (exit)
                {
                    break;
                }
            }

            m_output_pins.push_back(pin);
            m_output_nets.push_back(out_net);
            m_functions.emplace(out_net, func);
        }

        return OK({});
    }

    bool NetlistSimulator::SimulationGateCombinational::simulate(const Simulation& simulation, const WaveEvent& event, std::map<std::pair<const Net*, u64>, BooleanFunction::Value>& new_events)
    {
        UNUSED(simulation);

        // compute delay, currently just a placeholder
        u64 delay = 0;

        for (auto out_net : m_output_nets)
        {
            BooleanFunction::Value result = m_functions[out_net].evaluate(m_input_values).get();

            new_events[std::make_pair(out_net, event.time + delay)] = result;
        }

        return true;
    }
}    // namespace hal
