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

#include "hal_core/netlist/net.h"
#include "hal_core/netlist/boolean_function.h"

namespace hal
{
    /**
     * A single simulation event, i.e., the value that a net assumes at a given point in time.
     */
    struct WaveEvent
    {
        /**
         * The net affected by the event.
         */
        const Net* affected_net = nullptr;

        /**
         * The new value caused by the event.
         */
        BooleanFunction::Value new_value = BooleanFunction::Value::X;

        /**
         * The time of the event.
         */
        u64 time = 0;

        /**
         * The unique ID of the event.
         *
         * Two events that affect the same net at the same time are ordered by this ID, so it has to
         * be set by whoever creates the event -- an event queue that sorts on an indeterminate value
         * has no defined order at all. The default is a backstop, not a valid ID: every producer is
         * expected to hand out IDs from a counter that only ever increases, so that the order of two
         * events of the same point in time is the order in which they were created.
         */
        u64 id = 0;

        /**
         * Tests whether two events are equal.
         *
         * @param[in] other - Event to compare to.
         * @returns `true` when both events are equal, `false` otherwise.
         */
        bool operator==(const WaveEvent& other) const
        {
            return affected_net == other.affected_net && new_value == other.new_value && time == other.time;
        }

        /**
         * Tests whether one event happened before the other.
         *
         * @param[in] other - Event to compare to.
         * @returns `true` when this event happened before the other, `false` otherwise.
         */
        bool operator<(const WaveEvent& other) const
        {
            if (time != other.time)
            {
                return time < other.time;
            }
            return id < other.id;
        }
    };
}    // namespace hal
