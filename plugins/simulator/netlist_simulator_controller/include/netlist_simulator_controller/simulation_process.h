// MIT License
//
// Copyright (c) 2019 Ruhr University Bochum, Chair for Embedded Security. All Rights reserved.
// Copyright (c) 2019 Marc Fyrbiak, Sebastian Wallat, Max Hoffmann ("ORIGINAL AUTHORS"). All rights reserved.
// Copyright (c) 2021 Max Planck Institute for Security and Privacy. All Rights reserved.
// Copyright (c) 2021 Jörn Langheinrich, Julian Speith, Nils Albartus, René Walendy, Simon Klix ("ORIGINAL AUTHORS"). All Rights reserved.
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in all
// copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.

#pragma once

#include "hal_core/defines.h"
#include "netlist_simulator_controller/simulation_engine.h"
#include "netlist_simulator_controller/simulation_input.h"

#include <fstream>
#include <string>
#include <thread>
#include <vector>

namespace hal {

    class NetlistSimulatorController;

    /**
     * Receives the log output that an external simulation process produces.
     *
     * Derive from this class and hand an instance to NetlistSimulatorController::setLogReceiver
     * to get notified about every chunk of engine output.
     */
    class SimulationLogReceiver
    {
    public:
        SimulationLogReceiver() {;}
        virtual ~SimulationLogReceiver() {;}

        /**
         * Called whenever the simulation process produced output.
         * @param[in] txt The output (HTML formatted).
         */
        virtual void handleLog(const std::string& txt) { UNUSED(txt); }
    };


    /**
     * Writes the log output of an external simulation process to a file.
     */
    class SimulationProcessLog
    {
        std::ofstream mFile;
        SimulationLogReceiver* mLogReceiver;

    public:
        SimulationProcessLog(const std::string& workdir);
        virtual ~SimulationProcessLog();
        bool good() const { return mFile.is_open(); }
        void setLogReceiver(SimulationLogReceiver* logReceiver);
        void operator<< (const std::string& txt);
        void flush();
        static std::string sLogFilename;
    };

    /**
     * Runs the shell commands of a scripted simulation engine in a separate thread.
     */
    class SimulationProcess {

        NetlistSimulatorController* mController;
        SimulationEngineScripted* mEngine;

        int mLineIndex;
        int mNumberLines;
        std::string mSaleaeDirectoryFilename;
        SimulationProcessLog* mProcessLog;
        std::thread mThread;

        void abortOnError();
        bool runProcess(const std::string& prog, const std::vector<std::string>& args);

        /// Notify the controller that the simulation process terminated.
        void processFinished(bool success);

    public:
        SimulationProcess(NetlistSimulatorController* controller, SimulationEngineScripted* engine);
        virtual ~SimulationProcess();
        SimulationProcessLog* log() const { return mProcessLog; }

        /// Start the simulation process in a separate thread.
        void start();

        /// Execute all command lines of the engine, called by the thread.
        void run();
    private:
        std::string toHtml(const std::string& txt);
        void runLocal();

        // server execution
        void runRemote();

    };
}
