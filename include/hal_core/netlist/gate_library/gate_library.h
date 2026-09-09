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
#include "hal_core/netlist/gate_library/gate_type.h"
#include "hal_core/utilities/result.h"

#include <filesystem>
#include <map>
#include <set>
#include <string>
#include <vector>

namespace hal
{
    /**
     * A gate library is a collection of gate types including their pins and Boolean functions.
     *
     * ### Composite gate libraries
     *
     * A gate library may be assembled from more than one source file, which is what real chip-top netlists require:
     * standard cells, RAM macros, I/O pads, and analog models usually live in separate Liberty or HGL files.
     * Such a library is built by absorbing the gate types of freshly parsed libraries into a single one
     * (see `GateLibrary::absorb()` and `gate_library_manager::load_multiple()`), because a netlist in HAL is always
     * bound to exactly one gate library and `Netlist::create_gate()` rejects gate types that are not part of it.
     *
     * The following semantics apply:
     * - **Gate type ID collisions:** gate type IDs are only unique within a gate library. Absorbing a library
     *   therefore re-assigns fresh IDs to all gate types taken over, so IDs of the source libraries are not preserved.
     *   Refer to gate types by name, never by ID, across libraries.
     * - **Gate type name collisions:** names are the identity used by every netlist parser. The gate library that
     *   comes first in the ordered list wins, later definitions of the same name are discarded with a warning
     *   (search-path semantics), unless the caller explicitly asks for the opposite.
     * - **Provenance:** `get_source_paths()` returns the ordered list of files the library was assembled from.
     *   For a library parsed from a single file, this is just its own path.
     *
     * ### Black box gate types
     *
     * Gate types created via `create_black_box_gate_type()` are synthesized by HAL, not read from any gate library
     * file. They stand in for cells that a netlist instantiates but that no gate library defines. Their pins are
     * derived from the netlist instantiation, so their direction is unknown and all of them are `PinDirection::inout`;
     * they carry no properties and no Boolean functions. Use `is_black_box_gate_type()` to tell them apart from
     * gate types that are backed by a gate library file, for instance before drawing conclusions from a
     * gate type's (empty) properties.
     *
     * A netlist parser adds them to the very gate library the netlist is being instantiated with, as a netlist only
     * accepts gate types of its own library. That library is usually owned by the gate library manager and shared
     * with every other netlist loaded from the same file, so the black boxes of one import stay visible to the next
     * one: importing the same netlist again reuses them instead of synthesizing them a second time, while an import
     * that did not ask for the fallback ignores them and still fails on the cell they stand in for.
     *
     * Neither composite libraries nor black box gate types survive a `.hal` serialization round-trip yet: the
     * serializer records a single gate library path and black box types exist in no file at all. Re-import the
     * netlist with the same set of gate libraries and the same parser options instead.
     *
     * @ingroup gate_lib
     */
    class NETLIST_API GateLibrary
    {
    public:
        /**
         * Construct a gate library by specifying its name and the path to the file that describes the library.
         *
         * @param[in] path - The path to the gate library file.
         * @param[in] name - The name of the gate library.
         */
        GateLibrary(const std::filesystem::path& path, const std::string& name);
        ~GateLibrary() = default;

        /**
         * Get the name of the gate library.
         *
         * @returns The name of the gate library.
         */
        std::string get_name() const;

        /**
         * Get the path to the file describing the gate library.
         *
         * @returns The path to the gate library file.
         */
        std::filesystem::path get_path() const;

        /**
         * Get the ordered list of files the gate library has been assembled from.
         *
         * A library parsed from a single file reports exactly that file. A composite library reports the source
         * files of all libraries it absorbed, in the order in which they were absorbed, which is the order in
         * which gate type name collisions were resolved.
         *
         * @returns The ordered list of source paths.
         */
        const std::vector<std::filesystem::path>& get_source_paths() const;

        /**
         * Check whether the gate library has been assembled from more than one gate library file.
         *
         * @returns `true` if the gate library is a composite of multiple gate library files, `false` otherwise.
         */
        bool is_composite() const;

        /**
         * Take over all gate types of another gate library, consuming them.
         *
         * The gate types are moved, not copied, so `other` is left without any gate types and must not be used
         * afterwards. Pass a freshly parsed library that nothing else refers to; absorbing a library that is
         * registered with the gate library manager would pull the ground out from under every netlist using it.
         *
         * Gate types keep their name but are assigned a fresh ID within this library, VCC and GND markings as well
         * as black box markings are carried over, and the source paths of `other` are appended to the ones of this
         * library. On a gate type name collision the type already present in this library is kept unless
         * `overwrite_existing` is set, in either case a warning is logged. As a library holds only one gate
         * location data category, the first library absorbed into an empty one sets it.
         *
         * @param[in] other - The gate library to absorb.
         * @param[in] overwrite_existing - Set `true` to let the gate types of `other` replace equally named gate types of this library, `false` to keep the existing ones. Defaults to `false`.
         * @returns Ok on success, an error otherwise.
         */
        Result<std::monostate> absorb(GateLibrary* other, bool overwrite_existing = false);

        /**
         * Create a black box gate type standing in for a cell that no gate library defines.
         *
         * The ports are turned into pins in the given order: a port of width 1 becomes a single pin carrying the
         * port name, a port of width n > 1 becomes a pin group of that name holding the pins `<name>(0)` to
         * `<name>(n-1)`, ordered from the most significant bit down and indexed from 0, which is what a bus of a
         * gate library read from a file looks like and what the netlist parsers expect when they connect one. As
         * the direction of a port cannot be recovered from a netlist instantiation, every pin is created as
         * `PinDirection::inout`. The gate type carries no properties, so that it is not mistaken for combinational
         * or sequential logic, and no Boolean functions.
         *
         * @param[in] name - The name of the gate type.
         * @param[in] ports - The ports as pairs of port name and port width, in the order in which the pins shall be created.
         * @returns The new gate type on success, an error otherwise.
         */
        Result<GateType*> create_black_box_gate_type(const std::string& name, const std::vector<std::pair<std::string, u32>>& ports);

        /**
         * Check whether the given gate type is a black box gate type synthesized by HAL.
         *
         * @param[in] gate_type - The gate type.
         * @returns `true` if the gate type is a black box gate type of this library, `false` otherwise.
         */
        bool is_black_box_gate_type(const GateType* gate_type) const;

        /**
         * Get all black box gate types of the library.
         *
         * @returns A map from black box gate type names to gate types.
         */
        std::unordered_map<std::string, GateType*> get_black_box_gate_types() const;

        /**
         * Hack to alter the path if gate library gets modified and written to a new location.
         * Use this function only if you know exactly what you are doing
         *
         * @param[in] modified_path - The path to the new location.
         */
        void set_path(const std::filesystem::path& modified_path);

        /**
         * Hack to alter the name if gate library gets modified and written to a new location.
         * Use this function only if you know exactly what you are doing
         *
         * @param[in] modified_name - The new name.
         */
        void set_name(const std::string& modified_name);

        /**
         * Set the data category of the gate location information.
         * 
         * @param[in] category - The data category.
         */
        void set_gate_location_data_category(const std::string& category);

        /**
         * Get the data category of the gate location information.
         * 
         * @returns The data category.
         */
        const std::string& get_gate_location_data_category() const;
        /**
         * Set the data identifiers of the gate location information for both the x- and y-coordinates.
         * 
         * @param[in] x_coordinate - The data identifier for the x-coordinate.
         * @param[in] y_coordinate - The data identifier for the y-coordinate.
         */
        void set_gate_location_data_identifiers(const std::string& x_coordinate, const std::string& y_coordinate);

        /**
         * Get the data identifiers of the gate location information for both the x- and y-coordinates.
         * 
         * @returns A pair of data identifiers.
         */
        const std::pair<std::string, std::string>& get_gate_location_data_identifiers() const;

        /**
         * Create a new gate type, add it to the gate library, and return it.
         * 
         * @param[in] name - The name of the gate type.
         * @param[in] properties - The properties of the gate type.
         * @param[in] component - A component adding additional functionality to the gate type.
         * @returns The new gate type instance on success, a `nullptr` otherwise.
         */
        GateType* create_gate_type(const std::string& name, std::set<GateTypeProperty> properties = {GateTypeProperty::combinational}, std::unique_ptr<GateTypeComponent> component = nullptr);

        // TODO pybind
        /**
         * Replace gate type with given ID, might change name, properties, component, and return it.
         *
         * @param[in] id - The ID of gate type
         * @param[in] name - The name of the gate type.
         * @param[in] properties - The properties of the gate type.
         * @param[in] component - A component adding additional functionality to the gate type.
         * @returns The new gate type instance on success, a `nullptr` otherwise.
         */
        GateType* replace_gate_type(u32 id, const std::string& name, std::set<GateTypeProperty> properties = {GateTypeProperty::combinational}, std::unique_ptr<GateTypeComponent> component = nullptr);

        /**
         * Check whether the given gate type is contained in this library.
         *
         * @param[in] gate_type - The gate type.
         * @returns `true` if the gate type is part of this library, `false` otherwise.
         */
        bool contains_gate_type(GateType* gate_type) const;

        /**
         * Check by name whether the given gate type is contained in this library.
         *
         * @param[in] name - The name of the gate type.
         * @returns `true` if the gate type is part of this library, `false` otherwise.
         */
        bool contains_gate_type_by_name(const std::string& name) const;

        /**
         * Get the gate type corresponding to the given name if contained within the library. In case there is no gate type with that name, a `nullptr` is returned.
         *
         * @param[in] name - The name of the gate type.
         * @returns The gate type on success, a `nullptr` otherwise.
         */
        GateType* get_gate_type_by_name(const std::string& name) const;

        /**
         * Get all gate types of the library.
         * In case a filter is applied, only the gate types matching the filter condition are returned.
         *
         * @param[in] filter - The user-defined filter function.
         * @returns A map from gate type names to gate types.
         */
        std::unordered_map<std::string, GateType*> get_gate_types(const std::function<bool(const GateType*)>& filter = nullptr) const;

        /**
         * Mark a gate type as a VCC gate type.
         *
         * @param[in] gate_type - The gate type.
         * @returns `true` on success, `false` otherwise.
         */
        bool mark_vcc_gate_type(GateType* gate_type);

        /**
         * Get all VCC gate types of the library.
         *
         * @returns A map from VCC gate type names to gate type objects.
         */
        std::unordered_map<std::string, GateType*> get_vcc_gate_types() const;

        /**
         * Mark a gate type as a GND gate type.
         *
         * @param[in] gate_type - The gate type.
         * @returns `true` on success, `false` otherwise.
         */
        bool mark_gnd_gate_type(GateType* gate_type);

        /**
         * Get all GND gate types of the library.
         *
         * @returns A map from GND gate type names to gate type objects.
         */
        std::unordered_map<std::string, GateType*> get_gnd_gate_types() const;

        /**
         * Add an include required for parsing a corresponding netlist, e.g., VHDL libraries.
         *
         * @param[in] include - The include to add.
         */
        void add_include(const std::string& include);

        /**
         * Get a vector of includes required for parsing a corresponding netlist, e.g., VHDL libraries.
         *
         * @returns A vector of includes.
         */
        std::vector<std::string> get_includes() const;

        /**
         * Remove the gate type of the given name from the gate library, so that it can no longer be looked up by name
         * and no longer stands in the way of a new gate type of that name. The gate type object itself stays alive for
         * as long as the library does, so gates already created from it keep working.
         *
         * @param[in] name - The name of the gate type to remove.
         */
        void remove_gate_type(const std::string& name);

    private:
        std::string m_name;
        std::filesystem::path m_path;
        std::vector<std::filesystem::path> m_source_paths;

        std::string m_gate_location_data_category                            = "generic";
        std::pair<std::string, std::string> m_gate_location_data_identifiers = {"X_COORDINATE", "Y_COORDINATE"};

        u32 m_next_gate_type_id;

        std::vector<std::unique_ptr<GateType>> m_gate_types;
        std::unordered_map<std::string, GateType*> m_gate_type_map;
        std::unordered_map<std::string, GateType*> m_vcc_gate_types;
        std::unordered_map<std::string, GateType*> m_gnd_gate_types;
        std::unordered_map<std::string, GateType*> m_black_box_gate_types;

        std::vector<std::string> m_includes;

        GateLibrary(const GateLibrary&) = delete;
        GateLibrary& operator=(const GateLibrary&) = delete;

        u32 get_unique_gate_type_id();
    };
}    // namespace hal
