#include "python_shell/plugin_python_shell.h"

#include "hal_core/defines.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/utilities/program_arguments.h"
#include "hal_core/utilities/utils.h"

#include <Python.h>
#include <cstring>
#include <fstream>
#include <pybind11/pybind11.h>

namespace py = pybind11;

namespace hal
{
    extern std::unique_ptr<BasePluginInterface> create_plugin_instance()
    {
        return std::make_unique<PluginPythonShell>();
    }

    ProgramOptions CliExtensionPythonShell::get_cli_options() const
    {
        ProgramOptions description;

        description.add("--python", "start python shell");
        description.add("--python-script", "run a python script in HAL. to pass args use --python-args", {ProgramOptions::A_REQUIRED_PARAMETER});
        description.add(
            {"--python-args", "--py-args"}, "supply arguments to the python invocation. to provide multiple arguments use '\"' and separate them with spaces", {ProgramOptions::A_REQUIRED_PARAMETER});

        return description;
    }

    std::string PluginPythonShell::get_name() const
    {
        return std::string("HAL Python");
    }

    std::string PluginPythonShell::get_version() const
    {
        return std::string("0.1");
    }

    namespace
    {
        /**
         * Runs a piece of Python code and reports whether it finished without an uncaught exception.
         *
         * `PyRun_SimpleString` returns 0 on success and -1 when an exception reached the top level; in the
         * latter case it has already printed the traceback to stderr, so all that is left to do here is to
         * turn the ignored status code into something the caller can act on. Note that `sys.exit()` never
         * gets this far: CPython handles `SystemExit` by ending the process with the requested status.
         */
        bool run_python_code(const std::string& code)
        {
            return PyRun_SimpleString(code.c_str()) == 0;
        }

        /** Frees the first `count` decoded arguments and the array holding them. */
        void free_python_argv(wchar_t** argv, int count)
        {
            for (int i = 0; i < count; ++i)
            {
                PyMem_RawFree(argv[i]);
            }
            delete[] argv;
        }

        /**
         * Binds the netlist HAL loaded to the name `netlist` in the namespace scripts run in.
         *
         * `PyRun_SimpleString` and the interactive shell both execute in `__main__`, so putting the
         * object there is what makes `netlist` a plain global for the script -- the same name the GUI
         * console used to provide.
         *
         * The object is handed out as a borrowed reference: HAL owns the netlist and destroys it after
         * the interpreter is gone, so Python must not take ownership of it. Nothing here can be done
         * before `hal_py` has been imported, since that import is what registers the `Netlist` type
         * with pybind11.
         *
         * @param[in] netlist - The netlist to expose, which must not be `nullptr`.
         * @param[out] error - A description of what went wrong, set only when this returns `false`.
         * @returns `true` if the name was bound, `false` otherwise.
         */
        bool inject_netlist(Netlist* netlist, std::string& error)
        {
            try
            {
                py::module_ main_module      = py::module_::import("__main__");
                main_module.attr("netlist")  = py::cast(netlist, py::return_value_policy::reference);
            }
            catch (const std::exception& e)
            {
                error = e.what();
                return false;
            }
            catch (...)
            {
                error = "unknown exception";
                return false;
            }

            return true;
        }
    }    // namespace

    bool PluginPythonShell::exec(ProgramArguments& args)
    {
        /* The result of this call becomes the exit code of HAL (see UIPluginInterface::exec), so every
         * error below has to travel back to the caller as false. */

        // the script is located and read before the interpreter is touched, so that a bad path fails
        // without any half-initialized Python state to clean up
        const bool run_script = args.is_option_set("--python-script");
        std::string script_source;
        std::string file_path;

        if (run_script)
        {
            file_path = args.get_parameter("--python-script");
            if (!std::filesystem::exists(file_path) || std::filesystem::is_directory(file_path) || !utils::ends_with(file_path, std::string(".py")))
            {
                log_error(get_name(), "'{}' is not a python script file", file_path);
                return false;
            }

            std::ifstream stream(file_path);
            if (!stream.is_open())
            {
                log_error(get_name(), "cannot open python script file '{}'", file_path);
                return false;
            }

            stream.seekg(0, std::ios::end);
            script_source.reserve(stream.tellg());
            stream.seekg(0, std::ios::beg);
            script_source.assign((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());

            if (stream.bad())
            {
                log_error(get_name(), "cannot read python script file '{}'", file_path);
                return false;
            }
        }

        int argc       = 0;
        wchar_t** argv = nullptr;

        // python needs arguments as argc/argv, so we convert them here
        if (args.is_option_set("--python-args"))
        {
            std::vector<std::string> py_args;
            auto py_arg_str = args.get_parameter("--python-args");

            if (py_arg_str.find(' ') != std::string::npos)
            {
                py_args = utils::split(py_arg_str, ' ');
            }
            else
            {
                py_args.push_back(py_arg_str);
            }

            /* copy command line interface options */
            argc = py_args.size();
            argv = new wchar_t*[argc];

            /* pass all parameters to python shell */
            for (int i = 0; i < argc; i++)
            {
                argv[i] = Py_DecodeLocale(py_args[i].c_str(), nullptr);
                if (argv[i] == nullptr)
                {
                    log_error(get_name(), "unable to convert argument '{}' for Python", py_args[i]);
                    free_python_argv(argv, i);
                    return false;
                }
            }
        }

        // initiliaze python shell
        Py_Initialize();

        PySys_SetArgv(argc, argv);

        bool success = true;

        // the shell is unusable without hal_py, so a failure here is reported instead of leaving the
        // script to fail later with a confusing NameError
        const std::vector<std::string> setup_statements = {
            "import sys",
            "sys.path.append(\"" + utils::get_library_directory().string() + "\")",
            "from hal_py import *",
            "import hal_py",
        };

        for (const auto& statement : setup_statements)
        {
            if (!run_python_code(statement))
            {
                log_error(get_name(), "cannot initialize the Python environment, '{}' raised an exception", statement);
                success = false;
                break;
            }
        }

        // a netlist that main() loaded is put in front of the script under the name it had in the GUI
        // console; without it a script started with --project-dir would fail on `netlist` being undefined
        if (success && m_netlist != nullptr)
        {
            std::string injection_error;
            if (!inject_netlist(m_netlist, injection_error))
            {
                log_error(get_name(), "cannot expose the loaded netlist to Python: {}", injection_error);
                success = false;
            }
        }

        // changing cwd not required
        // PyRun_SimpleString("import os");
        // PyRun_SimpleString(("os.chdir(\""+ std::filesystem::current_path().string() +"\")").c_str());

        if (success)
        {
            if (run_script)
            {
                if (!run_python_code(script_source))
                {
                    log_error(get_name(), "the python script '{}' terminated with an exception", file_path);
                    success = false;
                }
            }
            else
            {
                // the interactive shell keeps running until the user ends it; a normal exit yields 0,
                // anything else is a failed run
                const int py_main_status = Py_Main(argc, argv);
                if (py_main_status != 0)
                {
                    log_error(get_name(), "the python shell terminated with status {}", py_main_status);
                    success = false;
                }
            }
        }

        Py_Finalize();

        /* cleanup of copied command line interface options */
        free_python_argv(argv, argc);

        return success;
    }
}    // namespace hal
