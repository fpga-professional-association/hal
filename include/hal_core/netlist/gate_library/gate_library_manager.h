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

#include <filesystem>
#include <string>
#include <vector>

namespace hal
{
    class GateLibrary;

    /**
     * The gate library manager keeps track of all gate libraries that are used within HAL. Further, it takes care of loading and saving gate libraries on demnand.
     * 
     * @ingroup gate_lib
     */
    namespace gate_library_manager
    {
        /**
         * Load a gate library from file.
         *
         * @param[in] file_path - The input path.
         * @param[in] reload - If `true`, reloads the library in case it is already loaded.
         * @returns The gate library on success, `nullptr` otherwise.
         */
        NETLIST_API GateLibrary* load(std::filesystem::path file_path, bool reload = false);

        /**
         * Resolve an ordered gate library search list into an ordered list of gate library files.
         *
         * Every entry may be
         * - a path to a gate library file, which is taken as is,
         * - a path to a directory, which is expanded into all gate library files below it, sorted by path so that
         *   the result does not depend on the order in which the file system happens to return directory entries, or
         * - a bare file name, which is looked up in the standard gate library directories.
         *
         * The order of the entries is preserved, as it decides which gate type wins a name collision when the
         * libraries are loaded together (see `load_multiple()`). Duplicates are dropped, keeping the first
         * occurrence. Entries that cannot be resolved are skipped with an error, so an empty result means that
         * nothing at all could be resolved.
         *
         * @param[in] entries - The ordered gate library search list.
         * @returns The ordered list of absolute paths to gate library files.
         */
        NETLIST_API std::vector<std::filesystem::path> resolve_search_list(const std::vector<std::string>& entries);

        /**
         * Split a gate library search list given as a single string into its entries.
         *
         * Entries are separated by `,` and surrounding whitespace is stripped, which keeps the syntax usable for
         * both the command line and the Python bindings without clashing with drive letters or absolute paths.
         *
         * @param[in] search_list - The gate library search list, e.g. `"stdcells.lib, macros/ram.lib"`.
         * @returns The entries of the search list in the order in which they were given.
         */
        NETLIST_API std::vector<std::string> split_search_list(const std::string& search_list);

        /**
         * Load multiple gate libraries at once and combine them into a single composite gate library.
         *
         * A netlist in HAL is bound to exactly one gate library, so netlists that mix cells from several Liberty or
         * HGL files (standard cells, RAM macros, I/O pads, ...) need the libraries merged before instantiation.
         * The files are parsed in the given order and their gate types are moved into one composite library, which
         * gives the list search-path semantics: if two files define a gate type of the same name, the one from the
         * file that comes first wins and the other is discarded with a warning. Gate type IDs are re-assigned by
         * the composite library, so they do not match the IDs of the individual libraries.
         *
         * The composite library is owned by the gate library manager just like a library loaded from a single file,
         * and it records the ordered source paths (see `GateLibrary::get_source_paths()`). Loading the same ordered
         * list again returns the library that is already loaded unless `reload` is set. Note that the composite
         * library does not correspond to any file on disk: netlists using it cannot be written to and read back
         * from a `.hal` file yet, they have to be re-imported with the same search list instead.
         *
         * @param[in] file_paths - The ordered paths to the gate library files.
         * @param[in] name - The name of the composite gate library. Defaults to an empty string, in which case the name is derived from the names of the loaded libraries.
         * @param[in] reload - If `true`, reloads the composite library in case it is already loaded.
         * @returns The composite gate library on success, `nullptr` otherwise.
         */
        NETLIST_API GateLibrary* load_multiple(const std::vector<std::filesystem::path>& file_paths, const std::string& name = "", bool reload = false);

        /**
         * Load all gate libraries available in standard gate library directories.
         *
         * @param[in] reload - If `true`, reloads all libraries that have already been loaded.
         */
        NETLIST_API void load_all(bool reload = false);

        /**
         * Lists all pathnames to gate libraries
         * @return Vector of path
         */
        NETLIST_API std::vector<std::filesystem::path> get_all_path();

        /**
         * Save a gate library to file.
         * 
         * @param[in] file_path - The output path. 
         * @param[in] gate_lib - The gate library.
         * @param[in] overwrite - If `true`, overwrites already existing files.
         * @returns `true` on success, `false` otherwise.
         */
        // TODO test
        NETLIST_API bool save(std::filesystem::path file_path, GateLibrary* gate_lib, bool overwrite = false);

        /**
         * Remove a gate library.
         *
         * @param[in] file_path - The input path.
         */
        // TODO test
        NETLIST_API void remove(std::filesystem::path file_path);

        /**
         * Get a gate library by file path. If no library with the given name is loaded, loading the gate library from file will be attempted.
         *
         * @param[in] file_path - The input path.
         * @returns The gate library on success, `nullptr` otherwise.
         */
        NETLIST_API GateLibrary* get_gate_library(const std::string& file_path);

        /**
         * Get a gate library by name. If no library with the given name is loaded, a `nullptr` will be returned.
         *
         * @param[in] lib_name - The name of the gate library.
         * @returns The gate library on success, `nullptr` otherwise.
         */
        NETLIST_API GateLibrary* get_gate_library_by_name(const std::string& lib_name);

        /**
         * Get all loaded gate libraries.
         *
         * @returns A vector of gate libraries.
         */
        NETLIST_API std::vector<GateLibrary*> get_gate_libraries();

        /**
         * Get the owning pointer to a gate library, so that it can be kept alive independently of the manager.
         *
         * Reloading a library replaces it in the manager and destroys the one loaded before, which would leave
         * every netlist built against it pointing into freed memory. Holding the owning pointer prevents that.
         *
         * @param[in] gate_lib - The gate library.
         * @returns The owning pointer, non-owning if the library is not managed here.
         */
        NETLIST_API std::shared_ptr<GateLibrary> get_owning(const GateLibrary* gate_lib);

    }    // namespace gate_library_manager
}    // namespace hal
