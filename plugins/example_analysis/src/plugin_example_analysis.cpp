#include "example_analysis/plugin_example_analysis.h"

#include "example_analysis/capabilities_generated.h"

namespace hal
{
    extern std::unique_ptr<BasePluginInterface> create_plugin_instance()
    {
        return std::make_unique<ExampleAnalysisPlugin>();
    }

    std::string ExampleAnalysisPlugin::get_name() const
    {
        return std::string("example_analysis");
    }

    std::string ExampleAnalysisPlugin::get_version() const
    {
        return std::string("0.1");
    }

    std::string ExampleAnalysisPlugin::get_description() const
    {
        return std::string("Group sequential gates by the net that drives their clock pin (reference output of tools/new_plugin.py).");
    }

    std::set<std::string> ExampleAnalysisPlugin::get_dependencies() const
    {
        // Keep in sync with dependencies.plugins in capabilities.json.
        return {};
    }

    std::string ExampleAnalysisPlugin::get_capabilities()
    {
        return std::string(example_analysis::CAPABILITIES_JSON);
    }
}    // namespace hal
