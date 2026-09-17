#include "netlist_simulator_controller/simulation_thread.h"
#include "netlist_simulator_controller/netlist_simulator_controller.h"
#include "netlist_simulator_controller/saleae_parser.h"
#include "hal_core/utilities/log.h"
#include <vector>

namespace hal {

    SimulationThread::SimulationThread(NetlistSimulatorController* controller, const SimulationInput* simInput, SimulationEngineEventDriven *engine)
        : mController(controller), mSimulationInput(simInput), mEngine(engine), mLogChannel(controller->get_name()), mSimulTime(0),
          mSaleaeDirectoryFilename(controller->get_saleae_directory_filename()), mEngineFailed(false)
    {;}

    SimulationThread::~SimulationThread()
    {
        if (mThread.joinable()) mThread.detach();
    }

    void SimulationThread::start()
    {
        mThread = std::thread([this]() { this->run(); });
    }

    bool SimulationThread::runFailed() const
    {
        return mEngineFailed || mEngine->state() == SimulationEngine::Failed;
    }

    void SimulationThread::terminateThread(bool success, const char* failedStep)
    {
        if (!success && failedStep)
        {
            log_warning(mLogChannel, "simulation engine error during {}.", failedStep);
        }

        // Report to the controller first and publish the engine's terminal state last: `state()` is what a
        // caller polls to learn that the run is over, so everything this thread still has to do has to be
        // done by the time it flips. The other way round the caller raced the hand-off -- it could read the
        // results from, or destroy, a controller this thread was about to call into, and it saw the state
        // the controller was about to be put in only if it waited long enough for no stated reason.
        if (mController) mController->handleRunFinished(success);

        // failed() is the engine's clean-up hook for an aborted run and publishes Failed itself
        if (success)
            mEngine->setRunTerminated(true);
        else
            mEngine->failed();
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
                        mEngineFailed = true;
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
            if (runFailed())
                return terminateThread(false, "run");
        }

        // The loop above only sees a failure that happened before the last event was parsed. Checking
        // once more here keeps finalize() from turning an engine that failed on the final event into a
        // successful run with an incomplete result.
        if (runFailed())
            return terminateThread(false, "run");

        terminateThread(mEngine->finalize(), "finalize");
    }
}
