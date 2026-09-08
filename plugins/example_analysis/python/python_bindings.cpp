#include "hal_core/python_bindings/python_bindings.h"

#include "example_analysis/example_analysis.h"
#include "example_analysis/plugin_example_analysis.h"

#include "pybind11/pybind11.h"
#include "pybind11/stl.h"

#include <stdexcept>

namespace py = pybind11;

namespace hal
{
    // The name in PYBIND11_MODULE *MUST* match the file name of the output library (without
    // extension), otherwise importing it fails with "dynamic module does not define module export
    // function".

#ifdef PYBIND11_MODULE
    PYBIND11_MODULE(example_analysis, m)
    {
        m.doc() = "Group sequential gates by the net that drives their clock pin (reference output of tools/new_plugin.py).";
#else
    PYBIND11_PLUGIN(example_analysis)
    {
        py::module m("example_analysis", "Group sequential gates by the net that drives their clock pin (reference output of tools/new_plugin.py).");
#endif    // ifdef PYBIND11_MODULE

        py::class_<ExampleAnalysisPlugin, RawPtrWrapper<ExampleAnalysisPlugin>, BasePluginInterface> py_example_analysis_plugin(m, "ExampleAnalysisPlugin", R"(Group sequential gates by the net that drives their clock pin (reference output of tools/new_plugin.py).)");

        py_example_analysis_plugin.def_property_readonly("name", &ExampleAnalysisPlugin::get_name, R"(
            The name of the plugin.

            :type: str
        )");

        py_example_analysis_plugin.def("get_name", &ExampleAnalysisPlugin::get_name, R"(
            Get the name of the plugin.

            :returns: The name of the plugin.
            :rtype: str
        )");

        py_example_analysis_plugin.def_property_readonly("version", &ExampleAnalysisPlugin::get_version, R"(
            The version of the plugin.

            :type: str
        )");

        py_example_analysis_plugin.def("get_version", &ExampleAnalysisPlugin::get_version, R"(
            Get the version of the plugin.

            :returns: The version of the plugin.
            :rtype: str
        )");

        py_example_analysis_plugin.def("get_description", &ExampleAnalysisPlugin::get_description, R"(
            Get a short description of the plugin.

            :returns: The description of the plugin.
            :rtype: str
        )");

        py_example_analysis_plugin.def("get_dependencies", &ExampleAnalysisPlugin::get_dependencies, R"(
            Get the plugins this plugin depends on.

            :returns: The set of plugin names this plugin depends on.
            :rtype: set[str]
        )");

        py_example_analysis_plugin.def_static("get_capabilities", &ExampleAnalysisPlugin::get_capabilities, R"(
            Get this plugin's capability declaration (the contents of its ``capabilities.json``) as a JSON string.

            :returns: The capability declaration.
            :rtype: str
        )");

        py::class_<example_analysis::ClockDomain> py_clock_domain(m, "ClockDomain", R"(
            A set of sequential gates whose clock pins are driven by the same net.

            Structural, not verified: buffers and clock gating split one physical clock into several nets.
        )");

        py_clock_domain.def_property_readonly(
            "clock_net",
            py::cpp_function([](const example_analysis::ClockDomain& self) { return self.clock_net; }, py::is_method(py_clock_domain), borrowed()),
            R"(
            The net driving the clock pins of every gate in this domain.

            :type: hal_py.Net
        )");

        py_clock_domain.def_property_readonly(
            "gates",
            py::cpp_function([](const example_analysis::ClockDomain& self) { return self.gates; }, py::is_method(py_clock_domain), borrowed()),
            R"(
            The gates clocked by ``clock_net``, ordered by gate ID.

            :type: list[hal_py.Gate]
        )");

        py_clock_domain.def("__len__", [](const example_analysis::ClockDomain& self) { return self.gates.size(); });

        py::class_<example_analysis::UnsupportedGateType> py_unsupported(m, "UnsupportedGateType", R"(
            A gate type the analysis matched but could not evaluate, with the reason why.
        )");

        py_unsupported.def_property_readonly(
            "gate_type",
            py::cpp_function([](const example_analysis::UnsupportedGateType& self) { return self.gate_type; }, py::is_method(py_unsupported), borrowed()),
            R"(
            The gate type that could not be evaluated.

            :type: hal_py.GateType
        )");

        py_unsupported.def_property_readonly(
            "count", py::cpp_function([](const example_analysis::UnsupportedGateType& self) { return self.count; }, py::is_method(py_unsupported)), R"(
            How many gates of that type the netlist contains.

            :type: int
        )");

        py_unsupported.def_property_readonly(
            "reason", py::cpp_function([](const example_analysis::UnsupportedGateType& self) { return self.reason; }, py::is_method(py_unsupported)), R"(
            Why the analysis could not evaluate this gate type.

            :type: str
        )");

        py::class_<example_analysis::Report> py_report(m, "Report", R"(
            The result of one ``analyze()`` run.
        )");

        py_report.def_property_readonly(
            "domains", py::cpp_function([](const example_analysis::Report& self) { return self.domains; }, py::is_method(py_report)), R"(
            The structural clock domains, largest first.

            :type: list[example_analysis.ClockDomain]
        )");

        py_report.def_property_readonly(
            "unresolved_gates",
            py::cpp_function([](const example_analysis::Report& self) { return self.unresolved_gates; }, py::is_method(py_report), borrowed()),
            R"(
            Sequential gates whose clock pin is driven by nothing.

            :type: list[hal_py.Gate]
        )");

        py_report.def_property_readonly(
            "unsupported", py::cpp_function([](const example_analysis::Report& self) { return self.unsupported; }, py::is_method(py_report)), R"(
            Sequential gate types without a clock pin, and why they could not be evaluated.

            :type: list[example_analysis.UnsupportedGateType]
        )");

        py_report.def_property_readonly(
            "sequential_gate_count", py::cpp_function([](const example_analysis::Report& self) { return self.sequential_gate_count; }, py::is_method(py_report)), R"(
            How many gates carried the ``sequential`` property in total.

            :type: int
        )");

        m.def(
            "analyze",
            [](Netlist* netlist) -> example_analysis::Report {
                auto result = example_analysis::analyze(netlist);
                if (result.is_error())
                {
                    // Raised, not logged: a Python caller that ignores a log line and reads an empty
                    // result as "nothing found" is exactly the failure this plugin is meant to avoid.
                    throw std::runtime_error(result.get_error().get());
                }
                return result.get();
            },
            py::arg("netlist"),
            R"(
            Group the sequential gates of a netlist by the net that drives their clock pin.

            :param hal_py.Netlist netlist: The netlist to analyze.
            :returns: The report.
            :rtype: example_analysis.Report
            :raises RuntimeError: If the netlist has no sequential gates, or the analysis fails.
        )");

#ifndef PYBIND11_MODULE
        return m.ptr();
#endif    // PYBIND11_MODULE
    }
}    // namespace hal
