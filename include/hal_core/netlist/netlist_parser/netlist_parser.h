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
#include "hal_core/netlist/netlist.h"
#include "hal_core/utilities/result.h"

#include <filesystem>

namespace hal
{
    /* forward declaration*/
    class GateLibrary;

    /**
     * The base class for all netlist parsers.
     * A parser reads a netlist file into an internal intermediate representation and can then instantiate it for a given gate library.
     *
     * @ingroup netlist_parser
     */
    class NETLIST_API NetlistParser
    {
    public:
        NetlistParser()          = default;
        virtual ~NetlistParser() = default;

        /**
         * Parse a netlist into an internal intermediate format.
         *
         * @param[in] file_path - Path to the netlist file.
         * @returns `true` on success, `false` otherwise.
         */
        virtual Result<std::monostate> parse(const std::filesystem::path& file_path) = 0;

        /**
         * Instantiate the parsed netlist using the specified gate library.
         *
         * @param[in] gate_library - The gate library.
         * @returns A pointer to the resulting netlist.
         */
        virtual Result<std::unique_ptr<Netlist>> instantiate(const GateLibrary* gate_library) = 0;

        /**
         * Parse and instantiate a netlist using the specified gate library.
         *
         * @param[in] file_path - Path to the netlist file.
         * @param[in] gate_library - The gate library.
         * @returns A pointer to the resulting netlist.
         */
        Result<std::unique_ptr<Netlist>> parse_and_instantiate(const std::filesystem::path& file_path, const GateLibrary* gate_library)
        {
            if (auto res = parse(file_path); res.is_ok())
            {
                return instantiate(gate_library);
            }
            else
            {
                return ERR(res.get_error());
            }
        }

        /**
         * Enable or disable the black box fallback.
         *
         * With the fallback disabled (the default), instantiating a cell that the gate library does not define is an
         * error that aborts the import of the whole netlist. With it enabled, such a cell is turned into a black box
         * gate type derived from the way it is instantiated, and only a warning is logged. This trades strictness for
         * the ability to load chip-top netlists whose gate libraries are incomplete, so the resulting netlist contains
         * gates whose function is unknown. Use `GateLibrary::is_black_box_gate_type()` to identify them.
         *
         * The black boxes are added to the gate library the netlist is instantiated with, which is usually shared with
         * every other netlist loaded from the same gate library file. A parse with the fallback disabled therefore
         * ignores the black boxes it finds in that library and still fails on the cells they stand in for, so that
         * whether an import is strict depends on this setting alone and not on what was imported before it.
         *
         * Parsers that do not implement the fallback ignore this setting.
         *
         * @param[in] enable - Set `true` to enable the black box fallback, `false` to abort on unknown cells.
         */
        void enable_black_box_fallback(bool enable)
        {
            m_black_box_fallback = enable;
        }

        /**
         * Check whether the black box fallback is enabled.
         *
         * @returns `true` if the black box fallback is enabled, `false` otherwise.
         */
        bool is_black_box_fallback_enabled() const
        {
            return m_black_box_fallback;
        }

    protected:
        /**
         * Set to `true` if cells that are not defined by the gate library shall become black box gate types.
         */
        bool m_black_box_fallback = false;
    };
}    // namespace hal
