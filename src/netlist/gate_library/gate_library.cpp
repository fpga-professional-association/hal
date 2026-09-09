#include "hal_core/netlist/gate_library/gate_library.h"

#include "hal_core/netlist/gate_library/gate_type.h"
#include "hal_core/utilities/log.h"

#include <algorithm>

namespace hal
{
    GateLibrary::GateLibrary(const std::filesystem::path& path, const std::string& name) : m_name(name), m_path(path)
    {
        m_next_gate_type_id = 1;

        if (!path.empty())
        {
            m_source_paths.push_back(path);
        }
    }

    std::string GateLibrary::get_name() const
    {
        return m_name;
    }

    std::filesystem::path GateLibrary::get_path() const
    {
        return m_path;
    }

    void GateLibrary::set_path(const std::filesystem::path& modified_path)
    {
        // a library with a single source file is fully described by that (new) file and follows along, whereas the
        // sources of a composite library are unrelated to the path it is addressed by
        if (m_source_paths.size() == 1 && m_source_paths.front() == m_path)
        {
            m_source_paths.front() = modified_path;
        }

        m_path = modified_path;
    }

    const std::vector<std::filesystem::path>& GateLibrary::get_source_paths() const
    {
        return m_source_paths;
    }

    bool GateLibrary::is_composite() const
    {
        return m_source_paths.size() > 1;
    }

    void GateLibrary::set_name(const std::string &modified_name)
    {
        m_name = modified_name;
    }

    void GateLibrary::set_gate_location_data_category(const std::string& category)
    {
        m_gate_location_data_category = category;
    }

    const std::string& GateLibrary::get_gate_location_data_category() const
    {
        return m_gate_location_data_category;
    }

    void GateLibrary::set_gate_location_data_identifiers(const std::string& x_coordinate, const std::string& y_coordinate)
    {
        m_gate_location_data_identifiers = std::make_pair(x_coordinate, y_coordinate);
    }

    const std::pair<std::string, std::string>& GateLibrary::get_gate_location_data_identifiers() const
    {
        return m_gate_location_data_identifiers;
    }

    GateType* GateLibrary::create_gate_type(const std::string& name, std::set<GateTypeProperty> properties, std::unique_ptr<GateTypeComponent> component)
    {
        if (m_gate_type_map.find(name) != m_gate_type_map.end())
        {
            log_error("gate_library", "could not create gate type with name '{}' as a gate type with the same name already exists within gate library '{}'.", name, m_name);
            return nullptr;
        }

        std::unique_ptr<GateType> gt = std::unique_ptr<GateType>(new GateType(this, get_unique_gate_type_id(), name, properties, std::move(component)));

        auto res = gt.get();
        m_gate_type_map.emplace(name, res);
        m_gate_types.push_back(std::move(gt));
        return res;
    }

    GateType* GateLibrary::replace_gate_type(u32 id, const std::string& name, std::set<GateTypeProperty> properties, std::unique_ptr<GateTypeComponent> component)
    {
        // must not insert duplicate name
        auto it = m_gate_type_map.find(name);
        if (it != m_gate_type_map.end() && it->second->get_id() != id)
        {
            log_error("gate_library", "could not replace gate type ID={} since new name '{}' exists already within gate library '{}'.", id, name, m_name);
            return nullptr;
        }

        auto jt = m_gate_types.begin();
        while (jt != m_gate_types.end())
        {
            if (jt->get()->get_id() == id) break;
            ++jt;
        }
        if (jt == m_gate_types.end())
        {
            log_error("gate_library", "could not replace gate type ID={}, no gate with this ID found within gate library", id, m_name);
            return nullptr;
        }
        auto nt = m_gate_type_map.find(jt->get()->get_name());
        if (nt != m_gate_type_map.end())
            m_gate_type_map.erase(nt);
        m_gate_types.erase(jt);

        std::unique_ptr<GateType> gt = std::unique_ptr<GateType>(new GateType(this, id, name, properties, std::move(component)));

        auto res = gt.get();
        m_gate_type_map.emplace(name, res);
        m_gate_types.push_back(std::move(gt));
        return res;
    }

    Result<std::monostate> GateLibrary::absorb(GateLibrary* other, bool overwrite_existing)
    {
        if (other == nullptr)
        {
            return ERR("could not absorb gate library into gate library '" + m_name + "': nullptr given as gate library");
        }

        if (other == this)
        {
            return ERR("could not absorb gate library '" + m_name + "' into itself");
        }

        // there is only one location data category per library, so the first one absorbed sets it for the composite
        const bool is_first = m_source_paths.empty() && m_gate_types.empty();
        if (is_first)
        {
            m_gate_location_data_category    = other->m_gate_location_data_category;
            m_gate_location_data_identifiers = other->m_gate_location_data_identifiers;
        }
        else if (m_gate_location_data_category != other->m_gate_location_data_category || m_gate_location_data_identifiers != other->m_gate_location_data_identifiers)
        {
            log_warning("gate_library",
                        "gate library '{}' stores gate locations differently than gate library '{}' does, keeping the way of the latter.",
                        other->get_name(),
                        m_name);
        }

        for (auto& gt_owner : other->m_gate_types)
        {
            if (gt_owner == nullptr)
            {
                continue;
            }

            GateType* gt            = gt_owner.get();
            const std::string gt_name = gt->get_name();

            if (const auto it = m_gate_type_map.find(gt_name); it != m_gate_type_map.end())
            {
                if (!overwrite_existing)
                {
                    log_warning("gate_library",
                                "gate type '{}' of gate library '{}' is shadowed by the gate type of the same name that gate library '{}' already contains, the latter takes precedence.",
                                gt_name,
                                other->get_name(),
                                m_name);
                    continue;
                }

                log_warning("gate_library",
                            "gate type '{}' of gate library '{}' replaces the gate type of the same name that gate library '{}' already contains.",
                            gt_name,
                            other->get_name(),
                            m_name);

                GateType* shadowed = it->second;
                m_gate_type_map.erase(it);
                m_vcc_gate_types.erase(gt_name);
                m_gnd_gate_types.erase(gt_name);
                m_black_box_gate_types.erase(gt_name);
                m_gate_types.erase(std::remove_if(m_gate_types.begin(), m_gate_types.end(), [shadowed](const std::unique_ptr<GateType>& gt_ptr) { return gt_ptr.get() == shadowed; }),
                                   m_gate_types.end());
            }

            const bool is_vcc       = other->m_vcc_gate_types.find(gt_name) != other->m_vcc_gate_types.end();
            const bool is_gnd       = other->m_gnd_gate_types.find(gt_name) != other->m_gnd_gate_types.end();
            const bool is_black_box = other->m_black_box_gate_types.find(gt_name) != other->m_black_box_gate_types.end();

            // gate type IDs are unique within a gate library only, so the absorbed type gets a fresh one
            gt->m_gate_library = this;
            gt->m_id           = get_unique_gate_type_id();

            m_gate_type_map[gt_name] = gt;
            m_gate_types.push_back(std::move(gt_owner));

            if (is_vcc)
            {
                m_vcc_gate_types[gt_name] = gt;
            }
            if (is_gnd)
            {
                m_gnd_gate_types[gt_name] = gt;
            }
            if (is_black_box)
            {
                m_black_box_gate_types[gt_name] = gt;
            }
        }

        for (const auto& include : other->m_includes)
        {
            m_includes.push_back(include);
        }

        for (const auto& source_path : other->m_source_paths)
        {
            m_source_paths.push_back(source_path);
        }

        // whatever was not taken over is dropped together with the (now empty) source library
        other->m_gate_types.clear();
        other->m_gate_type_map.clear();
        other->m_vcc_gate_types.clear();
        other->m_gnd_gate_types.clear();
        other->m_black_box_gate_types.clear();
        other->m_includes.clear();
        other->m_source_paths.clear();

        return OK({});
    }

    Result<GateType*> GateLibrary::create_black_box_gate_type(const std::string& name, const std::vector<std::pair<std::string, u32>>& ports)
    {
        if (m_gate_type_map.find(name) != m_gate_type_map.end())
        {
            return ERR("could not create black box gate type '" + name + "': a gate type with the same name already exists within gate library '" + m_name + "'");
        }

        // no properties: nothing is known about the internals of a black box, not even whether it is combinational
        GateType* gt = create_gate_type(name, {});
        if (gt == nullptr)
        {
            return ERR("could not create black box gate type '" + name + "' within gate library '" + m_name + "': failed to create gate type");
        }

        for (const auto& [port_name, width] : ports)
        {
            if (width == 0)
            {
                continue;
            }

            // the direction of a port cannot be recovered from a netlist instantiation, hence 'inout'
            if (width == 1)
            {
                if (auto res = gt->create_pin(port_name, PinDirection::inout); res.is_error())
                {
                    return ERR_APPEND(res.get_error(), "could not create black box gate type '" + name + "' within gate library '" + m_name + "': failed to create pin '" + port_name + "'");
                }
                continue;
            }

            std::vector<GatePin*> pins;
            for (u32 i = width; i > 0; i--)
            {
                const std::string pin_name = port_name + "(" + std::to_string(i - 1) + ")";
                if (auto res = gt->create_pin(pin_name, PinDirection::inout, PinType::none, false); res.is_error())
                {
                    return ERR_APPEND(res.get_error(), "could not create black box gate type '" + name + "' within gate library '" + m_name + "': failed to create pin '" + pin_name + "'");
                }
                else
                {
                    pins.push_back(res.get());
                }
            }

            // a descending group of pins given from the most significant bit down is what a bus of a gate library
            // parsed from a file looks like, and what the netlist parsers expect when they map a bus connection
            if (auto res = gt->create_pin_group(port_name, pins, PinDirection::inout, PinType::none, false, static_cast<i32>(width - 1)); res.is_error())
            {
                return ERR_APPEND(res.get_error(), "could not create black box gate type '" + name + "' within gate library '" + m_name + "': failed to create pin group '" + port_name + "'");
            }
        }

        m_black_box_gate_types[name] = gt;
        return OK(gt);
    }

    bool GateLibrary::is_black_box_gate_type(const GateType* gate_type) const
    {
        if (gate_type == nullptr)
        {
            return false;
        }

        const auto it = m_black_box_gate_types.find(gate_type->get_name());
        return it != m_black_box_gate_types.end() && it->second == gate_type;
    }

    std::unordered_map<std::string, GateType*> GateLibrary::get_black_box_gate_types() const
    {
        return m_black_box_gate_types;
    }

    bool GateLibrary::contains_gate_type(GateType* gate_type) const
    {
        if (gate_type == nullptr)
        {
            return false;
        }

        auto it = m_gate_type_map.find(gate_type->get_name());
        if (it == m_gate_type_map.end())
        {
            return false;
        }
        return *(it->second) == *gate_type;
    }

    bool GateLibrary::contains_gate_type_by_name(const std::string& name) const
    {
        if (auto it = m_gate_type_map.find(name); it != m_gate_type_map.end())
        {
            return true;
        }

        return false;
    }

    GateType* GateLibrary::get_gate_type_by_name(const std::string& name) const
    {
        if (auto it = m_gate_type_map.find(name); it != m_gate_type_map.end())
        {
            return it->second;
        }

        log_error("gate_library", "could not find the specified gate type, as there exists no gate type called '{}' within gate library '{}'.", name, m_name);
        return nullptr;
    }

    std::unordered_map<std::string, GateType*> GateLibrary::get_gate_types(const std::function<bool(const GateType*)>& filter) const
    {
        if (filter)
        {
            std::unordered_map<std::string, GateType*> res;
            for (const auto& type : m_gate_types)
            {
                if (filter(type.get()))
                {
                    res[type->get_name()] = type.get();
                }
            }
            return res;
        }

        return m_gate_type_map;
    }

    bool GateLibrary::mark_vcc_gate_type(GateType* gate_type)
    {
        auto out_pins = gate_type->get_output_pins();

        if (gate_type->get_input_pins().empty() && (out_pins.size() == 1))
        {
            auto bf = gate_type->get_boolean_function(out_pins.at(0));
            if (!bf.is_empty() && bf.has_constant_value(1))
            {
                m_vcc_gate_types.emplace(gate_type->get_name(), gate_type);
                return true;
            }
        }

        return false;
    }

    std::unordered_map<std::string, GateType*> GateLibrary::get_vcc_gate_types() const
    {
        return m_vcc_gate_types;
    }

    bool GateLibrary::mark_gnd_gate_type(GateType* gate_type)
    {
        auto out_pins = gate_type->get_output_pins();

        if (gate_type->get_input_pins().empty() && (out_pins.size() == 1))
        {
            auto bf = gate_type->get_boolean_function(out_pins.at(0));
            if (!bf.is_empty() && bf.has_constant_value(0))
            {
                m_gnd_gate_types.emplace(gate_type->get_name(), gate_type);
                return true;
            }
        }

        return false;
    }

    std::unordered_map<std::string, GateType*> GateLibrary::get_gnd_gate_types() const
    {
        return m_gnd_gate_types;
    }

    std::vector<std::string> GateLibrary::get_includes() const
    {
        return m_includes;
    }

    void GateLibrary::add_include(const std::string& include)
    {
        m_includes.push_back(include);
    }

    u32 GateLibrary::get_unique_gate_type_id()
    {
        return m_next_gate_type_id++;
    }

    void GateLibrary::remove_gate_type(const std::string& name)
    {
        if (m_gate_type_map.find(name) == m_gate_type_map.end())
        {
            log_error("gate_library", "could not remove gate type with name '{}' as a gate type with this name does not exist within gate library '{}'.", name, m_name);
        }
        else
        {
            auto it = m_gate_type_map.find(name);
            m_gate_type_map.erase(it);

            // the type is no longer reachable by name, so it must not be reported as a black box of this library either
            m_black_box_gate_types.erase(name);
        }
    }
}    // namespace hal
