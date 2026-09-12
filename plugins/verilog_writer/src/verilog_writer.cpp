#include "verilog_writer/verilog_writer.h"

#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/module.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/utilities/log.h"

#include <algorithm>
#include <fstream>
#include <string>
#include <vector>

namespace hal
{
    namespace
    {
        /**
         * Check whether a pin group is to be written using bus notation, i.e., as `input [1:0] a;` instead of one port per pin.
         *
         * A pin group featuring more than one pin is a bus, provided its pins share a common direction. Whether a pin group of
         * exactly one pin is a bus cannot be inferred from its size: `input [0:0] a;` and `input a;` both yield a single pin.
         * The parser therefore marks every pin group that has been declared with a range as ordered, which is the recorded
         * "declared as a bus" property this relies on.
         *
         * @param[in] pin_group - The pin group.
         * @returns `true` if the pin group is written using bus notation, `false` otherwise.
         */
        bool is_bus(const PinGroup<ModulePin>* pin_group)
        {
            const std::vector<ModulePin*> pins = pin_group->get_pins();
            if (pins.empty())
            {
                return false;
            }

            if (pins.size() < 2 && !pin_group->is_ordered())
            {
                return false;
            }

            const PinDirection direction = pins.front()->get_direction();
            return std::all_of(pins.begin(), pins.end(), [direction](const ModulePin* pin) { return pin->get_direction() == direction; });
        }

        // a bit position beyond this is a corrupted pin name rather than a bus index, and honoring it would emit a port
        // declaration wide enough to exhaust the memory of whatever reads the file back
        const i32 MAX_BUS_INDEX = 1 << 20;

        /**
         * Recover the declared bit position of every pin of a bus pin group from its name.
         *
         * A pin group indexes its pins consecutively and cannot hold gaps, so a bus port of which only some bits are connected
         * loses its declared bit positions on import: `input [3:0] a;` with only `a[0]` and `a[3]` in use becomes a two-pin
         * group indexed `[3:2]`. The declared position does survive in the pin names, which the parser derives from the port
         * range (`a(0)`, `a(3)`), and that is what is recovered here.
         *
         * @param[in] pin_group - The pin group.
         * @returns One index per pin, in pin order, or an empty vector if the pin names do not encode bit positions.
         */
        std::vector<i32> get_declared_bus_indices(const PinGroup<ModulePin>* pin_group)
        {
            const std::vector<ModulePin*> pins = pin_group->get_pins();
            const std::string prefix           = pin_group->get_name() + "(";

            std::vector<i32> indices;
            indices.reserve(pins.size());

            for (const ModulePin* pin : pins)
            {
                const std::string& name = pin->get_name();
                if (name.size() <= prefix.size() + 1 || name.compare(0, prefix.size(), prefix) != 0 || name.back() != ')')
                {
                    return {};
                }

                const std::string digits = name.substr(prefix.size(), name.size() - prefix.size() - 1);
                if (digits.size() > 9 || !std::all_of(digits.begin(), digits.end(), [](const char c) { return c >= '0' && c <= '9'; }))
                {
                    return {};
                }

                const i32 index = std::stoi(digits);
                if (index > MAX_BUS_INDEX)
                {
                    return {};
                }

                indices.push_back(index);
            }

            // the pins of a bus are ordered by bit position, most significant bit first for a descending bus
            for (u32 i = 1; i < indices.size(); i++)
            {
                if (pin_group->is_ascending() ? (indices.at(i) <= indices.at(i - 1)) : (indices.at(i) >= indices.at(i - 1)))
                {
                    return {};
                }
            }

            return indices;
        }
    }    // namespace

    const std::set<std::string> VerilogWriter::valid_types = {"string", "integer", "floating_point", "bit_value", "bit_vector", "bit_string"};

    Result<std::monostate> VerilogWriter::write(Netlist* netlist, const std::filesystem::path& file_path)
    {
        std::stringstream res_stream;

        if (netlist == nullptr)
        {
            return ERR("could not write netlist to Verilog file '" + file_path.string() + "': netlist is a 'nullptr'");
        }

        if (file_path.empty())
        {
            return ERR("could not write netlist to Verilog file '" + file_path.string() + "': file path is empty");
        }

        // get modules in hierarchical order (bottom-up)
        std::vector<Module*> ordered_modules;
        {
            const std::vector<Module*> modules = netlist->get_modules();
            std::unordered_set<Module*> modules_set(modules.begin(), modules.end());

            while (!modules_set.empty())
            {
                for (auto it = modules_set.begin(); it != modules_set.end();)
                {
                    std::vector<Module*> submodules = (*it)->get_submodules();
                    if (submodules.empty() || std::all_of(submodules.begin(), submodules.end(), [modules_set](Module* submod) { return modules_set.find(submod) == modules_set.end(); }))
                    {
                        ordered_modules.push_back(*it);
                        it = modules_set.erase(it);
                    }
                    else
                    {
                        it++;
                    }
                }
            }

            assert(ordered_modules.back()->is_top_module() == true);
        }

        std::unordered_map<const Module*, std::string> module_aliases;
        std::unordered_map<std::string, u32> module_identifier_occurrences;
        for (Module* mod : ordered_modules)
        {
            if (auto res = write_module_declaration(res_stream, mod, module_aliases, module_identifier_occurrences); res.is_error())
            {
                return ERR_APPEND(res.get_error(), "could not write netlist to Verilog file '" + file_path.string() + "': failed to write module declaration");
            }
            res_stream << std::endl;
        }

        // write to file
        std::ofstream file;
        file.open(file_path.string(), std::ofstream::out);
        if (!file.is_open())
        {
            return ERR("could not write netlist to Verilog file '" + file_path.string() + "': failed to open file");
        }
        file << "`timescale 1 ps/1 ps" << std::endl;
        file << res_stream.str();
        file.close();

        return OK({});
    }

    Result<std::monostate> VerilogWriter::write_module_declaration(std::stringstream& res_stream,
                                                                   const Module* module,
                                                                   std::unordered_map<const Module*, std::string>& module_type_aliases,
                                                                   std::unordered_map<std::string, u32>& module_type_occurrences) const
    {
        // deal with empty modules
        if (module->get_gates(nullptr, true).empty() && !module->is_top_module())
        {
            return OK({});
        }

        // deal with unspecified module type
        std::string module_type = module->get_type();
        if (module_type.empty())
        {
            module_type = module->get_name();
            if (!module->is_top_module())
            {
                module_type += "_type";
            }
        }
        module_type_aliases[module] = get_unique_alias(module_type_occurrences, module_type);

        if (const std::string& design_name = module->get_netlist()->get_design_name(); module_type_aliases.at(module) == "top_module" && !design_name.empty())
        {
            res_stream << "module " << escape(design_name);
        }
        else
        {
            res_stream << "module " << escape(module_type_aliases.at(module));
        }

        std::unordered_map<const DataContainer*, std::string> aliases;
        std::unordered_map<std::string, u32> identifier_occurrences;

        bool first_port = true;
        std::stringstream tmp_stream;

        res_stream << "(";
        for (const PinGroup<ModulePin>* pin_group : module->get_pin_groups())
        {
            const std::vector<ModulePin*> pins = pin_group->get_pins();
            if (pins.empty())
            {
                continue;
            }

            if (is_bus(pin_group))
            {
                // preserve the bus notation of pin groups comprising multiple pins
                if (first_port)
                {
                    first_port = false;
                }
                else
                {
                    res_stream << ",";
                }

                const std::string group_alias = escape(get_unique_alias(identifier_occurrences, pin_group->get_name()));

                // A pin group cannot hold gaps, so a partially connected bus port is re-indexed contiguously on import and
                // every bit above the first gap would shift when the group index is trusted here. The declared bit positions
                // survive in the pin names, which is what is written instead. Only the top module is treated this way: the
                // ports of a sub-module are connected positionally through a concatenation at its instantiation site, which
                // would no longer line up with a declaration that spans the gaps as well.
                const std::vector<i32> declared_indices = module->is_top_module() ? get_declared_bus_indices(pin_group) : std::vector<i32>();

                i32 left_index  = pin_group->is_ascending() ? pin_group->get_lowest_index() : pin_group->get_highest_index();
                i32 right_index = pin_group->is_ascending() ? pin_group->get_highest_index() : pin_group->get_lowest_index();
                if (!declared_indices.empty())
                {
                    left_index  = declared_indices.front();
                    right_index = declared_indices.back();
                }

                res_stream << group_alias;
                tmp_stream << "    " << enum_to_string(pins.front()->get_direction()) << " [" << left_index << ":" << right_index << "] " << group_alias << ";" << std::endl;

                for (u32 pin_index = 0; pin_index < pins.size(); pin_index++)
                {
                    const ModulePin* pin = pins.at(pin_index);

                    i32 index;
                    if (!declared_indices.empty())
                    {
                        index = declared_indices.at(pin_index);
                    }
                    else
                    {
                        auto index_res = pin_group->get_index(pin);
                        if (index_res.is_error())
                        {
                            return ERR_APPEND(index_res.get_error(),
                                              "could not write declaration of module '" + module->get_name() + "' with ID " + std::to_string(module->get_id()) + ": failed to get index of pin '"
                                                  + pin->get_name() + "' within pin group '" + pin_group->get_name() + "'");
                        }
                        index = index_res.get();
                    }

                    aliases[pin->get_net()] = group_alias + "[" + std::to_string(index) + "]";
                }
            }
            else
            {
                for (const ModulePin* pin : pins)
                {
                    Net* net = pin->get_net();
                    if (first_port)
                    {
                        first_port = false;
                    }
                    else
                    {
                        res_stream << ",";
                    }

                    aliases[net] = escape(get_unique_alias(identifier_occurrences, pin->get_name()));

                    res_stream << aliases.at(net);
                    tmp_stream << "    " << enum_to_string(pin->get_direction()) << " " << aliases.at(net) << ";" << std::endl;
                }
            }
        }

        res_stream << ");" << std::endl;
        res_stream << tmp_stream.str();

        {
            // module parameters
            const std::map<std::tuple<std::string, std::string>, std::tuple<std::string, std::string>>& data = module->get_data_map();

            for (const auto& [first, second] : data)
            {
                const auto& [category, key] = first;
                const auto& [type, value]   = second;

                if (category != "generic" || valid_types.find(type) == valid_types.end())
                {
                    continue;
                }

                res_stream << "    parameter " << escape(key) << " = ";
                if (auto res = write_parameter_value(res_stream, type, value); res.is_error())
                {
                    return ERR_APPEND(res.get_error(),
                                      "could not write declaration of module '" + module->get_name() + "' with ID " + std::to_string(module->get_id()) + ": failed to write parameter value");
                }
                res_stream << ";" << std::endl;
            }
        }

        std::unordered_set<Net*> port_nets       = module->get_input_nets();
        std::unordered_set<Net*> output_nets_tmp = module->get_output_nets();
        port_nets.reserve(output_nets_tmp.size());
        port_nets.insert(output_nets_tmp.begin(), output_nets_tmp.end());

        // HAL models the constant signals as the two nets named "'0'" and "'1'", which are held back for a second pass; see
        // below for why they cannot be named on a first-come basis like every other net
        std::vector<Net*> constant_nets;

        for (Net* net : module->get_nets())
        {
            if (port_nets.find(net) != port_nets.end())
            {
                continue;
            }

            if (aliases.find(net) == aliases.end())
            {
                if (const std::string& net_name = net->get_name(); net_name == "'0'" || net_name == "'1'")
                {
                    constant_nets.push_back(net);
                    continue;
                }

                auto net_alias = escape(get_unique_alias(identifier_occurrences, net->get_name()));
                aliases[net]   = net_alias;

                res_stream << "    wire " << net_alias << ";" << std::endl;
            }
        }

        // Escaping "'0'" yields the escaped identifier \'0' -- which is exactly how a net that genuinely carries that name
        // (as emitted by fasm2bels, see emsec/hal#545) is written as well. A netlist holding both used to declare the same
        // wire twice and the two distinct nets silently merged on re-parse. The constant nets are therefore named last and
        // under the identifier the escaped net would occupy, so the escaped net keeps its name and the constant is the one
        // that gets a unique suffix. Their wires carry the number literal they stand for as a continuous assignment, which
        // is what the parser merges back into these very nets, name included -- an identifier alone cannot name them, as
        // the parser deliberately keeps the backslash of \'0' to tell the two apart.
        std::sort(constant_nets.begin(), constant_nets.end(), [](const Net* a, const Net* b) { return a->get_name() < b->get_name(); });
        for (Net* net : constant_nets)
        {
            const std::string net_name = net->get_name();
            const std::string alias    = escape(get_unique_alias(identifier_occurrences, "\\" + net_name));
            aliases[net]               = alias;

            res_stream << "    wire " << alias << " = " << ((net_name == "'0'") ? "1'b0" : "1'b1") << ";" << std::endl;
        }

        // write gate instances
        for (const Gate* gate : module->get_gates())
        {
            res_stream << std::endl;
            if (auto res = write_gate_instance(res_stream, gate, aliases, identifier_occurrences); res.is_error())
            {
                return ERR_APPEND(res.get_error(),
                                  "could not write declaration of module '" + module->get_name() + "' with ID " + std::to_string(module->get_id()) + ": failed to write gate '" + gate->get_name()
                                      + "' with ID " + std::to_string(gate->get_id()));
            }
        }

        // write module instances
        for (const Module* sub_module : module->get_submodules())
        {
            res_stream << std::endl;
            if (auto res = write_module_instance(res_stream, sub_module, aliases, identifier_occurrences, module_type_aliases); res.is_error())
            {
                return ERR_APPEND(res.get_error(),
                                  "could not write declaration of module '" + module->get_name() + "' with ID " + std::to_string(module->get_id()) + ": failed to write sub-module '"
                                      + sub_module->get_name() + "' with ID " + std::to_string(sub_module->get_id()));
            }
        }

        res_stream << "endmodule" << std::endl;

        return OK({});
    }

    Result<std::monostate> VerilogWriter::write_gate_instance(std::stringstream& res_stream,
                                                              const Gate* gate,
                                                              std::unordered_map<const DataContainer*, std::string>& aliases,
                                                              std::unordered_map<std::string, u32>& identifier_occurrences) const
    {
        const GateType* gate_type = gate->get_type();

        res_stream << "    " << escape(gate_type->get_name());
        if (auto res = write_parameter_assignments(res_stream, gate); res.is_error())
        {
            return ERR_APPEND(res.get_error(), "could not write gate '" + gate->get_name() + "' with ID " + std::to_string(gate->get_id()) + ": failed to write parameter assignments");
        }
        aliases[gate] = escape(get_unique_alias(identifier_occurrences, gate->get_name()));
        res_stream << " " << aliases.at(gate);

        // collect all endpoints (i.e., pins that are actually in use)
        std::unordered_map<const GatePin*, const Net*> connections;
        for (const Endpoint* ep : gate->get_fan_in_endpoints())
        {
            connections[ep->get_pin()] = ep->get_net();
        }

        for (const Endpoint* ep : gate->get_fan_out_endpoints())
        {
            connections[ep->get_pin()] = ep->get_net();
        }

        // extract pin assignments (in order, respecting pin groups)
        std::vector<std::pair<std::string, std::vector<const Net*>>> pin_assignments;
        for (const PinGroup<GatePin>* pin_group : gate_type->get_pin_groups())
        {
            std::vector<const Net*> nets;
            for (const GatePin* pin : pin_group->get_pins())
            {
                if (const auto ep_it = connections.find(pin); ep_it != connections.end())
                {
                    nets.push_back(ep_it->second);
                }
                else
                {
                    nets.push_back(nullptr);
                }
            }

            // only append if at least one pin of the group is connected
            if (std::any_of(nets.begin(), nets.end(), [](const Net* net) { return net != nullptr; }))
            {
                pin_assignments.push_back(std::make_pair(pin_group->get_name(), nets));
            }
        }

        if (auto res = write_pin_assignments(res_stream, pin_assignments, aliases); res.is_error())
        {
            return ERR_APPEND(res.get_error(), "could not write gate '" + gate->get_name() + "' with ID " + std::to_string(gate->get_id()) + ": failed to write pin assignments");
        }

        res_stream << ";" << std::endl;

        return OK({});
    }

    Result<std::monostate> VerilogWriter::write_module_instance(std::stringstream& res_stream,
                                                                const Module* module,
                                                                std::unordered_map<const DataContainer*, std::string>& aliases,
                                                                std::unordered_map<std::string, u32>& identifier_occurrences,
                                                                std::unordered_map<const Module*, std::string>& module_type_aliases) const
    {
        res_stream << "    " << escape(module_type_aliases.at(module));
        if (auto res = write_parameter_assignments(res_stream, module); res.is_error())
        {
            return ERR_APPEND(res.get_error(), "could not write sub-module '" + module->get_name() + "' with ID " + std::to_string(module->get_id()) + ": failed to write parameter assignments");
        }
        aliases[module] = escape(get_unique_alias(identifier_occurrences, module->get_name()));
        res_stream << " " << aliases.at(module);

        // extract port assignments (in order, respecting pin groups)
        std::vector<std::pair<std::string, std::vector<const Net*>>> port_assignments;

        for (const PinGroup<ModulePin>* pin_group : module->get_pin_groups())
        {
            const std::vector<ModulePin*> pins = pin_group->get_pins();
            if (pins.empty())
            {
                continue;
            }

            if (is_bus(pin_group))
            {
                // the pins of a bus are connected using a concatenation in the order in which they have been declared
                std::vector<const Net*> nets;
                for (const ModulePin* pin : pins)
                {
                    nets.push_back(pin->get_net());
                }
                port_assignments.push_back(std::make_pair(pin_group->get_name(), std::move(nets)));
            }
            else
            {
                for (const ModulePin* pin : pins)
                {
                    port_assignments.push_back(std::make_pair(pin->get_name(), std::vector<const Net*>({pin->get_net()})));
                }
            }
        }

        if (auto res = write_pin_assignments(res_stream, port_assignments, aliases); res.is_error())
        {
            return ERR_APPEND(res.get_error(), "could not write sub-module '" + module->get_name() + "' with ID " + std::to_string(module->get_id()) + ": failed to write pin assignments");
        }

        res_stream << ";" << std::endl;

        return OK({});
    }

    Result<std::monostate> VerilogWriter::write_parameter_assignments(std::stringstream& res_stream, const DataContainer* container) const
    {
        const std::map<std::tuple<std::string, std::string>, std::tuple<std::string, std::string>>& data = container->get_data_map();

        bool first_parameter = true;
        for (const auto& [first, second] : data)
        {
            const auto& [category, key] = first;
            const auto& [type, value]   = second;

            if (category != "generic" || valid_types.find(type) == valid_types.end())
            {
                continue;
            }

            if (first_parameter)
            {
                res_stream << " #(" << std::endl;
                first_parameter = false;
            }
            else
            {
                res_stream << "," << std::endl;
            }

            res_stream << "        ." << escape(key) << "(";

            if (auto res = write_parameter_value(res_stream, type, value); res.is_error())
            {
                return ERR_APPEND(res.get_error(), "could not write parameter assignments: failed to write parameter value '" + value + "' of type '" + type + "'");
            }

            res_stream << ")";
        }

        if (!first_parameter)
        {
            res_stream << std::endl << "    )";
        }

        return OK({});
    }

    Result<std::monostate> VerilogWriter::write_pin_assignments(std::stringstream& res_stream,
                                                                const std::vector<std::pair<std::string, std::vector<const Net*>>>& pin_assignments,
                                                                std::unordered_map<const DataContainer*, std::string>& aliases) const
    {
        res_stream << " (" << std::endl;
        bool first_pin = true;
        for (const auto& [pin, nets] : pin_assignments)
        {
            if (first_pin)
            {
                first_pin = false;
            }
            else
            {
                res_stream << "," << std::endl;
            }

            res_stream << "        ." << escape(pin) << "(";
            if (nets.size() > 1)
            {
                res_stream << "{" << std::endl << "            ";
            }

            bool first_net = true;
            for (auto it = nets.begin(); it != nets.end(); it++)
            {
                const Net* net = *it;

                if (!first_net)
                {
                    res_stream << "," << std::endl << "            ";
                }
                first_net = false;

                if (net != nullptr)
                {
                    if (const auto alias_it = aliases.find(net); alias_it != aliases.end())
                    {
                        res_stream << alias_it->second;
                    }
                    else
                    {
                        return ERR("could not write pin assignments: no alias for net '" + net->get_name() + "' with ID " + std::to_string(net->get_id()) + " found");
                    }
                }
                else
                {
                    // An unconnected pin of a group of which at least one other pin is connected. A concatenation slot cannot
                    // be left empty in Verilog, so it is filled with the high-impedance literal, which the parser skips: the
                    // pin comes back unconnected. Writing a placeholder identifier instead would either reference a wire that
                    // is never declared -- which HAL refuses to re-parse -- or, once declared, turn a pin that is unconnected
                    // into one that is driven by a dangling net.
                    res_stream << "1'bz";
                }
            }

            if (nets.size() > 1)
            {
                res_stream << std::endl << "        }";
            }

            res_stream << ")";
        }

        res_stream << std::endl << "    )";

        return OK({});
    }

    Result<std::monostate> VerilogWriter::write_parameter_value(std::stringstream& res_stream, const std::string& type, const std::string& value) const
    {
        if (type == "string")
        {
            res_stream << "\"" << value << "\"";
        }
        else if (type == "integer" || type == "floating_point")
        {
            res_stream << value;
        }
        else if (type == "bit_value")
        {
            res_stream << "1'b" << value;
        }
        else if (type == "bit_vector")
        {
            u32 len = value.size() * 4;
            // if (value.at(0) == '0' || value.at(0) == '1')
            // {
            //     len -= 3;
            // }
            // else if (value.at(0) == '2' || value.at(0) == '3')
            // {
            //     len -= 2;
            // }
            // else if (value.at(0) >= '4' && value.at(0) <= '7')
            // {
            //     len -= 1;
            // }
            res_stream << len << "'h" << value;
        }
        else if (type == "bit_string")
        {
            u32 len = value.size();
            // if (value.at(0) == '0' || value.at(0) == '1')
            // {
            //     len -= 3;
            // }
            // else if (value.at(0) == '2' || value.at(0) == '3')
            // {
            //     len -= 2;
            // }
            // else if (value.at(0) >= '4' && value.at(0) <= '7')
            // {
            //     len -= 1;
            // }
            res_stream << len << "'b" << value;
        }
        else
        {
            return ERR("could not write parameter value '" + value + "' of type '" + type + "': invalid type");
        }

        return OK({});
    }

    std::string VerilogWriter::get_unique_alias(std::unordered_map<std::string, u32>& name_occurrences, const std::string& name) const
    {
        name_occurrences[name]++;

        // if the name only appears once, we don't have to suffix it
        if (name_occurrences[name] < 2)
        {
            return name;
        }

        // otherwise, add a unique string to the name
        return name + "__[" + std::to_string(name_occurrences[name]) + "]__";
    }

    std::string VerilogWriter::escape(const std::string& s) const
    {
        if (s.empty())
        {
            return "";
        }

        const char first = s.at(0);
        if (first == '\\' && s.size() > 1 && ((s.at(1) >= '0' && s.at(1) <= '9') || s.at(1) == '\''))
        {
            // the name already is an escaped identifier that has to keep its escape to remain distinguishable from a
            // number literal (e.g., '\'1'' as emitted by fasm2bels), hence it must not be escaped a second time
            return s + " ";
        }
        else if (!(first >= 'a' && first <= 'z') && !(first >= 'A' && first <= 'Z') && first != '_')
        {
            return "\\" + s + " ";
        }
        else if (std::any_of(s.begin(), s.end(), [](const char c) { return (!(c >= 'a' && c <= 'z') && !(c >= 'A' && c <= 'Z') && !(c >= '0' && c <= '9') && c != '_' && c != '$'); }))
        {
            return "\\" + s + " ";
        }

        return s;
    }
}    // namespace hal
