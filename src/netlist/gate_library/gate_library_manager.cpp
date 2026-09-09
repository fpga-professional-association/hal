#include "hal_core/netlist/gate_library/gate_library_manager.h"

#include "hal_core/netlist/gate_library/gate_library.h"
#include "hal_core/netlist/gate_library/gate_library_parser/gate_library_parser_manager.h"
#include "hal_core/netlist/gate_library/gate_library_writer/gate_library_writer_manager.h"
#include "hal_core/utilities/log.h"
#include "hal_core/utilities/utils.h"

#include <algorithm>
#include <iostream>
#include <set>

namespace hal
{
    namespace gate_library_manager
    {
        namespace
        {
            std::map<std::filesystem::path, std::shared_ptr<GateLibrary>> m_gate_libraries;

            Result<std::monostate> prepare_library(const std::shared_ptr<GateLibrary>& lib)
            {
                auto gate_types = lib->get_gate_types();

                for (const auto& [gt_name, gt] : gate_types)
                {
                    if (gt->has_property(GateTypeProperty::power))
                    {
                        lib->mark_vcc_gate_type(gt);
                    }
                    if (gt->has_property(GateTypeProperty::ground))
                    {
                        lib->mark_gnd_gate_type(gt);
                    }
                }

                if (lib->get_gnd_gate_types().empty())
                {
                    std::string name = "HAL_GND";
                    if (gate_types.find(name) != gate_types.end())
                    {
                        return ERR("could not prepare gate library '" + lib->get_name() + "': no GND gate type found within gate library, but gate type 'HAL_GND' already exists");
                    }

                    GateType* gt = lib->create_gate_type(name, {GateTypeProperty::combinational, GateTypeProperty::ground});
                    if (auto res = gt->create_pin("O", PinDirection::output, PinType::ground); res.is_error())
                    {
                        return ERR_APPEND(res.get_error(), "could not prepare gate library '" + lib->get_name() + "': failed to create output pin 'O' for gate type 'HAL_GND'");
                    }
                    gt->add_boolean_function("O", BooleanFunction::Const(BooleanFunction::Value::ZERO));
                    lib->mark_gnd_gate_type(gt);
                    log_info("gate_library_manager", "gate library did not contain a GND gate, auto-generated type '{}'.", name);
                }

                if (lib->get_vcc_gate_types().empty())
                {
                    std::string name = "HAL_VDD";
                    if (gate_types.find(name) != gate_types.end())
                    {
                        return ERR("could not prepare gate library '" + lib->get_name() + "': no VDD gate type found within gate library, but gate type 'HAL_VDD' already exists");
                    }

                    GateType* gt = lib->create_gate_type(name, {GateTypeProperty::combinational, GateTypeProperty::power});
                    if (auto res = gt->create_pin("O", PinDirection::output, PinType::power); res.is_error())
                    {
                        return ERR_APPEND(res.get_error(), "could not prepare gate library '" + lib->get_name() + "': failed to create output pin 'O' for gate type 'HAL_VDD'");
                    }
                    gt->add_boolean_function("O", BooleanFunction::Const(BooleanFunction::Value::ONE));
                    lib->mark_vcc_gate_type(gt);
                    log_info("gate_library_manager", "gate library did not contain a VDD gate, auto-generated type '{}'.", name);
                }

                return OK({});
            }

            /**
             * Build the key under which a composite gate library is cached. It is not a path on disk, it only has to
             * identify the ordered list of files the library was assembled from.
             */
            std::filesystem::path get_composite_key(const std::vector<std::filesystem::path>& file_paths)
            {
                std::string key = "<multi>";
                for (u32 i = 0; i < file_paths.size(); i++)
                {
                    key += (i == 0 ? "" : "+") + file_paths.at(i).string();
                }
                return std::filesystem::path(key);
            }
        }    // namespace

        GateLibrary* load(std::filesystem::path file_path, bool reload)
        {
            if (!std::filesystem::exists(file_path))
            {
                log_error("gate_library_manager", "gate library file '{}' does not exist.", file_path.string());
                return nullptr;
            }

            if (!file_path.is_absolute())
            {
                file_path = std::filesystem::absolute(file_path);
            }

            if (!reload)
            {
                if (auto it = m_gate_libraries.find(file_path); it != m_gate_libraries.end())
                {
                    log_info("gate_library_parser", "the gate library file '{}' is already loaded.", file_path.string());
                    return it->second.get();
                }
            }

            std::shared_ptr<GateLibrary> gate_lib = gate_library_parser_manager::parse(file_path);
            if (gate_lib == nullptr)
            {
                return nullptr;
            }

            if (auto res = prepare_library(gate_lib); res.is_error())
            {
                log_error("gate_library_parser", "error encountered while loading gate library:\n{}", res.get_error().get());
                return nullptr;
            }

            GateLibrary* res                     = gate_lib.get();
            m_gate_libraries[file_path.string()] = std::move(gate_lib);
            return res;
        }

        std::vector<std::string> split_search_list(const std::string& search_list)
        {
            std::vector<std::string> entries;

            for (const auto& part : utils::split(search_list, ','))
            {
                const std::string entry = utils::trim(part);
                if (!entry.empty())
                {
                    entries.push_back(entry);
                }
            }

            return entries;
        }

        std::vector<std::filesystem::path> resolve_search_list(const std::vector<std::string>& entries)
        {
            std::vector<std::filesystem::path> resolved;
            std::set<std::string> already_resolved;

            auto append = [&resolved, &already_resolved](const std::filesystem::path& path) {
                const std::filesystem::path absolute_path = std::filesystem::absolute(path);
                if (already_resolved.insert(absolute_path.string()).second)
                {
                    resolved.push_back(absolute_path);
                }
                else
                {
                    log_info("gate_library_manager", "gate library file '{}' is listed more than once, ignoring the later occurrence.", absolute_path.string());
                }
            };

            for (const auto& entry : entries)
            {
                if (entry.empty())
                {
                    continue;
                }

                std::error_code ec;
                const std::filesystem::path entry_path(entry);

                if (std::filesystem::is_directory(entry_path, ec))
                {
                    // sort so that the resulting order does not depend on the order the file system reports
                    std::vector<std::filesystem::path> in_directory;
                    for (const auto& lib_path : utils::RecursiveDirectoryRange(entry_path))
                    {
                        if (gate_library_parser_manager::can_parse(lib_path.path()))
                        {
                            in_directory.push_back(lib_path.path());
                        }
                    }
                    std::sort(in_directory.begin(), in_directory.end());

                    if (in_directory.empty())
                    {
                        log_warning("gate_library_manager", "gate library search list entry '{}' is a directory that does not contain any gate library files.", entry);
                        continue;
                    }

                    for (const auto& lib_path : in_directory)
                    {
                        append(lib_path);
                    }
                }
                else if (std::filesystem::exists(entry_path, ec))
                {
                    append(entry_path);
                }
                else
                {
                    // not a path that exists, so search the standard gate library directories for the file name
                    const auto stripped_name = entry_path.filename();
                    const auto lib_path      = utils::get_file(stripped_name, utils::get_gate_library_directories());
                    if (lib_path.empty())
                    {
                        log_error("gate_library_manager", "could not resolve gate library search list entry '{}': no such file, directory, or gate library in the default directories.", entry);
                        continue;
                    }
                    append(lib_path);
                }
            }

            return resolved;
        }

        GateLibrary* load_multiple(const std::vector<std::filesystem::path>& file_paths, const std::string& name, bool reload)
        {
            if (file_paths.empty())
            {
                log_error("gate_library_manager", "could not load gate libraries: no gate library file given.");
                return nullptr;
            }

            // a single library needs no composition, so keep it addressable by its own file
            if (file_paths.size() == 1)
            {
                return load(file_paths.front(), reload);
            }

            std::vector<std::filesystem::path> absolute_paths;
            for (const auto& file_path : file_paths)
            {
                if (!std::filesystem::exists(file_path))
                {
                    log_error("gate_library_manager", "gate library file '{}' does not exist.", file_path.string());
                    return nullptr;
                }
                absolute_paths.push_back(std::filesystem::absolute(file_path));
            }

            const std::filesystem::path key = get_composite_key(absolute_paths);

            if (!reload)
            {
                if (auto it = m_gate_libraries.find(key); it != m_gate_libraries.end())
                {
                    log_info("gate_library_manager", "the gate libraries '{}' are already loaded as a composite gate library.", key.string());
                    return it->second.get();
                }
            }

            // parse fresh copies: the gate types are moved into the composite library, which must not tear apart a
            // library that is already loaded and possibly in use by a netlist
            std::vector<std::unique_ptr<GateLibrary>> parsed_libraries;
            for (const auto& file_path : absolute_paths)
            {
                std::unique_ptr<GateLibrary> parsed = gate_library_parser_manager::parse(file_path);
                if (parsed == nullptr)
                {
                    log_error("gate_library_manager", "could not load gate library '{}' as part of a composite gate library.", file_path.string());
                    return nullptr;
                }
                parsed_libraries.push_back(std::move(parsed));
            }

            std::string composite_name = name;
            if (composite_name.empty())
            {
                for (u32 i = 0; i < parsed_libraries.size(); i++)
                {
                    composite_name += (i == 0 ? "" : "+") + parsed_libraries.at(i)->get_name();
                }
            }

            // start out without a path so that the source paths of the absorbed libraries are the only provenance
            std::shared_ptr<GateLibrary> composite = std::make_shared<GateLibrary>(std::filesystem::path(), composite_name);

            for (u32 i = 0; i < parsed_libraries.size(); i++)
            {
                if (auto res = composite->absorb(parsed_libraries.at(i).get()); res.is_error())
                {
                    log_error("gate_library_manager", "could not absorb gate library '{}' into composite gate library:\n{}", absolute_paths.at(i).string(), res.get_error().get());
                    return nullptr;
                }
            }

            composite->set_path(key);

            if (auto res = prepare_library(composite); res.is_error())
            {
                log_error("gate_library_manager", "error encountered while loading composite gate library:\n{}", res.get_error().get());
                return nullptr;
            }

            log_info("gate_library_manager", "loaded composite gate library '{}' from {} gate library files.", composite->get_name(), absolute_paths.size());

            GateLibrary* res      = composite.get();
            m_gate_libraries[key] = std::move(composite);
            return res;
        }

        void load_all(bool reload)
        {
            std::vector<std::filesystem::path> lib_dirs = utils::get_gate_library_directories();

            for (const auto& lib_dir : lib_dirs)
            {
                if (!std::filesystem::exists(lib_dir))
                {
                    continue;
                }

                log_info("gate_library_manager", "loading all gate library files from {}.", lib_dir.string());

                for (const auto& lib_path : utils::RecursiveDirectoryRange(lib_dir))
                {
                    load(lib_path.path(), reload);
                }
            }
        }

        std::vector<std::filesystem::path> get_all_path()
        {
            std::vector<std::filesystem::path> retval;
            for (const auto& lib_dir : utils::get_gate_library_directories())
            {
                if (!std::filesystem::exists(lib_dir))
                    continue;
                for (const auto& lib_path : utils::RecursiveDirectoryRange(lib_dir))
                    retval.push_back(lib_path.path());
            }
            return retval;
        }

        bool save(std::filesystem::path file_path, GateLibrary* gate_lib, bool overwrite)
        {
            if (std::filesystem::exists(file_path))
            {
                if (overwrite)
                {
                    log_info("gate_library_manager", "gate library file '{}' already exists and will be overwritten.", file_path.string());
                }
                else
                {
                    log_error("gate_library_manager", "gate library file '{}' already exists, aborting.", file_path.string());
                    return false;
                }
            }

            if (!file_path.is_absolute())
            {
                file_path = std::filesystem::absolute(file_path);
            }

            return gate_library_writer_manager::write(gate_lib, file_path);
        }

        std::shared_ptr<GateLibrary> get_owning(const GateLibrary* gate_lib)
        {
            // A reload replaces the entry of a library in the map, which destroys the library that was
            // there before. Anything still using it - a loaded netlist above all - has to keep it alive,
            // so hand out the owning pointer rather than the raw one.
            for (const auto& [path, lib] : m_gate_libraries)
            {
                UNUSED(path);
                if (lib.get() == gate_lib)
                {
                    return lib;
                }
            }

            // not managed here, for instance a library a test built itself: refer to it without owning it
            return std::shared_ptr<GateLibrary>(const_cast<GateLibrary*>(gate_lib), [](GateLibrary*) {});
        }

        void remove(std::filesystem::path file_path)
        {
            m_gate_libraries.erase(file_path);
        }

        GateLibrary* get_gate_library(const std::string& file_path)
        {
            std::filesystem::path absolute_path;

            if (std::filesystem::exists(file_path))
            {
                // if an existing file is queried, load it by its absolute path
                absolute_path = std::filesystem::absolute(file_path);
            }
            else
            {
                // if a non existing file is queried, search for it in the standard directories
                auto stripped_name = std::filesystem::path(file_path).filename();
                log_info("gate_library_manager", "file '{}' does not exist, searching for '{}' in the default gate library directories...", file_path, stripped_name.string());

                auto lib_path = utils::get_file(stripped_name, utils::get_gate_library_directories());
                if (lib_path.empty())
                {
                    log_info("gate_library_manager", "could not find gate library file '{}'.", stripped_name.string());
                    return nullptr;
                }
                absolute_path = std::filesystem::absolute(lib_path);
            }

            // absolute path to file is known, check if it is already loaded
            if (auto it = m_gate_libraries.find(absolute_path.string()); it != m_gate_libraries.end())
            {
                return it->second.get();
            }

            // not loaded yet -> load
            return load(absolute_path);
        }

        GateLibrary* get_gate_library_by_name(const std::string& lib_name)
        {
            for (const auto& it : m_gate_libraries)
            {
                if (it.second->get_name() == lib_name)
                {
                    return it.second.get();
                }
            }
            return nullptr;
        }

        std::vector<GateLibrary*> get_gate_libraries()
        {
            std::vector<GateLibrary*> res;
            res.reserve(m_gate_libraries.size());
            for (const auto& it : m_gate_libraries)
            {
                res.push_back(it.second.get());
            }
            return res;
        }
    }    // namespace gate_library_manager
}    // namespace hal