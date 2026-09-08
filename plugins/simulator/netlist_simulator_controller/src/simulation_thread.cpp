#include "netlist_simulator_controller/simulation_thread.h"
#include "netlist_simulator_controller/netlist_simulator_controller.h"
#include "netlist_simulator_controller/saleae_parser.h"
#include "hal_core/utilities/log.h"
#include <vector>

namespace hal {

    SimulationThread::SimulationThread(NetlistSimulatorController* controller, const SimulationInput* simInput, SimulationEngineEventDriven *engine)
        : mController(controller), mSimulationInput(simInput), mEngine(engine), mLogChannel(controller->get_name()), mSimulTime(0),
          mSaleaeDirectoryFilename(controller->get_saleae_directory_filename())
    {;}

    SimulationThread::~SimulationThread()
    {
        if (mThread.joinable()) mThread.detach();
    }

    void SimulationThread::start()
    {
        mThread = std::thread([this]() { this->run(); });
    }

    void SimulationThread::terminateThread(bool success, const char* failedStep)
    {
        if (!success)
        {
            mEngine->failed();
            if (failedStep)
               log_warning(mLogChannel, "simulation engine error during {}.", failedStep);
        }
        if (mController) mController->handleRunFinished(success);
    }

    void SimulationThread::run()
    {
        mSimulTime = 0;
        SaleaeParser sp(mSaleaeDirectoryFilename);

        for (const Net* net : mSimulationInput->get_input_nets())
        {
            void* registerObj = (void*) net;
            sp.register_callback(net,[this](const void* obj, uint64_t t, int val) {
                if (t != mSimulTime)
                {
                    mSimulationInputNetEvent.set_simulation_duration(t - mSimulTime);
                    if (!mEngine->inputEvent(mSimulationInputNetEvent))
                    {
                        mEngine->failed();
                        return;
                    }
                    mSimulTime = t;
                    mSimulationInputNetEvent.clear();
                }
                mSimulationInputNetEvent.insert(std::make_pair(static_cast<const Net*>(obj),static_cast<BooleanFunction::Value>(val)));
            }, registerObj);
        }

        while (sp.next_event())
        {
            if (mEngine->state()==SimulationEngine::Failed)
                return terminateThread(false, "run");
        }

        terminateThread(mEngine->finalize(), "finalize");
    }
}
