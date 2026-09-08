/**
 * @file plugin_example_analysis.h
 * @brief Plugin interface of the example_analysis plugin.
 */

#pragma once

#include "hal_core/plugin_system/plugin_interface_base.h"

#include <set>
#include <string>

namespace hal
{
    /**
     * @class ExampleAnalysisPlugin
     * @brief Plugin interface for example_analysis.
     *
     * Group sequential gates by the net that drives their clock pin (reference output of tools/new_plugin.py).
     */
    class PLUGIN_API ExampleAnalysisPlugin : public BasePluginInterface
    {
    public:
        ExampleAnalysisPlugin()  = default;
        ~ExampleAnalysisPlugin() = default;

        /**
         * Get the name of the plugin.
         *
         * @returns The name of the plugin.
         */
        std::string get_name() const override;

        /**
         * Get the version of the plugin.
         *
         * @returns The version of the plugin.
         */
        std::string get_version() const override;

        /**
         * Get a short description of the plugin.
         *
         * @returns The description of the plugin.
         */
        std::string get_description() const override;

        /**
         * Get the plugins this plugin depends on.
         *
         * Must stay in sync with `dependencies.plugins` in `capabilities.json`;
         * `python tools/hal_capabilities list --probe` reports a mismatch as a defect.
         *
         * @returns The set of plugin names this plugin depends on.
         */
        std::set<std::string> get_dependencies() const override;

        /**
         * Get this plugin's capability declaration as a JSON document.
         *
         * The string is `plugins/example_analysis/capabilities.json`, compiled in at configure time, so a
         * loaded plugin can be asked what it needs without anyone having to find the source tree.
         * See `tools/hal_capabilities` for the schema and the discovery command.
         *
         * @returns The contents of `capabilities.json`.
         */
        static std::string get_capabilities();
    };
}    // namespace hal
