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

/**
 * @file netlist_factory.h
 * @brief This file contains various functions to create and load netlists.
 */

#pragma once

#include "hal_core/defines.h"
#include "hal_core/netlist/gate_library/gate_library.h"
#include "hal_core/utilities/program_options.h"

namespace hal
{
    /* forward declaration */
    class Netlist;
    class GateLibrary;
    class ProjectDirectory;

    /**
     * \namespace netlist_factory
     * Contains the functions that create a netlist, either empty or by parsing a netlist file.
     *
     * @ingroup netlist
     */
    namespace netlist_factory
    {
        /**
         * @brief Create a new empty netlist using the specified gate library.
         *
         * @param[in] gate_library - The gate library.
         * @returns The netlist on success, a `nullptr` otherwise.
         */
        NETLIST_API std::unique_ptr<Netlist> create_netlist(const GateLibrary* gate_library);

        /**
         * @brief Create a netlist from the given file. 
         * 
         * Will either deserialize `.hal` file or call parser plugin for other formats.
         * In the latter case the specified gate library file is mandatory.
         *
         * @param[in] netlist_file - Path to the netlist file.
         * @param[in] gate_library_file - Path to the gate library file. Optional argument for `.hal` file.
         * @returns The netlist on success, a `nullptr` otherwise.
         */
        NETLIST_API std::unique_ptr<Netlist> load_netlist(const std::filesystem::path& netlist_file, const std::filesystem::path& gate_library_file = std::filesystem::path());

        /**
         * @brief Create a netlist from the given file trying to parse it with the specified gate library.
         * 
         * Will either deserialize `.hal` file or call parser plugin for other formats.
         *
         * @param[in] netlist_file - Path to the netlist file.
         * @param[in] gate_library - The gate library.
         * @returns The netlist on success, a `nullptr` otherwise.
         */
        NETLIST_API std::unique_ptr<Netlist> load_netlist(const std::filesystem::path& netlist_file, GateLibrary* gate_library);

        /**
         * @brief Create a netlist from the given file using an ordered list of gate libraries.
         *
         * The entries of the search list may be gate library files, directories holding gate library files, or bare
         * file names that are looked up in the standard gate library directories. They are loaded into a single
         * composite gate library, in which the entry that comes first wins a gate type name collision
         * (see `gate_library_manager::load_multiple()`).
         *
         * With `black_box_fallback` set, cells that none of the gate libraries defines do not abort the import but
         * become black box gate types derived from the way they are instantiated. The resulting netlist then
         * contains gates whose function is unknown, so this is off by default.
         *
         * Note that a netlist loaded from multiple gate libraries or with black boxes cannot be written to and read
         * back from a `.hal` file yet, it has to be re-imported with the same search list and options.
         *
         * @param[in] netlist_file - Path to the netlist file.
         * @param[in] gate_library_search_list - The ordered gate library search list.
         * @param[in] black_box_fallback - Set `true` to turn cells that no gate library defines into black boxes. Defaults to `false`.
         * @returns The netlist on success, a `nullptr` otherwise.
         */
        NETLIST_API std::unique_ptr<Netlist>
            load_netlist(const std::filesystem::path& netlist_file, const std::vector<std::string>& gate_library_search_list, bool black_box_fallback = false);

        /**
         * @brief Create a netlist from the given string.
         * 
         * The string must contain a netlist in HAL-(JSON)-format.
         *
         * @param[in] netlist_string - The string containing the netlist.
         * @param[in] gate_library_file - Path to the gate library file.
         * @returns The netlist on success, a `nullptr` otherwise.
         */
        NETLIST_API std::unique_ptr<Netlist> load_netlist_from_string(const std::string& netlist_string, const std::filesystem::path& gate_library_file = std::filesystem::path());

        /**
         * @brief Create a netlist from the given hal project.
         *
         * @param[in] project_dir - Path to the hal project directory.
         * @returns The netlist on success, a `nullptr` otherwise.
         */
        NETLIST_API std::unique_ptr<Netlist> load_hal_project(const std::filesystem::path& project_dir);

        /**
         * @brief Create a netlist using information specified in command line arguments on startup.
         * 
         * Will either deserialize `.hal` file or call parser plugin for other formats.
         *
         * @param[in] pdir - The HAL project directory.
         * @param[in] args - Command line arguments.
         * @returns The netlist on success, a `nullptr` otherwise.
         */
        NETLIST_API std::unique_ptr<Netlist> load_netlist(const ProjectDirectory& pdir, const ProgramArguments& args);

        /**
         * @brief Create a netlist from a given file for each matching pre-loaded gate library.
         *
         * @param[in] netlist_file - Path to the netlist file.
         * @returns A vector of netlists, one for each suitable gate library.
         */
        NETLIST_API std::vector<std::unique_ptr<Netlist>> load_netlists(const std::filesystem::path& netlist_file);
    }    // namespace netlist_factory
}    // namespace hal
