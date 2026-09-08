#include "netlist_simulator_controller/plugin_netlist_simulator_controller.h"
#include "netlist_simulator_controller/simulation_settings.h"
#include "hal_core/netlist/netlist_writer/netlist_writer_manager.h"
#include "netlist_simulator_controller/netlist_simulator_controller.h"
#include "hal_core/plugin_system/plugin_manager.h"
#include "hal_core/utilities/json_write_document.h"
#include "hal_core/utilities/log.h"
#include "hal_core/utilities/utils.h"
#include "hal_core/netlist/project_manager.h"
#include "rapidjson/document.h"
#include "rapidjson/filereadstream.h"

#include <filesystem>
#include <stdio.h>

namespace hal
{
    u32 NetlistSimulatorControllerPlugin::sMaxControllerId = 0;
    SimulationSettings* NetlistSimulatorControllerPlugin::sSimulationSettings = nullptr;
    SimulatorSerializer* NetlistSimulatorControllerPlugin::sSimulatorSerializer = nullptr;

    SimulatorSerializer::SimulatorSerializer()
        : ProjectSerializer("simulator"), mNetlist(nullptr)
    {;}

    std::string SimulatorSerializer::serialize(Netlist* netlist, const std::filesystem::path& savedir, bool isAutosave)
    {
        UNUSED(netlist);
        UNUSED(isAutosave);
        std::string simFilename("simulator.json");

        JsonWriteDocument jwd;
        JsonWriteArray& simArr = jwd.add_array("simulator");

        for (NetlistSimulatorController* ctrl : NetlistSimulatorControllerMap::instance()->toList())
        {
            JsonWriteObject& simEntry = simArr.add_object();
            simEntry["id"]            = (int) ctrl->get_id();
            simEntry["name"]          = ctrl->name();
            std::filesystem::path relProjdir = ProjectManager::instance()->get_project_directory().get_relative_file_path(ctrl->get_working_directory());
            simEntry["workdir"]       = relProjdir.string();
            simEntry.close();
        }
        simArr.close();

        if (!jwd.serialize((savedir / simFilename).string()))
        {
            return std::string();
        }

        return simFilename;
    }

    void SimulatorSerializer::deserialize(Netlist* netlist, const std::filesystem::path& loaddir)
    {
        mNetlist = netlist;
        if (!loaddir.empty())
            mProjDir = loaddir;
        NetlistSimulatorControllerMap::instance()->clearAll();
    }

    std::vector<std::unique_ptr<NetlistSimulatorController>> SimulatorSerializer::restore()
    {
        std::vector<std::unique_ptr<NetlistSimulatorController>> retval;
        ProjectManager* pm = ProjectManager::instance();
        std::string relname = pm->get_filename(m_name);
        if (relname.empty()) return retval;

        NetlistSimulatorControllerPlugin* ctrlPlug = static_cast<NetlistSimulatorControllerPlugin*>(plugin_manager::get_plugin_instance("netlist_simulator_controller"));
        if (!ctrlPlug) return retval;
        if (mProjDir.empty())
            mProjDir = pm->get_project_directory();

        std::filesystem::path simFilename = mProjDir / relname;

        FILE* ff = fopen(simFilename.string().c_str(), "rb");
        if (!ff) return retval;

        char buffer[65536];
        rapidjson::FileReadStream frs(ff, buffer, sizeof(buffer));
        rapidjson::Document document;
        document.ParseStream<0, rapidjson::UTF8<>, rapidjson::FileReadStream>(frs);
        fclose(ff);

        if (document.HasParseError() || !document.HasMember("simulator") || !document["simulator"].IsArray())
        {
            return retval;
        }

        for (auto& jsim : document["simulator"].GetArray())
        {
            if (!jsim.HasMember("workdir")) continue;
            std::string workdir = jsim["workdir"].GetString();
            if (workdir.empty()) continue;
            std::filesystem::path workdirPath(workdir);
            if (workdirPath.is_relative())
            {
                workdirPath = ProjectManager::instance()->get_project_directory().get_filename(workdir);
            }
            std::filesystem::path contrFile = workdirPath / "netlist_simulator_controller.json";
            retval.push_back(ctrlPlug->restore_simulator_controller(mNetlist, contrFile.string()));
        }

        return retval;
    }

    extern std::unique_ptr<BasePluginInterface> create_plugin_instance()
    {
        return std::make_unique<NetlistSimulatorControllerPlugin>();
    }

    std::string NetlistSimulatorControllerPlugin::get_name() const
    {
        return std::string("netlist_simulator_controller");
    }

    std::string NetlistSimulatorControllerPlugin::get_version() const
    {
        return std::string("0.7");
    }

    std::string NetlistSimulatorControllerPlugin::get_description() const
    {
        return std::string("Non-GUI base plugin to control simulation");
    }

    std::unique_ptr<NetlistSimulatorController> NetlistSimulatorControllerPlugin::create_simulator_controller(const std::string &nam, const std::string &workdir) const
    {
        NetlistSimulatorController* nsc = new NetlistSimulatorController(++sMaxControllerId, nam, workdir);
        if (!nsc->is_legal_directory_name())
        {
            log_warning("simulation_plugin", "Invalid simulator working directory '{}' (hint: avoid spaces)", nsc->get_working_directory());
            delete nsc;
            return nullptr;
        }
        return std::unique_ptr<NetlistSimulatorController>(nsc);
    }

    std::unique_ptr<NetlistSimulatorController> NetlistSimulatorControllerPlugin::restore_simulator_controller(Netlist* nl, const std::string &filename) const
    {
        NetlistSimulatorController* nsc = new NetlistSimulatorController(++sMaxControllerId,nl,filename);
        if (nsc->get_working_directory().empty())
        {
            delete nsc;
            return nullptr;
        }
        return std::unique_ptr<NetlistSimulatorController>(nsc);
    }

    std::shared_ptr<NetlistSimulatorController> NetlistSimulatorControllerPlugin::simulator_controller_by_id(u32 id) const
    {
        NetlistSimulatorController* ctrl = NetlistSimulatorControllerMap::instance()->controller(id);
        if (!ctrl)
        {
            log_warning("simulation_plugin", "Simulation controller with ID={} not found in memory, will return nullptr", id);
            return nullptr;
        }
        return std::shared_ptr<NetlistSimulatorController>(ctrl,[](void*){;});
    }

    void NetlistSimulatorControllerPlugin::on_unload()
    {
        NetlistSimulatorControllerMap::instance()->shutdown();
        if (sSimulationSettings) sSimulationSettings->sync();
        if (sSimulatorSerializer) delete sSimulatorSerializer;
    }

    void NetlistSimulatorControllerPlugin::on_load()
    {
        // report simulation warnings and error messages not related to specific controller to common channel
        LogManager::get_instance()->add_channel("simulation_plugin", {LogManager::create_stdout_sink(), LogManager::create_file_sink()}, "info");
        LogManager::get_instance()->add_channel("waveform", {LogManager::create_stdout_sink(), LogManager::create_file_sink()}, "info");
        std::filesystem::path userConfigDir = utils::get_user_config_directory();
        sSimulationSettings = new SimulationSettings((userConfigDir / "simulationsettings.ini").string());
        sSimulatorSerializer = new SimulatorSerializer;
    }

}    // namespace hal
