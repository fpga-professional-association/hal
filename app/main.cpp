#include "hal_core/defines.h"
#include "hal_core/netlist/gate_library/gate_library_manager.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/netlist/netlist_factory.h"
#include "hal_core/netlist/netlist_parser/netlist_parser_manager.h"
#include "hal_core/netlist/netlist_writer/netlist_writer_manager.h"
#include "hal_core/netlist/persistent/netlist_serializer.h"
#include "hal_core/netlist/project_manager.h"
#include "hal_core/plugin_system/plugin_interface_base.h"
#include "hal_core/plugin_system/plugin_interface_ui.h"
#include "hal_core/plugin_system/cli_extension_interface.h"
#include "hal_core/plugin_system/plugin_manager.h"
#include "hal_core/utilities/log.h"
#include "hal_core/utilities/program_arguments.h"
#include "hal_core/utilities/program_options.h"
#include "hal_core/utilities/utils.h"
#include "hal_version.h"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>

#define SUCCESS 0
#define ERROR 1

using namespace hal;

int cleanup(int return_code = SUCCESS)
{
    if (!plugin_manager::unload_all_plugins())
    {
        return ERROR;
    }
    return return_code;
}

namespace
{
    /**
     * Hands control to the one UI plugin that was selected on the command line.
     *
     * `netlist` is whatever HAL loaded from `--project-dir` / `--import-netlist` / `--empty-project`
     * before this call, or `nullptr` when no project was requested. It is handed over through
     * UIPluginInterface::set_netlist so that an interactive frontend can put it in front of the user --
     * that is what lets a `--python-script` see a `netlist` it did not load itself. The netlist stays
     * owned by main(), which keeps it alive for the whole call.
     *
     * The result is the success flag of the run, as described on UIPluginInterface::exec.
     */
    bool execute_ui_plugin(UIPluginInterface* plugin, const std::string& plugin_name, ProgramArguments& args, Netlist* netlist)
    {
        ProgramArguments plugin_args;

        CliExtensionInterface* ceif = plugin->get_first_extension<CliExtensionInterface>();
        if (ceif)
        {
            for (const auto& option : ceif->get_cli_options().get_options())
            {
                auto flags      = std::get<0>(option);
                auto first_flag = *flags.begin();
                if (args.is_option_set(first_flag))
                {
                    plugin_args.set_option(first_flag, flags, args.get_parameters(first_flag));
                }
            }
        }

        log_info("core", "executing '{}' with", plugin_name);
        for (const auto& option : plugin_args.get_set_options())
        {
            log_info("core", "  '{}': {}", option, utils::join(",", plugin_args.get_parameters(option)));
        }

        /* add timestamp to log output */
        LogManager::get_instance()->set_format_pattern("[%d.%m.%Y %H:%M:%S] [%n] [%l] %v");

        plugin->set_netlist(netlist);

        /* UIPluginInterface::exec returns true when the requested work completed, so a failing
         * plugin -- a script that raised, a path that could not be read, a UI that did not start --
         * has to leave HAL with a nonzero exit code. */
        const bool succeeded = plugin->exec(args);

        if (!succeeded)
        {
            log_error("core", "execution of '{}' failed", plugin_name);
        }

        return succeeded;
    }
}    // namespace

void initialize_cli_options(ProgramOptions& cli_options)
{
    ProgramOptions generic_options("generic options");
    /* initialize generic options */
    generic_options.add({"-h", "--help"}, "print help messages");
    generic_options.add({"-v", "--version"}, "display the current version");
    generic_options.add({"-L", "--show-log-options"}, "show all logging options");
    generic_options.add({"-l", "--logfile"}, "specify log file name", {""});
    generic_options.add({"--log-time"}, "include time information into the log");
    generic_options.add({"--licenses"}, "show the licenses of all projects used by HAL");

    generic_options.add({"-i", "--import-netlist"}, "import a netlist into new project", {ProgramOptions::A_REQUIRED_PARAMETER});
    generic_options.add({"-p", "--project-dir"}, "load a HAL project from its directory", {ProgramOptions::A_REQUIRED_PARAMETER});
    generic_options.add({"-gl", "--gate-library"}, "specify the gate library to be used", {ProgramOptions::A_REQUIRED_PARAMETER});
    generic_options.add({"-e", "--empty-project"}, "create an empty project (requires gate library to be specified)");
    generic_options.add("--volatile-mode", "prevent HAL from creating a .hal progress file (e.g., for cluster use)");
    generic_options.add("--no-log", "prevent hal from creating a .log file");

    /* initialize netlist parser options */
    generic_options.add(netlist_parser_manager::get_cli_options());

    /* initialize netlist writer options */
    generic_options.add(netlist_writer_manager::get_cli_options());
    cli_options.add(generic_options);
}

int main(int argc, const char* argv[])
{
    /* initialize and parse generic cli options */
    ProgramOptions cli_options("cli options");
    initialize_cli_options(cli_options);
    ProgramOptions all_options("all options");
    all_options.add(cli_options);
    all_options.add(LogManager::get_instance()->get_option_descriptions());
    ProgramArguments args = all_options.parse(argc, argv);

    /* initialize logging */
    LogManager* lm = LogManager::get_instance();

    const char* info_channels[] = { "core", "stdout", "gate_library_parser", "gate_library_writer", "gate_library_manager", "gate_library",
                                    "netlist", "netlist_utils", "netlist_internal", "netlist_persistent",
                                    "gate", "net", "module", "grouping", "netlist_parser", "netlist_writer",
                                    "python_context", "event", nullptr};

    for (int i=0; info_channels[i]; i++)
        lm->add_channel(info_channels[i], {LogManager::create_stdout_sink(), LogManager::create_file_sink()}, "info");

    if (args.is_option_set("--logfile"))
    {
        lm->set_file_name(std::filesystem::path(args.get_parameter("--logfile")));
    }
    lm->handle_options(args);

    if (args.is_option_set("--log-time"))
    {
        lm->set_format_pattern("[%d.%m.%Y %H:%M:%S] [%n] [%l] %v");
    }

    /* initialize plugin manager */
    plugin_manager::add_existing_options_description(cli_options);

    // suppress output
    if (args.is_option_set("--help") || args.is_option_set("--licenses") || args.is_option_set("--version") || argc == 1)
    {
        lm->deactivate_all_channels();
    }

    // We need to check at an early stage (before CLI options are parsed) whether an already loaded UI plugin
    // (i.e., an interactive frontend such as the Python shell) will take control over HAL.
    //    if yes :    User determines which plugins get loaded
    //    if no  :    Need to load all plugins to have full range of CLI options available
    bool uictrl = false;
    auto ui_plugin_flags = plugin_manager::get_ui_plugin_flags();
    for (int i=1; i<argc; i++)
    {
        std::string option(argv[i]);
        auto it = ui_plugin_flags.find(option);
        if (it != ui_plugin_flags.end()) {
            uictrl = true;
            break;
        }
    }
    if (!uictrl && !plugin_manager::load_all_plugins())
    {
        // error loading all plugins
        return cleanup(ERROR);
    }

    /* add plugin cli options */
    auto options = plugin_manager::get_cli_plugin_options();
    if (!options.get_options().empty())
    {
        cli_options.add(plugin_manager::get_cli_plugin_options());
        all_options.add(plugin_manager::get_cli_plugin_options());
    }

    /* parse program options */
    bool unknown_option_exists = false;
    args                       = all_options.parse(argc, argv);
    // Check for unknown options include log manager options --> use all_options
    for (const auto& opt : all_options.get_unknown_arguments())
    {
        unknown_option_exists = true;
        log_error("core", "unkown command line argument '{}'", opt);
    }

    /* process help output */
    if (args.is_option_set("--help") || args.get_set_options().size() == 0 || unknown_option_exists)
    {
        std::cout << cli_options.get_options_string() << std::endl;
        return cleanup(unknown_option_exists ? ERROR : SUCCESS);
    }

    if (args.is_option_set("--version"))
    {
        std::cout << hal_version::version << std::endl;
        return cleanup();
    }

    if (args.is_option_set("--licenses"))
    {
        std::cout << utils::get_open_source_licenses() << std::endl;
        return cleanup();
    }

    /* find the ui plugin that will take control, if any */
    std::string ui_plugin_name;
    {
        std::vector<std::string> plugins_to_execute;
        auto ui_plugin_flags = plugin_manager::get_ui_plugin_flags();
        for (const auto& option : args.get_set_options())
        {
            auto it = ui_plugin_flags.find(option);
            if (it != ui_plugin_flags.end())
            {
                if (std::find(plugins_to_execute.begin(), plugins_to_execute.end(), it->second) == plugins_to_execute.end())
                {
                    plugins_to_execute.push_back(it->second);
                }
            }
        }

        if (plugins_to_execute.size() > 1)
        {
            log_error("core", "passed options for multiple ui plugins: {}", utils::join(", ", plugins_to_execute));
            return cleanup(ERROR);
        }
        else if (plugins_to_execute.size() == 1)
        {
            ui_plugin_name = plugins_to_execute[0];
        }
    }

    UIPluginInterface* ui_plugin = nullptr;
    if (!ui_plugin_name.empty())
    {
        ui_plugin = plugin_manager::get_plugin_instance<UIPluginInterface>(ui_plugin_name);
        if (ui_plugin == nullptr)
        {
            return cleanup(ERROR);
        }
    }

    /* A UI plugin flag no longer swallows the project arguments. When any of them is present the
     * project is opened and the netlist is loaded first -- exactly as on the plain CLI path -- and the
     * result is handed to the plugin, so that `--python-script` composes with `--project-dir`,
     * `--import-netlist` and `--gate-library` instead of running against nothing. A UI plugin invoked
     * without project arguments still takes over immediately and gets no netlist. */
    const bool project_requested = args.is_option_set("--project-dir") || args.is_option_set("--import-netlist") || args.is_option_set("--empty-project");

    if (ui_plugin != nullptr && !project_requested)
    {
        return cleanup(execute_ui_plugin(ui_plugin, ui_plugin_name, args, nullptr) ? SUCCESS : ERROR);
    }

    /**
     * control for command line interface
     */

    if (args.is_option_set("--show-log-options"))
    {
        std::cout << lm->get_option_descriptions().get_options_string() << std::endl;
        return cleanup();
    }

    /* empty project requires gate library, import or existing project args not allowed */
    /* every one of the checks below describes a run that cannot do what was asked, so it ends HAL with
     * a nonzero exit code -- the same contract the UI plugins follow (see UIPluginInterface::exec) */
    if (args.is_option_set("--empty-project") && args.is_option_set("--import-netlist"))
    {
        log_error("core", "Found --empty-project and --import-netlist!");
        return cleanup(ERROR);
    }

    if (args.is_option_set("--empty-project") && args.is_option_set("--project-dir"))
    {
        log_error("core", "Found --empty-project and --project-dir!");
        return cleanup(ERROR);
    }

    if (args.is_option_set("--empty-project") && !args.is_option_set("--gate-library"))
    {
        log_error("core", "Found --empty-project but --gate-library is missing!");
        return cleanup(ERROR);
    }

    std::filesystem::path proj_path;
    std::filesystem::path import_nl;
    bool openExisting = true;

    if (args.is_option_set("--empty-project"))
    {
        proj_path    = std::filesystem::path(args.get_parameter("--empty-project"));
        openExisting = false;
    }
    else if (args.is_option_set("--project-dir"))
    {
        proj_path = std::filesystem::path(args.get_parameter("--project-dir"));
    }
    if (args.is_option_set("--import-netlist"))
    {
        import_nl = std::filesystem::path(args.get_parameter("--import-netlist"));
        if (proj_path.empty())
            proj_path = import_nl;
        openExisting = false;
    }

    if (proj_path.string().empty())
    {
        log_error("core", "No hal project directory specified");
        return cleanup(ERROR);
    }

    ProjectManager* pm = ProjectManager::instance();
    if (openExisting)
    {
        if (!pm->open_project(proj_path.string()))
        {
            log_error("core", "Cannot open project <" + proj_path.string() + ">");
            return cleanup(ERROR);
        }
    }
    else
    {
        if (!pm->create_project_directory(proj_path.string()))
        {
            log_error("core", "Cannot create project <" + proj_path.string() + ">");
            return cleanup(ERROR);
        }
    }
    if (args.is_option_set("--no-log"))
    {
        log_warning("core",
                    "the console output will not be written to a local log "
                    "file (--no-log).");
    }
    else if (!args.is_option_set("--logfile"))
    {
        std::filesystem::path log_path = pm->get_project_directory().get_default_filename(".log");
        lm->set_file_name(log_path);
    }

    std::unique_ptr<Netlist> netlist;

    if (args.is_option_set("--empty-project"))
    {
        auto lib = gate_library_manager::load(args.get_parameter("--gate-library"));
        netlist  = netlist_factory::create_netlist(lib);
    }
    else
    {
        netlist = netlist_factory::load_netlist(pm->get_project_directory(), args);
    }

    if (netlist == nullptr)
    {
        return cleanup(ERROR);
    }

    bool volatile_mode = false;
    if (args.is_option_set("--volatile-mode"))
    {
        volatile_mode = true;
        log_warning("core", "your modifications will not be written to a .hal file (--volatile-mode).");
    }

    if (!import_nl.empty() && !volatile_mode)
    {
        pm->serialize_project(netlist.get());
    }

    /* redirect control to the ui plugin, now with the loaded netlist in hand */
    if (ui_plugin != nullptr)
    {
        if (!execute_ui_plugin(ui_plugin, ui_plugin_name, args, netlist.get()))
        {
            return cleanup(ERROR);
        }

        /* whatever the plugin did to the netlist is kept, just as it is for the cli plugins below */
        if (!volatile_mode)
        {
            pm->serialize_project(netlist.get());
        }

        if (!netlist_writer_manager::write(netlist.get(), args))
        {
            return cleanup(ERROR);
        }

        return cleanup();
    }

    /* cli plugins */
    std::vector<std::string> plugins_to_execute;
    auto option_to_plugin_name = plugin_manager::get_cli_plugin_flags();
    for (const auto& option : args.get_set_options())
    {
        auto it = option_to_plugin_name.find(option);
        if (it != option_to_plugin_name.end())
        {
            if (std::find(plugins_to_execute.begin(), plugins_to_execute.end(), it->second) == plugins_to_execute.end())
            {
                plugins_to_execute.push_back(it->second);
            }
        }
    }

    bool plugins_successful = true;
    for (const auto& plugin_name : plugins_to_execute)
    {
        BasePluginInterface* plugin = plugin_manager::get_plugin_instance(plugin_name);
        if (!plugin)
        {
            return cleanup(ERROR);
        }

        CliExtensionInterface* ceif = plugin_manager::get_first_extension<CliExtensionInterface>(plugin_name);
        if (!ceif)
        {
            return cleanup(ERROR);
        }

        ProgramArguments plugin_args;

        for (const auto& option : ceif->get_cli_options().get_options())
        {
            auto flags      = std::get<0>(option);
            auto first_flag = *flags.begin();
            if (args.is_option_set(first_flag))
            {
                plugin_args.set_option(first_flag, flags, args.get_parameters(first_flag));
            }
        }

        log_info("core", "executing '{}' with", plugin_name);
        for (const auto& option : plugin_args.get_set_options())
        {
            log_info("core", "  '{}': {}", option, utils::join(",", plugin_args.get_parameters(option)));
        }

        if (!ceif->handle_cli_call(netlist.get(), plugin_args))
        {
            plugins_successful = false;
            break;
        }
    }

    if (!plugins_successful)
    {
        return cleanup(ERROR);
    }

    if (!volatile_mode)
    {
        pm->serialize_project(netlist.get());
    }

    /* handle file writer */
    if (!netlist_writer_manager::write(netlist.get(), args))
    {
        return cleanup(ERROR);
    }

    /* cleanup */
    return cleanup();
}
