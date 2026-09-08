#include "netlist_simulator_controller/simulation_engine.h"

#include "netlist_simulator_controller/netlist_simulator_controller.h"
#include "netlist_simulator_controller/simulation_process.h"
#include "netlist_simulator_controller/simulation_thread.h"

#include "hal_core/utilities/log.h"
#include "hal_core/utilities/utils.h"

#include <filesystem>
#include <fstream>

namespace hal
{
    SimulationEngine::SimulationEngine(const std::string& nam)
        : mName(nam), mRequireClockEvents(false), mCanShareMemory(false), mState(Preparing),
          mSimulationInput(nullptr)
    {;}

    std::string SimulationEngine::get_working_directory() const
    {
        return mWorkDir;
    }

    void SimulationEngine::set_working_directory(const std::string& workDir)
    {
        mWorkDir = workDir;
    }

    bool SimulationEngine::finalize()
    {
        mState = Done;
        return true;
    }

    void SimulationEngine::failed()
    {
        mState = Failed;
    }

    bool SimulationEngine::install_saleae_parser(std::string dirname) const
    {
        hal::error_code ec;
        std::filesystem::path dir(dirname);
        if (!std::filesystem::exists(dir, ec)) return false;

        std::filesystem::path sourceDir = utils::get_share_directory() / "saleae_parser";
        if (sourceDir.empty() || !std::filesystem::exists(sourceDir, ec))
        {
            log_warning("simulation_plugin", "Cannot find SALEAE parser sources in '{}', please check HAL installation.", sourceDir.string());
            return false;
        }

        const char* filenames[] = {"saleae_parser.h", "saleae_parser.cpp",
                                   "saleae_file.h", "saleae_file.cpp",
                                   "saleae_directory.h", "saleae_directory.cpp", nullptr};
        for (int i=0; filenames[i]; i++)
        {
            // add STANDALONE_PARSER preprocessor directive to all source files
            std::ifstream ff(sourceDir / filenames[i], std::ios::binary);
            if (!ff.good()) return false;
            std::ofstream of(dir / filenames[i], std::ios::binary);
            if (!of.good()) return false;
            of << "#define STANDALONE_PARSER 1\n";
            of << ff.rdbuf();
        }
        return true;
    }

    void SimulationEngine::set_engine_property(const std::string& key, const std::string& value)
    {
        mProperties[key] = value;
    }

    std::string SimulationEngine::get_engine_property(const std::string& key)
    {
        if (mProperties.find(key) != mProperties.end())
        {
            return mProperties[key];
        }

        return std::string();
    }

    SimulationEngineEventDriven::SimulationEngineEventDriven(const std::string& nam) : SimulationEngine(nam)
    {
        mCanShareMemory = true;
    }

    std::vector<WaveEvent> SimulationEngineEventDriven::get_simulation_events(u32 netId) const
    {
        UNUSED(netId);
        return std::vector<WaveEvent>();
    }

    bool SimulationEngineEventDriven::setSimulationInput(SimulationInput* simInput)
    {
        mSimulationInput = simInput;
        return true;
    }

    bool SimulationEngineEventDriven::run(NetlistSimulatorController* controller, SimulationLogReceiver *logReceiver)
    {
        UNUSED(logReceiver);
        SimulationThread* thread = new SimulationThread(controller, mSimulationInput, this);
        mState                   = Running;
        thread->start();
        return true;
    }

    bool SimulationEngineScripted::run(NetlistSimulatorController* controller, SimulationLogReceiver *logReceiver)
    {
        SimulationProcess* proc = new SimulationProcess(controller, this);
        proc->log()->setLogReceiver(logReceiver);
        mState                  = Running;
        proc->start();
        return true;
    }

    //======================= FACTORY ================================
    SimulationEngineFactory::SimulationEngineFactory(const std::string& nam) : mName(nam)
    {
        SimulationEngineFactories::instance()->push_back(this);
    }

    SimulationEngineFactories* SimulationEngineFactories::inst = nullptr;

    SimulationEngineFactories* SimulationEngineFactories::instance()
    {
        if (!inst)
            inst = new SimulationEngineFactories;
        return inst;
    }

    std::vector<std::string> SimulationEngineFactories::factoryNames() const
    {
        std::vector<std::string> retval;
        for (auto it = begin(); it != end(); ++it)
            retval.push_back((*it)->name());
        return retval;
    }

    SimulationEngineFactory* SimulationEngineFactories::factoryByName(const std::string nam) const
    {
        for (auto it = begin(); it != end(); ++it)
            if ((*it)->name() == nam)
                return (*it);

        return nullptr;
    }

    void SimulationEngineFactories::deleteFactory(const std::string nam)
    {
        auto it = begin();
        while (it != end())
        {
            if ((*it)->name() == nam)
            {
                delete (*it);
                it = erase(it);
            }
            else
                ++it;
        }
    }

}    // namespace hal
