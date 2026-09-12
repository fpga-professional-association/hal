#include "netlist_simulator_controller/netlist_simulator_controller.h"

#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/gate_library/enums/pin_direction.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/netlist/project_manager.h"
#include "hal_core/utilities/json_write_document.h"
#include "hal_core/utilities/log.h"
#include "netlist_simulator_controller/plugin_netlist_simulator_controller.h"
#include "netlist_simulator_controller/saleae_directory.h"
#include "netlist_simulator_controller/saleae_parser.h"
#include "netlist_simulator_controller/simulation_engine.h"
#include "netlist_simulator_controller/simulation_input.h"
#include "netlist_simulator_controller/simulation_settings.h"
#include "netlist_simulator_controller/vcd_serializer.h"
#include "netlist_simulator_controller/wave_data.h"
#include "netlist_simulator_controller/string_utils.h"
#include "rapidjson/document.h"
#include "rapidjson/filereadstream.h"

#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <set>
#include <vector>

namespace hal
{
    const char* NetlistSimulatorController::sPersistFile = "netlist_simulator_controller.json";

    namespace
    {
        /**
         * Create a unique temporary directory from a template ending in `XXXXXX`, replacement for QTemporaryDir.
         * @return Path of the created directory, empty string if creation failed.
         */
        std::string createTemporaryDirectory(const std::string& templatePath)
        {
            std::string tmpl = templatePath;
            if (!simutil::ends_with(tmpl, "XXXXXX"))
            {
                tmpl += "XXXXXX";
            }
            if (std::filesystem::path(tmpl).is_relative())
            {
                hal::error_code ec;
                std::filesystem::path tmpdir = std::filesystem::temp_directory_path(ec);
                if (!ec)
                {
                    tmpl = (tmpdir / tmpl).string();
                }
            }
            std::vector<char> buffer(tmpl.begin(), tmpl.end());
            buffer.push_back(0);
            const char* created = mkdtemp(buffer.data());
            if (!created)
            {
                log_warning("simulation_plugin", "Cannot create temporary working directory '{}'.", tmpl);
                return std::string();
            }
            return std::string(created);
        }
    }    // namespace

    NetlistSimulatorController::NetlistSimulatorController(u32 id, const std::string nam, const std::string& workdir)
        : mId(id), mName(nam), mState(NoGatesSelected), mSimulationEngine(nullptr), mWaveDataList(nullptr),
          mSimulationInput(new SimulationInput), mLogReceiver(nullptr)
    {
        if (mName.empty())
        {
            mName = "sim_controller" + std::to_string(mId);
        }
        LogManager::get_instance()->add_channel(mName, {LogManager::create_stdout_sink(), LogManager::create_file_sink()}, "info");

        if (workdir.empty())
        {
            std::string templatePath = NetlistSimulatorControllerPlugin::sSimulationSettings->baseDirectory().empty()
                                           ? ProjectManager::instance()->get_project_directory().string()
                                           : NetlistSimulatorControllerPlugin::sSimulationSettings->baseDirectory();
            if (!templatePath.empty())
            {
                templatePath += '/';
            }
            templatePath += "hal_simulation_" + mName + "_XXXXXX";
            mWorkDir = createTemporaryDirectory(templatePath);
        }
        else
        {
            mWorkDir = workdir;
        }
        hal::error_code ec;
        std::filesystem::path saleaeDir = std::filesystem::path(mWorkDir) / "saleae";
        std::filesystem::create_directories(saleaeDir, ec);
        std::string saleaeDirectoryFilename = (saleaeDir / "saleae.json").string();
        if (!std::filesystem::exists(saleaeDirectoryFilename, ec))
        {
            std::ofstream of(saleaeDirectoryFilename, std::ios::binary);
            if (of.good())
            {
                of << "{\"saleae\":{}}";
            }
        }
        mWaveDataList = new WaveDataList(saleaeDirectoryFilename);

        NetlistSimulatorControllerMap::instance()->addController(this);
    }

    NetlistSimulatorController::NetlistSimulatorController(u32 id, Netlist* nl, const std::string& filename)
        : mId(id), mState(NoGatesSelected), mSimulationEngine(nullptr), mWaveDataList(nullptr), mSimulationInput(new SimulationInput), mLogReceiver(nullptr)
    {
        FILE* ff = fopen(filename.c_str(), "rb");
        if (!ff)
        {
            log_warning("simulation_plugin", "Error opening file '{}'.", filename);
            return;
        }

        char buffer[65536];
        rapidjson::FileReadStream frs(ff, buffer, sizeof(buffer));
        rapidjson::Document document;
        document.ParseStream<0, rapidjson::UTF8<>, rapidjson::FileReadStream>(frs);
        fclose(ff);

        if (document.HasParseError() || !document.HasMember("netlist_simulator_controller"))
        {
            log_warning("simulation_plugin", "Cannot restore simulation controller from file '{}'.", filename);
            return;
        }
        auto jnsc = document["netlist_simulator_controller"].GetObject();
        if (jnsc.HasMember("name"))
        {
            mName = jnsc["name"].GetString();
        }

        std::filesystem::path workDir = std::filesystem::path(filename).parent_path();
        std::vector<Gate*> simulatedGates;
        if (jnsc.HasMember("gates"))
        {
            for (auto& jgate : jnsc["gates"].GetArray())
            {
                u32 gateId = jgate.HasMember("id") ? jgate["id"].GetUint() : 0;
                Gate* g    = nl->get_gate_by_id(gateId);
                if (!g)
                {
                    log_warning("simulation_plugin", "Simulated gate ID={} not found in netlist.", gateId);
                    return;
                }
                if (jgate.HasMember("name") && jgate["name"].GetString() != g->get_name() && g->get_name().find("UNKNOWN") != 0)
                {
                    log_warning("simulation_plugin", "Gate name for ID={} differs in simulation '{}' and netlist '{}'", gateId, jgate["name"].GetString(), g->get_name());
                    return;
                }
                simulatedGates.push_back(g);
            }
        }
        LogManager::get_instance()->add_channel(mName, {LogManager::create_stdout_sink(), LogManager::create_file_sink()}, "info");
        hal::error_code ec;
        std::filesystem::path saleaeDir = workDir / "saleae";
        std::filesystem::create_directories(saleaeDir, ec);
        mWaveDataList = new WaveDataList((saleaeDir / "saleae.json").string());

        SaleaeDirectoryStoreRequest::sWriteDisabled = true;
        mWaveDataList->updateFromSaleae();
        mSimulationInput->add_gates(simulatedGates);
        mWorkDir = workDir.string();
        restoreComposed(mWaveDataList->saleaeDirectory());
        SaleaeDirectoryStoreRequest::sWriteDisabled = false;

        if (jnsc.HasMember("clocks"))
        {
            for (auto& jclock : jnsc["clocks"].GetArray())
            {
                u32 clkId   = jclock.HasMember("id") ? jclock["id"].GetUint() : 0;
                Net* clkNet = nl->get_net_by_id(clkId);
                if (!clkNet)
                {
                    log_warning(mName, "Clock net ID={} not found in netlist.", clkId);
                    continue;
                }
                bool startAtZero = jclock.HasMember("start_value") ? (jclock["start_value"].GetInt() == 0) : true;
                int period       = jclock.HasMember("switch_time") ? jclock["switch_time"].GetInt() * 2 : 1000;
                add_clock_period(clkNet, period, startAtZero, mWaveDataList->timeFrame().simulateMaxTime());
            }
        }

        if (jnsc.HasMember("engine"))
        {
            auto jengine = jnsc["engine"].GetObject();
            if (jengine.HasMember("name"))
            {
                create_simulation_engine(jengine["name"].GetString());
                if (mSimulationEngine && jengine.HasMember("properties"))
                {
                    auto jprop = jengine["properties"].GetObject();

                    for (auto it = jprop.MemberBegin(); it != jprop.MemberEnd(); ++it)
                    {
                        mSimulationEngine->set_engine_property(it->name.GetString(), it->value.GetString());
                    }
                }
            }
        }
        NetlistSimulatorControllerMap::instance()->addController(this);
    }

    NetlistSimulatorController::~NetlistSimulatorController()
    {
        NetlistSimulatorControllerMap::instance()->removeController(mId);
        if (mWaveDataList)
        {
            delete mWaveDataList;
        }
        delete mSimulationInput;
        //  delete mTempDir;
    }

    void NetlistSimulatorController::setLogReceiver(SimulationLogReceiver* logrec)
    {
        mLogReceiver = logrec;
    }

    void NetlistSimulatorController::setState(SimulationState stat)
    {
        if (stat == mState)
        {
            return;
        }
        mState = stat;
        switch (mState)
        {
            case NoGatesSelected:
                log_info(get_name(), "Select gates for simulation");
                break;
            case ParameterSetup:
                log_info(get_name(), "Expecting parameter and input");
                break;
            case ParameterReady:
                log_info(get_name(), "Preconditions to start simulation met");
                break;
            case SimulationRun:
                log_info(get_name(), "Running simulation, please wait...");
                break;
            case ShowResults:
                log_info(get_name(), "Simulation engine completed successfully");
                break;
            case EngineFailed:
                log_info(get_name(), "Simulation engine process error");
                if (mSimulationEngine)
                {
                    mSimulationEngine->failed();
                }
                break;
        }
    }

    std::string NetlistSimulatorController::get_working_directory() const
    {
        return mWorkDir;
    }

    bool NetlistSimulatorController::is_legal_directory_name() const
    {
        if (mWorkDir.find(' ') != std::string::npos)
        {
            return false;
        }
        return true;
    }

    u64 NetlistSimulatorController::get_max_simulated_time() const
    {
        if (!mWaveDataList)
        {
            return 0;
        }
        return mWaveDataList->timeFrame().simulateMaxTime();
    }

    std::filesystem::path NetlistSimulatorController::get_saleae_directory_filename() const
    {
        return std::filesystem::path(mWaveDataList->saleaeDirectory().get_filename());
    }

    SimulationEngine* NetlistSimulatorController::create_simulation_engine(const std::string& name)
    {
        SimulationEngineFactory* fac = SimulationEngineFactories::instance()->factoryByName(name);
        if (!fac)
        {
            return nullptr;
        }
        if (mSimulationEngine)
        {
            delete mSimulationEngine;
        }
        mSimulationEngine = fac->createEngine();
        mSimulationEngine->set_working_directory(get_working_directory());
        log_info(get_name(), "Engine '{}' created. Work directory set to '{}'.", mSimulationEngine->name(), get_working_directory());
        checkReadyState();
        return mSimulationEngine;
    }

    SimulationEngine* NetlistSimulatorController::get_simulation_engine() const
    {
        return mSimulationEngine;
    }

    std::vector<std::string> NetlistSimulatorController::get_engine_names() const
    {
        return SimulationEngineFactories::instance()->factoryNames();
    }

    void NetlistSimulatorController::initSimulator()
    {
    }

    void NetlistSimulatorController::set_no_clock_used()
    {
        mSimulationInput->set_no_clock_used();
        checkReadyState();
    }

    bool NetlistSimulatorController::is_no_clock_used() const
    {
        return mSimulationInput->is_no_clock_used();
    }

    void NetlistSimulatorController::simulate_only_probes(const std::vector<const Net*>& probes)
    {
        for (const Net* n : probes)
        {
            mSimulateOnlyProbes.insert(n->get_id());
        }
    }

    void NetlistSimulatorController::simulate_only_probes(const std::set<u32>& probes)
    {
        mSimulateOnlyProbes = probes;
    }

    u32 NetlistSimulatorController::add_trigger_time(const std::vector<WaveData*>& trigger_waves, const std::vector<int>& trigger_on_values)
    {
        if (trigger_waves.empty())
        {
            return 0;
        }
        std::vector<WaveData*> triglist;
        std::vector<int> trigOnVal;
        for (WaveData* wd : trigger_waves)
        {
            triglist.push_back(wd);
        }
        for (int tov : trigger_on_values)
        {
            trigOnVal.push_back(tov);
        }
        WaveDataTrigger* wdTrig = new WaveDataTrigger(mWaveDataList, triglist, trigOnVal);
        if (!wdTrig)
        {
            return 0;
        }
        return wdTrig->id();
    }

    u32 NetlistSimulatorController::add_boolean_expression_waveform(const std::string& expression)
    {
        if (expression.empty())
        {
            return 0;
        }
        WaveDataBoolean* wdBool = new WaveDataBoolean(mWaveDataList, expression);
        if (!wdBool)
        {
            return 0;
        }
        return wdBool->id();
    }

    u32 NetlistSimulatorController::add_boolean_accept_list_waveform(const std::vector<WaveData*>& input_waves, const std::vector<int>& accepted_combination)
    {
        if (input_waves.empty() || accepted_combination.empty())
        {
            return 0;
        }
        std::vector<WaveData*> inpWaves;
        std::vector<int> acceptVal;
        for (WaveData* wd : input_waves)
        {
            inpWaves.push_back(wd);
        }
        for (int acc : accepted_combination)
        {
            acceptVal.push_back(acc);
        }
        WaveDataBoolean* wdBool = new WaveDataBoolean(mWaveDataList, inpWaves, acceptVal);
        if (!wdBool)
        {
            return 0;
        }
        return wdBool->id();
    }

    u32 NetlistSimulatorController::add_waveform_group(const std::string& name, const std::vector<Net*>& nets)
    {
        if (name.empty())
        {
            return 0;
        }
        std::vector<WaveData*> waveVector;
        waveVector.reserve(nets.size());
        for (Net* n : nets)
        {
            WaveData* wd = get_waveform_by_net(n);
            if (!wd)
            {
                log_warning(get_name(), "Cannot add unkown waveform for net '{}(id={})' to group '{}'.", n->get_name(), n->get_id(), name);
                continue;
            }
            waveVector.push_back(wd);
        }

        WaveDataGroup* wdGrp = new WaveDataGroup(mWaveDataList, name);
        if (!waveVector.empty())
        {
            mWaveDataList->addWavesToGroup(wdGrp->id(), waveVector);
        }
        return wdGrp->id();
    }

    u32 NetlistSimulatorController::add_waveform_group(const PinGroup<ModulePin>* pin_group)
    {
        if (!pin_group)
        {
            log_warning(get_name(), "Invalid 'add_waveform_group' call with nullptr instead of module pin group");
            return 0;
        }
        std::vector<Net*> nets;
        for (const auto pin : pin_group->get_pins())
        {
            nets.push_back(pin->get_net());
        }

        return add_waveform_group(pin_group->get_name(), nets);
    }

    u32 NetlistSimulatorController::add_waveform_group(const Gate* gate, const PinGroup<GatePin>* pin_group)
    {
        if (!gate || !pin_group)
        {
            log_warning(get_name(), "Invalid 'add_waveform_group' call with nullptr instead of {}", (gate ? "gate pin group" : "gate"));
            return 0;
        }
        std::vector<Net*> nets;
        for (const auto pin : pin_group->get_pins())
        {
            Net* n = nullptr;
            switch (pin->get_direction())
            {
                case PinDirection::input:
                    n = gate->get_fan_in_net(pin);
                    break;
                case PinDirection::output:
                    n = gate->get_fan_out_net(pin);
                    break;
                default:
                    break;
            }

            if (n)
            {
                nets.push_back(n);
            }
        }

        return add_waveform_group(pin_group->get_name(), nets);
    }

    u32 NetlistSimulatorController::add_waveform_group(const std::string& name, const PinGroup<ModulePin>* pin_group)
    {
        if (!pin_group)
        {
            log_warning(get_name(), "Invalid 'add_waveform_group' call with name='{}' and nullptr instead of module pin group", name);
            return 0;
        }
        std::vector<Net*> nets;
        for (const auto pin : pin_group->get_pins())
        {
            nets.push_back(pin->get_net());
        }

        return add_waveform_group((name.empty() ? pin_group->get_name() : name), nets);
    }

    void NetlistSimulatorController::remove_waveform_group(u32 group_id)
    {
        mWaveDataList->removeGroup(group_id);
    }

    void NetlistSimulatorController::handleOpenInputFile(const std::string& filename)
    {
        if (filename.empty())
        {
            return;
        }
        VcdSerializer reader(mWorkDir, false, this);
        std::vector<const Net*> onlyNets;
        for (const Net* n : mSimulationInput->get_input_nets())
        {
            onlyNets.push_back(n);
        }
        if (reader.importVcd(filename, mWorkDir, onlyNets))
        {
            mWaveDataList->updateFromSaleae();
        }
        checkReadyState();
    }

    bool NetlistSimulatorController::persist() const
    {
        JsonWriteDocument jwd;
        JsonWriteObject& jnsc = jwd.add_object("netlist_simulator_controller");
        jnsc["id"]            = (int)get_id();
        jnsc["name"]          = get_name();
        /*       jnsc["workdir"] = get_working_directory();*/

        JsonWriteArray& jgates = jnsc.add_array("gates");
        for (const Gate* g : mSimulationInput->get_gates())
        {
            JsonWriteObject& jgate = jgates.add_object();
            jgate["id"]            = (int)g->get_id();
            jgate["name"]          = g->get_name();
            jgate.close();
        }
        jgates.close();

        JsonWriteArray& jclocks = jnsc.add_array("clocks");
        for (const SimulationInput::Clock& clk : mSimulationInput->get_clocks())
        {
            JsonWriteObject& jclock = jclocks.add_object();
            jclock["id"]            = (int)clk.clock_net->get_id();
            jclock["name"]          = clk.clock_net->get_name();
            jclock["switch_time"]   = (int)clk.switch_time;
            jclock["start_value"]   = clk.start_at_zero ? 0 : 1;
            jclock.close();
        }
        jclocks.close();

        if (mSimulationEngine)
        {
            JsonWriteObject& jengine = jnsc.add_object("engine");
            jengine["name"]          = mSimulationEngine->name();
            JsonWriteObject& jprops  = jengine.add_object("properties");
            for (auto it = mSimulationEngine->get_engine_properties().begin(); it != mSimulationEngine->get_engine_properties().end(); ++it)
            {
                jprops[it->first] = it->second;
            }
            jprops.close();
            jengine.close();
        }

        jnsc.close();
        return jwd.serialize((std::filesystem::path(mWorkDir) / sPersistFile).string());
    }

    bool NetlistSimulatorController::run_simulation()
    {
        if (!mSimulationEngine)
        {
            log_warning(get_name(), "no simulation engine selected");
            return false;
        }

        std::map<std::string, std::string> engPropMap = NetlistSimulatorControllerPlugin::sSimulationSettings->engineProperties();

        if (!engPropMap.empty())
        {
            bool engPropMapModified = false;
            for (auto it = engPropMap.begin(); it != engPropMap.end(); ++it)
            {
                std::string prop = it->first;
                std::string valu = it->second;

                std::string userAssignedValue = mSimulationEngine->get_engine_property(prop);
                if (userAssignedValue.empty())
                {
                    log_info(get_name(), "Engine property '{}' set to '{}'.", prop, valu);
                    mSimulationEngine->set_engine_property(prop, valu);
                }
                else
                {
                    if (userAssignedValue != valu)
                    {
                        log_info(get_name(), "Default value for engine property '{}' changed from '{}' to '{}'.", prop, valu, userAssignedValue);
                        it->second         = userAssignedValue;
                        engPropMapModified = true;
                    }
                }
            }
            if (engPropMapModified)
            {
                NetlistSimulatorControllerPlugin::sSimulationSettings->setEngineProperties(engPropMap);
                NetlistSimulatorControllerPlugin::sSimulationSettings->sync();
            }
        }

        if (mState != ParameterReady)
        {
            log_warning(get_name(), "wrong state {}.", (u32)mState);
            return false;
        }

        mWaveDataList->setValueForEmpty(0);
        mWaveDataList->emitTimeframeChanged();

        for (auto it = mBadAssignInputWarnings.cbegin(); it != mBadAssignInputWarnings.cend(); ++it)
        {
            if (it->second > 3)
            {
                log_warning(get_name(), "Totally {} attempts to set input values for net ID={}, but net is not an input.", it->second, it->first);
            }
        }

        // (Re)generate the clock waveforms now that the length of the simulation is known. Every engine
        // needs this, not just the ones that want the clock as regular input events: the waveform is
        // what the simulation thread replays, so a clock that stops early stops the whole run there.
        // A duration that was passed to add_clock_period() explicitly still wins.
        for (const Net* n : mSimulationInput->get_input_nets())
        {
            if (!mSimulationInput->is_clock(n))
            {
                continue;
            }

            const SimulationInput::Clock* clk = nullptr;
            for (const SimulationInput::Clock& testClk : mSimulationInput->get_clocks())
            {
                if (testClk.clock_net == n)
                {
                    clk = &testClk;
                    break;
                }
            }
            if (!clk)
            {
                // is_clock() and get_clocks() disagree; generating a clock from a default-constructed
                // Clock would divide the period by zero and never terminate.
                log_warning(get_name(), "No clock settings found for clock net[{}] '{}', clock waveform not generated.", n->get_id(), n->get_name());
                continue;
            }

            u64 tmax = mWaveDataList->timeFrame().sceneMaxTime();
            if (auto it = mClockDurations.find(n->get_id()); it != mClockDurations.end() && it->second)
            {
                tmax = it->second;
            }

            WaveDataClock* wdc = new WaveDataClock(n, *clk, tmax);
            mWaveDataList->addOrReplace(wdc);
        }

        persist();

        if (!mSimulationEngine->setSimulationInput(mSimulationInput))
        {
            log_warning(get_name(), "simulation engine error during setup.");
            setState(EngineFailed);
            return false;
        }

        // start simulation process (might be external process)
        if (!mSimulationEngine->run(this, mLogReceiver))
        {
            log_warning(get_name(), "simulation engine error during startup.");
            setState(EngineFailed);
            return false;
        }
        setState(SimulationRun);
        return true;
    }

    WaveData* NetlistSimulatorController::get_waveform_by_net(const Net* n) const
    {
        mWaveDataList->triggerAddToView(n->get_id());
        return mWaveDataList->waveDataByNet(n);
    }

    WaveDataGroup* NetlistSimulatorController::get_waveform_group_by_id(u32 id) const
    {
        return simutil::map_value(mWaveDataList->mDataGroups, id, (WaveDataGroup*)nullptr);
    }

    WaveDataBoolean* NetlistSimulatorController::get_waveform_boolean_by_id(u32 id) const
    {
        return simutil::map_value(mWaveDataList->mDataBooleans, id, (WaveDataBoolean*)nullptr);
    }

    WaveDataTrigger* NetlistSimulatorController::get_trigger_time_by_id(u32 id) const
    {
        return simutil::map_value(mWaveDataList->mDataTrigger, id, (WaveDataTrigger*)nullptr);
    }

    void NetlistSimulatorController::rename_waveform(WaveData* wd, std::string name)
    {
        WaveDataGroup* grp = dynamic_cast<WaveDataGroup*>(wd);
        if (grp)
        {
            grp->rename(name);
            mWaveDataList->emitGroupUpdated(grp->id());
            return;
        }
        int iwave = mWaveDataList->waveIndexByNetId(wd->id());
        if (iwave >= 0)
        {
            mWaveDataList->updateWaveName(iwave, name);
        }
    }

    std::vector<const Net*> NetlistSimulatorController::getFilterNets(FilterInputFlag filter) const
    {
        if (!mSimulationInput->has_gates())
        {
            return std::vector<const Net*>();
        }
        switch (filter)
        {
            case GlobalInputs:
                return std::vector<const Net*>(get_input_nets().begin(), get_input_nets().end());
            case PartialNetlist:
                return get_partial_netlist_nets();
            case CompleteNetlist: {
                std::vector<Net*> tmp = (*mSimulationInput->get_gates().begin())->get_netlist()->get_nets();
                return std::vector<const Net*>(tmp.begin(), tmp.end());
            }
            case NoFilter:
                break;
        }
        return std::vector<const Net*>();
    }

    bool NetlistSimulatorController::can_import_data() const
    {
        // TODO : check for ongoing import ?
        if (mState == ParameterReady || mState == ParameterSetup || mState == ShowResults)
        {
            return true;
        }
        return false;
    }

    void NetlistSimulatorController::emitLoadProgress(int percent)
    {
        UNUSED(percent);
    }

    bool NetlistSimulatorController::import_vcd(const std::string& filename, FilterInputFlag filter)
    {
        VcdSerializer reader(mWorkDir, false, this);

        std::vector<const Net*> inputNets;
        if (filter != NoFilter)
        {
            for (const Net* n : getFilterNets(filter))
            {
                inputNets.push_back(n);
            }
        }

        if (reader.importVcd(filename, mWorkDir, inputNets))
        {
            mWaveDataList->updateFromSaleae();
        }
        else
        {
            return false;
        }

        checkReadyState();
        return true;
    }

    void NetlistSimulatorController::import_csv(const std::string& filename, FilterInputFlag filter, u64 timescale)
    {
        VcdSerializer reader(mWorkDir, false, this);

        std::vector<const Net*> inputNets;
        if (filter != NoFilter)
        {
            for (const Net* n : getFilterNets(filter))
            {
                inputNets.push_back(n);
            }
        }

        if (reader.importCsv(filename, mWorkDir, inputNets, timescale))
        {
            mWaveDataList->updateFromSaleae();
        }
        checkReadyState();
    }

    void NetlistSimulatorController::import_saleae(const std::string& dirname, std::unordered_map<Net*, int> lookupTable, u64 timescale)
    {
        VcdSerializer reader(mWorkDir, false, this);
        if (reader.importSaleae(dirname, lookupTable, mWorkDir, timescale))
        {
            mWaveDataList->updateFromSaleae();
        }
        checkReadyState();
    }

    void NetlistSimulatorController::import_simulation(const std::string& dirname, FilterInputFlag filter, u64 timescale)
    {
        hal::error_code ec;
        std::filesystem::path sourceDir(dirname);
        std::filesystem::path sourceSaleaeLookup = sourceDir / "saleae.json";
        SaleaeDirectory sd(sourceSaleaeLookup.string());
        {
            std::ifstream test(sourceSaleaeLookup.string(), std::ios::binary);
            if (!test.good())
            {
                log_warning(get_name(), "cannot import SALEAE data from '{}', cannot read lookup table.", dirname);
                return;
            }
        }
        if (filter == NoFilter)
        {
            std::filesystem::path targetDir(mWaveDataList->saleaeDirectory().get_directory());
            std::filesystem::path targetSaleaeLookup = targetDir / "saleae.json";
            std::filesystem::remove(targetSaleaeLookup, ec);
            std::filesystem::copy_file(sourceSaleaeLookup, targetSaleaeLookup, std::filesystem::copy_options::overwrite_existing, ec);
            for (const std::filesystem::directory_entry& sourceFileInfo : std::filesystem::directory_iterator(sourceDir, ec))
            {
                std::string fname = sourceFileInfo.path().filename().string();
                if (!simutil::starts_with(fname, "digital_") || !simutil::ends_with(fname, ".bin"))
                {
                    continue;
                }
                std::filesystem::path targetFile = targetDir / fname;
                std::filesystem::remove(targetFile, ec);
                std::filesystem::copy_file(sourceFileInfo.path(), targetFile, std::filesystem::copy_options::overwrite_existing, ec);
            }
            mWaveDataList->updateFromSaleae();
        }
        else
        {
            std::unordered_map<Net*, int> lookupTable;
            for (const Net* n : getFilterNets(filter))
            {
                int inx = sd.get_datafile_index(n->get_name(), n->get_id());
                if (inx < 0)
                {
                    continue;
                }
                lookupTable.insert(std::make_pair((Net*)n, inx));
            }
            VcdSerializer reader(mWorkDir, false, this);
            if (reader.importSaleae(dirname, lookupTable, mWorkDir, timescale))
            {
                mWaveDataList->updateFromSaleae();
            }
        }
        checkReadyState();
        restoreComposed(sd);
    }

    void NetlistSimulatorController::restoreComposed(const SaleaeDirectory& sd)
    {
        for (const SaleaeDirectoryComposedEntry& sdce : sd.get_composed_list())
        {
//            sdce.dump();
            std::vector<WaveData*> wds;
            for (int childKey : sdce.get_children())
            {
                WaveData* wd = nullptr;
                int mType    = childKey / SaleaeDirectoryNetEntry::sComposedBaseKey;
                int index    = childKey % SaleaeDirectoryNetEntry::sComposedBaseKey;
                switch (mType)
                {
                    case SaleaeDirectoryNetEntry::Group:
                        wd = simutil::map_value(mWaveDataList->mDataGroups, (u32)index, (WaveDataGroup*)nullptr);
                        break;
                    case SaleaeDirectoryNetEntry::Boolean:
                        wd = simutil::map_value(mWaveDataList->mDataBooleans, (u32)index, (WaveDataBoolean*)nullptr);
                        break;
                    case SaleaeDirectoryNetEntry::Trigger:
                        wd = simutil::map_value(mWaveDataList->mDataTrigger, (u32)index, (WaveDataTrigger*)nullptr);
                        break;
                    default:
                        int iwave = mWaveDataList->waveIndexByNetId(childKey);
                        if (iwave >= 0)
                        {
                            wd = mWaveDataList->at(iwave);
                        }
                }
                if (wd)
                {
                    wds.push_back(wd);
                }
            }
            std::vector<int> data;
            for (int dat : sdce.get_data())
            {
                data.push_back(dat);
            }
            if (!wds.empty() && wds.size() == sdce.get_children().size())
            {
                switch (sdce.type())
                {
                    case SaleaeDirectoryNetEntry::Group: {
                        WaveDataGroup* wdGrp = new WaveDataGroup(mWaveDataList, sdce.name());
                        mWaveDataList->addWavesToGroup(wdGrp->id(), wds);
                        break;
                    }
                    case SaleaeDirectoryNetEntry::Boolean: {
                        new WaveDataBoolean(mWaveDataList, wds, data);
                        break;
                    }
                    case SaleaeDirectoryNetEntry::Trigger: {
                        WaveDataTrigger* wdTrig = new WaveDataTrigger(mWaveDataList, wds, data);
                        if (sdce.get_filter_entry())
                        {
                            int filterKey = sdce.get_filter_entry();
                            if (filterKey > 0)
                            {
                                WaveData* wd = nullptr;
                                int mType    = filterKey / SaleaeDirectoryNetEntry::sComposedBaseKey;
                                int index    = filterKey % SaleaeDirectoryNetEntry::sComposedBaseKey;
                                switch (mType)
                                {
                                    case SaleaeDirectoryNetEntry::Group:
                                        wd = simutil::map_value(mWaveDataList->mDataGroups, (u32)index, (WaveDataGroup*)nullptr);
                                        break;
                                    case SaleaeDirectoryNetEntry::Boolean:
                                        wd = simutil::map_value(mWaveDataList->mDataBooleans, (u32)index, (WaveDataBoolean*)nullptr);
                                        break;
                                    case SaleaeDirectoryNetEntry::Trigger:
                                        wd = simutil::map_value(mWaveDataList->mDataTrigger, (u32)index, (WaveDataTrigger*)nullptr);
                                        break;
                                    default:
                                        int iwave = mWaveDataList->waveIndexByNetId(filterKey);
                                        if (iwave >= 0)
                                        {
                                            wd = mWaveDataList->at(iwave);
                                        }
                                }
                                if (wd)
                                {
                                    wdTrig->set_filter_wave(wd);
                                }
                            }
                        }
                        break;
                    }
                    default:
                        break;
                }
            }
        }
    }

    void NetlistSimulatorController::set_saleae_timescale(u64 timescale)
    {
        SaleaeParser::sTimeScaleFactor = timescale;
    }

    void NetlistSimulatorController::handleRunFinished(bool success)
    {
        if (!success)
        {
            log_warning(get_name(), "simulation engine error during run.");
            setState(EngineFailed);
        }

        /*
        for (Net* n : gNetlist->get_nets())
        {
            WaveData* wd = WaveData::simulationResultFactory(n, mSimulator.get());
            if (wd) mResultMap.insert(wd->id(),wd);
        }

        mSimulator->generate_vcd("result.vcd",0,t);
        */
    }

    bool NetlistSimulatorController::get_results()
    {
        bool success = getResultsInternal();
        setState(success ? ShowResults : EngineFailed);
        return success;
    }

    bool NetlistSimulatorController::getResultsInternal()
    {
        SimulationEngineEventDriven* sevd = static_cast<SimulationEngineEventDriven*>(mSimulationEngine);
        // mWaveDataList->dump();
        if (mSimulationEngine->can_share_memory())
        {
            for (const Net* n : get_partial_netlist_nets())
            {
                WaveData* wd = new WaveData(n);
                for (WaveEvent evt : sevd->get_simulation_events(n->get_id()))
                {
                    wd->insertBooleanValueWithoutSync(evt.time, evt.new_value);
                }
                mWaveDataList->addOrReplace(wd);
            }
        }
        else
        {
            std::filesystem::path resultFile = mSimulationEngine->get_result_filename();
            if (resultFile.is_relative())
            {
                resultFile = get_working_directory() / resultFile;
            }
            VcdSerializer reader(mWorkDir, false, this);
            {
                std::ifstream test(resultFile.string(), std::ios::binary);
                if (!test.good())
                {
                    return false;
                }
            }

            std::vector<const Net*> partialNets;
            for (const Net* n : get_partial_netlist_nets())
            {
                if (!mSimulateOnlyProbes.empty() && mSimulateOnlyProbes.find(n->get_id()) == mSimulateOnlyProbes.end())
                {
                    continue;
                }
                partialNets.push_back(n);
            }

            if (reader.importVcd(resultFile.string(), mWorkDir, partialNets))
            {
                mWaveDataList->updateFromSaleae();
            }
            else
            {
                return false;
            }

            if (!mSimulateOnlyProbes.empty())
            {
                hal::error_code ec;
                std::filesystem::remove(resultFile, ec);
            }
        }
        return true;
    }

    void NetlistSimulatorController::add_clock_frequency(const Net* clock_net, u64 frequency, bool start_at_zero)
    {
        u64 period = 1'000'000'000'000ul / frequency;
        add_clock_period(clock_net, period, start_at_zero);
    }

    void NetlistSimulatorController::checkReadyState()
    {
        if (mState >= ParameterReady)
        {
            return;    // nothing to do
        }

        if (mSimulationInput->is_ready() && mSimulationEngine && mWaveDataList->timeFrame().simulateMaxTime() > 0)
        {
            setState(ParameterReady);
        }
        persist();
    }

    void NetlistSimulatorController::add_clock_period(const Net* clock_net, u64 period, bool start_at_zero, u64 duration)
    {
        if (!clock_net)
        {
            log_warning(get_name(), "Generating clock failed, clock net is a nullptr!");
            return;
        }

        if (period < 2)
        {
            log_warning(get_name(), "Generating clock failed, period must be at least 2 ps so that the half period is not zero, but is {}.", period);
            return;
        }

        SimulationInput::Clock clk;
        clk.clock_net     = clock_net;
        clk.switch_time   = period / 2;
        clk.start_at_zero = start_at_zero;
        mSimulationInput->add_clock(clk);

        // A duration of zero means "for the whole simulation": how long that is only becomes known once
        // the caller is done issuing simulate() calls, so the waveform generated here is extended in
        // run_simulation(). It used to fall back to 2000 ps, which is not a default so much as a silent
        // truncation -- the clock simply stopped toggling and every later sample repeated the last value.
        mClockDurations[clock_net->get_id()] = duration;

        WaveData* wd = new WaveDataClock(clock_net, clk, duration);
        mWaveDataList->addOrReplace(wd);
        checkReadyState();
    }

    void NetlistSimulatorController::compute_waveform_groups()
    {
        mSimulationInput->compute_net_groups();
    }

    void NetlistSimulatorController::load_waveform_groups(bool inputs)
    {
        for (const SimulationInput::NetGroup& ng : mSimulationInput->get_net_groups())
        {
            if (inputs)
            {
                if (!ng.is_input())
                {
                    continue;
                }
            }
            else
            {
                if (ng.is_input())
                {
                    continue;
                }
            }
            if (ng.gate)
            {
                add_waveform_group(ng.gate, ng.gate_pin_group);
            }
            else
            {
                add_waveform_group(ng.module_pin_group);
            }
        }
    }

    void NetlistSimulatorController::add_gates(const std::vector<Gate*>& gates)
    {
        if (mState != NoGatesSelected)
        {
            log_warning(get_name(), "Command failed, gates for simulation already selected in this controller.");
            return;
        }
        mSimulationInput->add_gates(gates);

        std::set<u32> previousInputSet = mWaveDataList->toSet();
        std::set<u32> currentInputSet;
        for (const Net* n : mSimulationInput->get_input_nets())
        {
            u32 nid = n->get_id();
            if (previousInputSet.find(nid) == previousInputSet.end())
            {
                WaveData* wd = new WaveData(n, WaveData::InputNet);
                mWaveDataList->addOrReplace(wd);
            }
            currentInputSet.insert(nid);
        }
        for (u32 id : currentInputSet)
        {
            previousInputSet.erase(id);
        }
        for (u32 id : previousInputSet)
        {
            mWaveDataList->remove(id);
        }
        if (mState == NoGatesSelected && mSimulationInput->has_gates())
        {
            setState(ParameterSetup);
        }
        checkReadyState();
    }

    const std::unordered_set<const Gate*>& NetlistSimulatorController::get_gates() const
    {
        return mSimulationInput->get_gates();
    }

    const std::unordered_set<const Net*>& NetlistSimulatorController::get_input_nets() const
    {
        return mSimulationInput->get_input_nets();
    }

    std::vector<NetlistSimulatorController::InputColumnHeader> NetlistSimulatorController::get_input_column_headers() const
    {
        std::vector<InputColumnHeader> retval;
        std::unordered_set<const Net*> clkNets;
        for (const SimulationInput::Clock& clk : mSimulationInput->get_clocks())
        {
            clkNets.insert(clk.clock_net);
        }

        for (const Net* n : mSimulationInput->get_input_nets())
        {
            InputColumnHeader ipc;
            ipc.nets.push_back(n);
            ipc.name     = n->get_name();
            ipc.is_clock = (clkNets.find(n) != clkNets.end());
            retval.push_back(ipc);
        }

        for (SimulationInput::NetGroup ng : mSimulationInput->get_net_groups())
        {
            if (!ng.is_input())
            {
                continue;
            }
            InputColumnHeader ipc;
            ipc.name     = ng.get_name();
            ipc.is_clock = false;

            std::vector<const Net*> temp_nets = ng.get_nets();
            ipc.nets = ng.ascending ? temp_nets : std::vector<const Net*>(temp_nets.rbegin(),temp_nets.rend());
            for (const Net* n : temp_nets)
            {
                if (clkNets.find(n) != clkNets.end())
                {
                    ipc.is_clock = true;
                }
                retval.erase(std::remove_if(retval.begin(), retval.end(), [n](const auto& s) { return s.nets.size() == 1 && s.nets.at(0) == n; }), retval.end());
            }
            retval.push_back(ipc);
        }
        return retval;
    }

    const std::vector<const Net*>& NetlistSimulatorController::get_output_nets() const
    {
        return mSimulationInput->get_output_nets();
    }

    const std::vector<const Net*>& NetlistSimulatorController::get_partial_netlist_nets() const
    {
        return mSimulationInput->get_partial_netlist_nets();
    }

    void NetlistSimulatorController::set_input(const Net* net, BooleanFunction::Value value)
    {
        assert(net);
        if (!mSimulationInput->is_input_net(net))
        {
            if (mBadAssignInputWarnings[net->get_id()]++ < 3)
            {
                log_warning(get_name(), "net[{}] '{}' is not an input net, value not assigned.", net->get_id(), net->get_name());
            }
            return;
        }
        u64 t        = mWaveDataList->timeFrame().simulateMaxTime();
        WaveData* wd = mWaveDataList->waveDataByNet(net);
        if (!wd)
        {
            wd = new WaveData(net);
            wd->insertBooleanValueWithoutSync(t, value);
            mWaveDataList->addOrReplace(wd);
        }
        else
        {
            mWaveDataList->insertBooleanValue(wd, t, value);
        }
    }

    void NetlistSimulatorController::set_input(WaveData* wd, BooleanFunction::Value value)
    {
        u64 t = mWaveDataList->timeFrame().simulateMaxTime();
        mWaveDataList->insertBooleanValue(wd, t, value);
    }

    void NetlistSimulatorController::set_input(const std::vector<Net*>& nets, const std::vector<BooleanFunction::Value>& values)
    {
        if (nets.size() != values.size())
        {
            log_error(get_name(), "Cannot set vector of nets to vector of values, because vectors are not of equal size! {} vs. {}", nets.size(), values.size());
            return;
        }

        for (u32 idx = 0; idx < nets.size(); idx++)
        {
            set_input(nets.at(idx), values.at(idx));
        }
    }

    void NetlistSimulatorController::set_input(const WaveDataGroup* wdg, const std::vector<BooleanFunction::Value>& values)
    {
        const auto wave_forms = wdg->get_waveforms();
        if (wave_forms.size() != values.size())
        {
            log_error(
                get_name(), "Cannot set WaveDataGroup to vector of values, because the amount of grouped wave forms is not equal to the vector size! {} vs. {}", wave_forms.size(), values.size());
            return;
        }

        for (u32 idx = 0; idx < wave_forms.size(); idx++)
        {
            set_input(wave_forms.at(idx), values.at(idx));
        }
    }

    void NetlistSimulatorController::set_input(const u32 id, const std::vector<BooleanFunction::Value>& values)
    {
        const auto wave_data_group = get_waveform_group_by_id(id);
        set_input(wave_data_group, values);
    }

    void NetlistSimulatorController::set_input(const PinGroup<ModulePin>* pin_group, const std::vector<BooleanFunction::Value>& values)
    {
        std::vector<Net*> nets;
        for (const auto pin : pin_group->get_pins())
        {
            nets.push_back(pin->get_net());
        }

        set_input(nets, values);
    }

    void NetlistSimulatorController::set_timeframe(u64 tmin, u64 tmax)
    {
        mWaveDataList->setUserTimeframe(tmin, tmax);
    }

    void NetlistSimulatorController::initialize()
    {
    }

    void NetlistSimulatorController::reset()
    {
        mSimulationInput->clear();
        mWaveDataList->clearAll();
        mClockDurations.clear();
        mState = NoGatesSelected;
    }

    void NetlistSimulatorController::simulate(u64 picoseconds)
    {
        mWaveDataList->incrementSimulTime(picoseconds);
        checkReadyState();
    }

    void NetlistSimulatorController::handleSelectGates()
    {
        /*
        mSimulateGates = gsd.selectedGates();
        initSimulator();
        */
    }

    bool NetlistSimulatorController::generate_vcd(const std::filesystem::path& path, u32 start_time, u32 end_time, std::set<const Net*> nets) const
    {
        VcdSerializer writer(mWorkDir);
        std::vector<const WaveData*> partialList;
        if (nets.empty())
        {
            for (const WaveData* wd : *mWaveDataList)
            {
                partialList.push_back(wd);
            }
        }
        else
        {
            for (const Net* n : nets)
            {
                const WaveData* wd = mWaveDataList->waveDataByNet(n);
                if (wd)
                {
                    partialList.push_back(wd);
                }
            }
        }
        if (!start_time && !end_time)
        {
            start_time = mWaveDataList->timeFrame().sceneMinTime();
            end_time   = mWaveDataList->timeFrame().sceneMaxTime();
        }
        bool success = writer.exportVcd(path.string(), partialList, start_time, end_time);
        return success;
    }

    NetlistSimulatorControllerMap* NetlistSimulatorControllerMap::sInst = nullptr;

    NetlistSimulatorControllerMap* NetlistSimulatorControllerMap::instance()
    {
        if (!sInst)
        {
            sInst = new NetlistSimulatorControllerMap;
        }
        return sInst;
    }

    void NetlistSimulatorControllerMap::addController(NetlistSimulatorController* ctrl)
    {
        u32 id    = ctrl->get_id();
        mMap[id]  = ctrl;
    }

    void NetlistSimulatorControllerMap::removeController(u32 id)
    {
        auto it = mMap.find(id);
        if (it == mMap.end())
        {
            return;
        }
        mMap.erase(it);
    }

    void NetlistSimulatorControllerMap::clearAll()
    {
        mMap.clear();
    }

    std::vector<NetlistSimulatorController*> NetlistSimulatorControllerMap::toList() const
    {
        std::vector<NetlistSimulatorController*> retval;
        for (auto it = mMap.begin(); it != mMap.end(); ++it)
        {
            retval.push_back(it->second);
        }
        return retval;
    }

    NetlistSimulatorController* NetlistSimulatorControllerMap::controller(u32 id) const
    {
        auto it = mMap.find(id);
        if (it == mMap.end())
        {
            return nullptr;
        }
        return it->second;
    }

}    // namespace hal
