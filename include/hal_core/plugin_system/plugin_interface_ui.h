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
#include "hal_core/plugin_system/plugin_interface_base.h"
#include "hal_core/utilities/program_options.h"

namespace hal
{
    class Netlist;

    /**
     * generic plugin instance interface
     *
     * @ingroup plugins
     */
    class CORE_API UIPluginInterface : virtual public BasePluginInterface
    {
    public:
        UIPluginInterface()          = default;
        virtual ~UIPluginInterface() = default;

        /**
         * Hands over the netlist that HAL loaded from the command line.
         *
         * Called before exec() whenever `--project-dir`, `--import-netlist` or `--empty-project` was
         * given alongside the flag of this plugin; the argument is `nullptr` when no project was
         * requested. The netlist stays owned by HAL and outlives the exec() call, so implementations
         * may keep the pointer for the duration of that call but must not free it or hold it past the
         * end of exec().
         *
         * An implementation that has nothing to show a netlist in can ignore this; the default does.
         *
         * @param[in] netlist - The loaded netlist, or `nullptr` if HAL did not load one.
         */
        virtual void set_netlist(Netlist* netlist)
        {
            UNUSED(netlist);
        }

        /**
         * Generic call to run the interactive UI.
         *
         * The return value is the success flag of the whole HAL run: `hal` hands control to exactly one
         * UI plugin and turns the result of this call directly into its process exit code, so
         * `true` becomes an exit code of 0 and `false` becomes a nonzero exit code. Implementations must
         * therefore report every condition that makes the requested work incomplete -- a UI that failed to
         * start, a script that could not be located or read, and any error raised by the code it ran -- as
         * `false`, and reserve `true` for a run that did what the user asked. Diagnostics belong in the log
         * or on stderr; the flag alone is what the caller sees.
         *
         * A user closing an interactive session in the normal way is a success, not a failure.
         *
         * @param[in] args - Program options for HAL.
         * @returns `true` if the requested work completed, `false` on any error.
         */
        virtual bool exec(ProgramArguments& args) = 0;

        /**
         * Generic call to block layouter.
         *
         * Can be enabled multiple times, but each enable must be match by disable to remove the lock.
         * @param[in] enable Enable lock on `true`, disable on `false`
         */
        virtual void set_layout_locker(bool enable) = 0;

        /**
         * Generic call to report the progress of a long-running operation to the user.
         *
         * A percentage of 100 indicates that the operation is done and dismisses the progress display again, so it
         * must be reported exactly once per operation. Prefer `ProgressScope` over calling this directly, as it takes
         * care of that. The default implementation does nothing.
         *
         * @param[in] percent - The progress in percent, where 100 means done.
         * @param[in] message - The message to display alongside the progress.
         */
        virtual void set_progress(int percent, const std::string& message)
        {
            UNUSED(percent);
            UNUSED(message);
        }
    };
}    // namespace hal
