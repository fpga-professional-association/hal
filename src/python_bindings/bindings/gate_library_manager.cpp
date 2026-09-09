#include "hal_core/python_bindings/python_bindings.h"

namespace hal
{
    void gate_library_manager_init(py::module& m)
    {
        auto py_gate_library_manager = m.def_submodule("GateLibraryManager", R"(
            The gate library manager keeps track of all gate libraries that are used within HAL. Further, it takes care of loading and saving gate libraries on demnand.
        )");

        py_gate_library_manager.def(
            "load",
            [](std::filesystem::path file_path, bool reload) { return gate_library_manager::get_owning(gate_library_manager::load(file_path, reload)); },
            py::arg("file_path"),
            py::arg("reload") = false,
            R"(
            Load a gate library from file.

            :param pathlib.Path file_path: The path to the gate library file.
            :param bool reload: If ``True``, reloads the library in case it is already loaded.
            :returns: The gate library on success, ``None`` otherwise.
            :rtype: hal_py.GateLibrary or None
        )");

        py_gate_library_manager.def(
            "load_multiple",
            [](const std::vector<std::filesystem::path>& file_paths, const std::string& name, bool reload) {
                return gate_library_manager::get_owning(gate_library_manager::load_multiple(file_paths, name, reload));
            },
            py::arg("file_paths"),
            py::arg("name")   = "",
            py::arg("reload") = false,
            R"(
            Load multiple gate libraries at once and combine them into a single composite gate library.

            A netlist in HAL is bound to exactly one gate library, so netlists that mix cells from several Liberty or HGL files
            (standard cells, RAM macros, I/O pads, ...) need the libraries merged before instantiation. The files are loaded in
            the given order, which gives the list search-path semantics: if two files define a gate type of the same name, the
            one from the file that comes first wins and the other is discarded with a warning. Gate type IDs are re-assigned by
            the composite library, so they do not match the IDs of the individual libraries.

            The composite library does not correspond to any file on disk, so netlists using it cannot be written to and read
            back from a ``.hal`` file yet.

            :param list[pathlib.Path] file_paths: The ordered paths to the gate library files.
            :param str name: The name of the composite gate library. Defaults to a name derived from the loaded libraries.
            :param bool reload: If ``True``, reloads the composite library in case it is already loaded.
            :returns: The composite gate library on success, ``None`` otherwise.
            :rtype: hal_py.GateLibrary or None
        )");

        py_gate_library_manager.def("resolve_search_list", &gate_library_manager::resolve_search_list, py::arg("entries"), R"(
            Resolve an ordered gate library search list into an ordered list of gate library files.

            Every entry may be a path to a gate library file, a path to a directory that is expanded into all gate library files
            below it (sorted by path), or a bare file name that is looked up in the standard gate library directories. The order
            of the entries is preserved and duplicates are dropped, keeping the first occurrence.

            :param list[str] entries: The ordered gate library search list.
            :returns: The ordered list of absolute paths to gate library files.
            :rtype: list[pathlib.Path]
        )");

        py_gate_library_manager.def("split_search_list", &gate_library_manager::split_search_list, py::arg("search_list"), R"(
            Split a gate library search list given as a single comma-separated string into its entries.

            :param str search_list: The gate library search list, e.g. ``"stdcells.lib, macros/ram.lib"``.
            :returns: The entries of the search list in the order in which they were given.
            :rtype: list[str]
        )");

        py_gate_library_manager.def("load_all", &gate_library_manager::load_all, py::arg("reload") = false, R"(
            Load all gate libraries available in standard gate library directories.

            :param bool reload: If ``True``, reloads all libraries that have already been loaded.
        )");

        py_gate_library_manager.def("save", &gate_library_manager::save, py::arg("file_path"), py::arg("gate_lib"), py::arg("overwrite") = false, R"(
            Save a gate library to file.

            :param pathlib.Path file_path: The output path. 
            :param hal_py.GateLibrary gate_lib: The gate library.
            :param bool overwrite: If ``True``, overwrites already existing files.
            :returns: ``True`` on success, ``False`` otherwise.
            :rtype: bool
        )");

        py_gate_library_manager.def(
            "get_gate_library", [](const std::string& file_name) { return gate_library_manager::get_owning(gate_library_manager::get_gate_library(file_name)); }, py::arg("file_path"), R"(
            Get a gate library by file path. If no library with the given name is loaded, loading the gate library from file will be attempted.

            :param str file_path: The input path.
            :returns: The gate library on success, ``None`` otherwise.
            :rtype: hal_py.GateLibrary or None
        )");

        py_gate_library_manager.def(
            "get_gate_library_by_name",
            [](const std::string& lib_name) { return gate_library_manager::get_owning(gate_library_manager::get_gate_library_by_name(lib_name)); },
            py::arg("lib_name"),
            R"(
            Get a gate library by name. If no library with the given name is loaded, ``None`` will be returned.

            :param str lib_name: The name of the gate library.
            :returns: The gate library on success, ``None`` otherwise.
            :rtype: hal_py.GateLibrary or None
        )");

        py_gate_library_manager.def(
            "get_gate_libraries",
            [] {
                // get_gate_libraries hands out borrowed pointers, so ask the manager for the owning
                // pointer of each. Constructing a shared_ptr from the borrowed one instead would
                // open a second, independent ownership group over a library the manager already
                // owns, and the library would be freed twice.
                std::vector<std::shared_ptr<GateLibrary>> result;
                for (const auto* lib : gate_library_manager::get_gate_libraries())
                {
                    result.emplace_back(gate_library_manager::get_owning(lib));
                }
                return result;
            },
            R"(
            Get all loaded gate libraries.

            :returns: A list of gate libraries.
            :rtype:  list[hal_py.GateLibrary]
        )");
    }
}    // namespace hal
