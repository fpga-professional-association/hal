#include "module_identification/plugin_module_identification.h"

#include "hal_core/defines.h"
#include "hal_core/utilities/result.h"
#include "module_identification/api/configuration.h"
#include "module_identification/api/module_identification.h"
#include "module_identification/api/result.h"

#include <algorithm>
#include <fstream>
#include <iostream>

namespace hal
{
    ModuleIdentificationPlugin::ModuleIdentificationPlugin()
    {
    }

    extern std::unique_ptr<BasePluginInterface> create_plugin_instance()
    {
        return std::make_unique<ModuleIdentificationPlugin>();
    }

    std::string ModuleIdentificationPlugin::get_name() const
    {
        return std::string("module_identification");
    }

    std::string ModuleIdentificationPlugin::get_version() const
    {
        return std::string("0.1");
    }

    std::string ModuleIdentificationPlugin::get_description() const
    {
        return std::string("Plugin for module classification against a library of predefined types.");
    }

    std::set<std::string> ModuleIdentificationPlugin::get_dependencies() const
    {
        std::set<std::string> retval;
        retval.insert("boolean_influence");
        retval.insert("z3_utils");
        return retval;
    }

}    // namespace hal