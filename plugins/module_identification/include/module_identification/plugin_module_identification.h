#pragma once

#include "hal_core/plugin_system/plugin_interface_base.h"
#include "module_identification/types/candidate_types.h"
#include "module_identification/types/multithreading_types.h"

#include <memory.h>

namespace hal
{
    class Gate;
    class Module;

    namespace module_identification
    {
        class BaseCandidate;
        class StructuralCandidate;
    }    // namespace module_identification

    /**
     * @class ModuleIdentificationPlugin
     * @brief Plugin for identifying functional modules within a netlist.
     * 
     * This plugin identifies groups of gates in a netlist that fulfill a recognized function, primarily focusing on arithmetic operations.
     */
    class PLUGIN_API ModuleIdentificationPlugin : public BasePluginInterface
    {
    public:
        /**
         * @brief Constructor for `ModuleIdentificationPlugin`.
         */
        ModuleIdentificationPlugin();

        /**
         * @brief Default destructor for `ModuleIdentificationPlugin`.
         */
        ~ModuleIdentificationPlugin() = default;

        /**
         * @brief Get the name of the plugin.
         *
         * @returns The name of the plugin.
         */
        std::string get_name() const override;

        /**
         * @brief Get a short description of the plugin.
         *
         * @returns The short description.
         */
        std::string get_description() const override;

        /**
         * @brief Get the version of the plugin.
         *
         * @returns The version of the plugin.
         */
        std::string get_version() const override;

        /**
         * @brief Get the plugin dependencies.
         * 
         * @returns A set of plugin names that this plugin depends on.
         */
        std::set<std::string> get_dependencies() const override;
    };
}    // namespace hal
