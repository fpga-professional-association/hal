#include "dataflow_analysis/plugin_dataflow.h"

#include "dataflow_analysis/common/grouping.h"
#include "dataflow_analysis/utils/timing_utils.h"
#include "hal_core/utilities/log.h"

#include <chrono>
#include <thread>

namespace hal
{
    extern std::unique_ptr<BasePluginInterface> create_plugin_instance()
    {
        return std::make_unique<DataflowPlugin>();
    }

    std::string DataflowPlugin::get_name() const
    {
        return std::string("dataflow");
    }

    std::string DataflowPlugin::get_version() const
    {
        return std::string("0.3");
    }

    std::string DataflowPlugin::get_description() const
    {
        return "Dataflow analysis tool DANA to recover word-level structures such as registers from gate-level netlists.";
    }

    std::set<std::string> DataflowPlugin::get_dependencies() const
    {
        return {};
    }

    DataflowPlugin::DataflowPlugin()
    {
        m_extensions.push_back(new CliExtensionDataflow());
    }

    ProgramOptions CliExtensionDataflow::get_cli_options() const
    {
        ProgramOptions description;

        description.add("--dataflow", "execute the dataflow plugin");

        description.add("--path", "specify output path", {""});

        description.add("--sizes", "(optional) specify sizes to be prioritized", {""});

        description.add("--bad_group_size", "(optional) specify the bad group size", {""});

        return description;
    }

    bool CliExtensionDataflow::handle_cli_call(Netlist* nl, ProgramArguments& args)
    {
        UNUSED(args);

        dataflow::Configuration config(nl);
        config.with_control_pin_types({PinType::clock, PinType::enable, PinType::reset, PinType::set});
        config.with_gate_types({GateTypeProperty::ff});

        std::string path;

        if (args.is_option_set("--path"))
        {
            if (args.get_parameter("--path").back() == '/')
                path = args.get_parameter("--path");
            else
                path = args.get_parameter("--path") + "/";
        }
        else
        {
            log_error("dataflow", "path parameter not set");
        }

        if (args.is_option_set("--sizes"))
        {
            std::istringstream f(args.get_parameter("--sizes"));
            std::string s;
            while (std::getline(f, s, ','))
            {
                config.expected_sizes.push_back(std::stoi(s));
            }
        }

        if (args.is_option_set("--bad_group_size"))
        {
            std::istringstream f(args.get_parameter("--bad_group_size"));
            std::string s;
            while (std::getline(f, s, ','))
            {
                config.min_group_size = std::stoi(s);
            }
        }

        auto grouping_res = dataflow::analyze(config);
        if (grouping_res.is_error())
        {
            log_error("dataflow", "dataflow analysis failed:\n{}", grouping_res.get_error().get());
            return false;
        }

        if (!path.empty())
        {
            auto grouping = grouping_res.get();
            if (const auto res = grouping.write_dot(path); res.is_error())
            {
                log_error("dataflow", "could not write .dot file:\n{}", res.get_error().get());
            }
            if (const auto res = grouping.write_txt(path); res.is_error())
            {
                log_error("dataflow", "could not write .txt file:\n{}", res.get_error().get());
            }
        }

        return true;
    }

}    // namespace hal
