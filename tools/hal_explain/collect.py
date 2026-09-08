"""The one module that touches ``hal_py``: produce the inputs a model needs.

``hal_explain`` composes results, so somebody has to produce them.  ``collect``
is that somebody, and it runs *inside* HAL (see :mod:`hal_explain.run_in_hal`):

1. load the netlist and write the :mod:`hal_explain.inventory` snapshot -- the
   complete gate list, the wiring and the gate-type semantics that let the
   composition run later on a plain interpreter;
2. run dataflow analysis and write its findings document through the existing
   ``hal_findings.adapters.dataflow`` adapter;
3. hand DANA's groups to ``module_identification`` as its known registers --
   which is how the plugin is meant to be driven -- and write that findings
   document through :mod:`hal_explain.adapters.module_identification`.

Every step is optional and every failure is *recorded*, never swallowed: a
plugin that is not in this build, a plugin that returns ``None``, an adapter
that raises -- each becomes an entry in ``result.json`` with a reason, and the
composition then simply has one source fewer and says so.  A collect run that
half-worked must not look like a design with less structure in it.

``hal_fsm`` is deliberately not run from here.  It is a whole workflow with its
own limits, timeouts and configuration (``tools/hal_fsm``), and its findings
document is an ordinary ``--findings`` input to ``hal_explain compose``.
"""

import os
import sys
import time
import traceback

__all__ = ["CollectError", "collect", "execute"]

RESULT_VERSION = "1.0.0"


class CollectError(RuntimeError):
    """The collect run could not start at all."""


def _record(records, name, status, detail=None, path=None):
    entry = {"step": name, "status": status}
    if detail:
        entry["detail"] = str(detail)
    if path:
        entry["path"] = str(path)
    records.append(entry)
    return entry


def _import_plugin(name):
    from hal_viz.halenv import HalUnavailable, import_plugin

    try:
        return import_plugin(name), None
    except HalUnavailable as exc:
        return None, str(exc)


def _run_dataflow(hal_py, netlist, output_dir, artifact_id, netlist_path, records,
                  min_group_size=None):
    """Run DANA and write its findings document.  Returns ``(result, path)``."""
    from hal_findings import serialize as findings_serialize
    from hal_findings.adapters import dataflow as dataflow_adapter

    module, error = _import_plugin("dataflow")
    if module is None:
        _record(records, "dataflow", "unavailable", error)
        return None, None

    try:
        configuration = module.Configuration(netlist)
        configuration = configuration.with_flip_flops()
        if min_group_size:
            configuration = configuration.with_min_group_size(int(min_group_size))
        started = time.time()
        result = module.analyze(configuration)
        duration = time.time() - started
    except Exception as exc:  # pragma: no cover - needs HAL
        _record(records, "dataflow", "error", "{}\n{}".format(exc, traceback.format_exc()))
        return None, None

    if result is None:
        _record(
            records,
            "dataflow",
            "no_result",
            "dataflow.analyze returned None; the plugin logged the reason",
        )
        return None, None

    control_pin_types = ()
    if hasattr(hal_py, "PinType"):
        control_pin_types = tuple(
            (label, getattr(hal_py.PinType, label))
            for label in ("clock", "enable", "reset", "set")
            if hasattr(hal_py.PinType, label)
        )

    try:
        document = dataflow_adapter.build_document(
            result,
            artifact_id=artifact_id,
            configuration=configuration,
            plugin_version=str(getattr(module, "__version__", "unknown")),
            control_pin_types=control_pin_types,
            netlist_path=netlist_path,
            duration_s=duration,
        )
        path = os.path.join(output_dir, "findings-dataflow.json")
        findings_serialize.write_document(document, path)
    except Exception as exc:  # pragma: no cover - needs HAL
        _record(records, "dataflow", "adapter_error", "{}\n{}".format(exc, traceback.format_exc()))
        return result, None

    _record(records, "dataflow", "ok", path=path)
    return result, path


def _known_registers(dataflow_result):
    """DANA's groups as the ``known_registers`` module_identification wants."""
    from hal_findings.adapters.common import call

    groups = call(dataflow_result, "get_groups", default={}) or {}
    registers = []
    for group_id in sorted(groups, key=int):
        gates = sorted(
            groups[group_id], key=lambda gate: call(gate, "get_id", default=0)
        )
        if gates:
            registers.append(list(gates))
    return registers


def _run_module_identification(netlist, output_dir, artifact_id, netlist_path, records,
                               dataflow_result=None, max_control_signals=None):
    from hal_findings import serialize as findings_serialize

    from .adapters import module_identification as modid_adapter

    module, error = _import_plugin("module_identification")
    if module is None:
        _record(records, "module_identification", "unavailable", error)
        return None

    registers = _known_registers(dataflow_result) if dataflow_result is not None else []
    try:
        configuration = module.Configuration(netlist)
        if registers:
            configuration = configuration.with_known_registers(registers)
        if max_control_signals:
            configuration = configuration.with_max_control_signals(int(max_control_signals))
        started = time.time()
        result = module.execute(configuration)
        duration = time.time() - started
    except Exception as exc:  # pragma: no cover - needs HAL
        _record(
            records,
            "module_identification",
            "error",
            "{}\n{}".format(exc, traceback.format_exc()),
        )
        return None

    if result is None:
        _record(
            records,
            "module_identification",
            "no_result",
            "module_identification.execute returned None; the plugin logged the reason. "
            "This is not evidence that the netlist contains no arithmetic.",
        )
        return None

    try:
        document = modid_adapter.build_document(
            result,
            artifact_id=artifact_id,
            configuration={
                "known_registers": len(registers),
                "max_control_signals": max_control_signals,
            },
            plugin_version=str(getattr(module, "__version__", "unknown")),
            netlist_path=netlist_path,
            duration_s=duration,
        )
        path = os.path.join(output_dir, "findings-module-identification.json")
        findings_serialize.write_document(document, path)
    except Exception as exc:  # pragma: no cover - needs HAL
        _record(
            records,
            "module_identification",
            "adapter_error",
            "{}\n{}".format(exc, traceback.format_exc()),
        )
        return None

    _record(records, "module_identification", "ok", path=path)
    return path


def collect(request):
    """Do the in-HAL half of a run.  Returns the result record as a dict."""
    from hal_cdc.netlist_view import from_hal_netlist
    from hal_viz.halenv import (
        HalUnavailable,
        NetlistLoadError,
        import_hal_py,
        load_all_plugins,
        load_netlist,
    )

    from . import __version__, inventory as inventory_module, serialize

    output_dir = request["output_dir"]
    os.makedirs(output_dir, exist_ok=True)
    artifact_id = request.get("artifact_id") or "netlist"
    netlist_path = request["netlist"]
    records = []

    hal_py = import_hal_py(request.get("hal_lib") or ())
    try:
        load_all_plugins(hal_py)
    except HalUnavailable as exc:
        _record(records, "plugins", "unavailable", str(exc))

    try:
        netlist = load_netlist(hal_py, netlist_path, request.get("gate_library"))
    except NetlistLoadError as exc:
        raise CollectError(str(exc))

    view = from_hal_netlist(netlist)
    inventory = inventory_module.from_netlist_view(
        view, artifact_id=artifact_id, path=netlist_path
    )
    inventory_path = os.path.join(output_dir, "inventory.json")
    serialize.write_json(inventory.to_json(), inventory_path)
    _record(records, "inventory", "ok", path=inventory_path)

    findings_paths = []
    dataflow_result = None
    if request.get("dataflow", True):
        dataflow_result, path = _run_dataflow(
            hal_py,
            netlist,
            output_dir,
            artifact_id,
            netlist_path,
            records,
            min_group_size=request.get("min_group_size"),
        )
        if path:
            findings_paths.append(path)
    else:
        _record(records, "dataflow", "skipped", "disabled by the request")

    if request.get("module_identification", True):
        path = _run_module_identification(
            netlist,
            output_dir,
            artifact_id,
            netlist_path,
            records,
            dataflow_result=dataflow_result,
            max_control_signals=request.get("max_control_signals"),
        )
        if path:
            findings_paths.append(path)
    else:
        _record(records, "module_identification", "skipped", "disabled by the request")

    failed = [entry for entry in records if entry["status"] not in ("ok", "skipped")]
    result = {
        "result_version": RESULT_VERSION,
        "analysis": "hal_explain.collect",
        "producer": {"name": "hal_explain.collect", "version": __version__},
        "status": "ok" if not failed else "partial",
        "artifacts": {
            "inventory": inventory_path,
            "findings": findings_paths,
        },
        "steps": records,
        "gate_count": len(inventory.gates),
        "net_count": len(inventory.nets),
    }
    if failed:
        result["notes"] = [
            "{} step(s) did not produce a document: {}. The composed model will have "
            "fewer sources; that is a gap in the evidence, not a simpler design.".format(
                len(failed), ", ".join(sorted(entry["step"] for entry in failed))
            )
        ]
    serialize.write_json(result, os.path.join(output_dir, request.get("result_file", "result.json")))
    return result


def execute(request):
    """Entry point used by :mod:`hal_explain.run_in_hal`; returns an exit code."""
    from . import serialize

    try:
        result = collect(request)
    except CollectError as exc:
        sys.stderr.write("[hal_explain] {}\n".format(exc))
        try:
            serialize.write_json(
                {
                    "result_version": RESULT_VERSION,
                    "analysis": "hal_explain.collect",
                    "status": "error",
                    "artifacts": {},
                    "steps": [],
                    "error": {"kind": "invalid_input", "message": str(exc)},
                },
                os.path.join(request["output_dir"], request.get("result_file", "result.json")),
            )
        except Exception:  # pragma: no cover - the caller still fails the run
            pass
        return 2
    except Exception as exc:  # pragma: no cover - needs HAL
        sys.stderr.write("[hal_explain] {}\n{}\n".format(exc, traceback.format_exc()))
        return 1
    return 0 if result["status"] == "ok" else 1
