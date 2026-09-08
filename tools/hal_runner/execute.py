"""Running one step as a subprocess, with a limit that actually bites.

Why a process at all: a HAL analysis is C++ code with no cancellation points.
There is no cooperative way to stop ``dataflow.analyze`` half way, and a Python
thread cannot interrupt it either.  A process can be killed, so the process
boundary *is* the cancellation mechanism -- and it doubles as a blast shield,
because a segfault or an OOM kill inside a plugin costs one step instead of the
whole run.

What this module guarantees:

* the child is put in its own process group (POSIX) or job-like process group
  (Windows), so killing it kills the plugin threads it spawned rather than
  leaving orphans holding the output directory open;
* a timeout escalates: SIGTERM first, then SIGKILL after a grace period, and
  the result records which one it took;
* stdout and stderr always land in files, and those files survive the failure
  they document -- HAL's own log lines are usually the only explanation of why
  a plugin gave up;
* a memory limit is applied with :func:`resource.setrlimit` where the platform
  has one, and is reported as *not enforced* where it does not, because
  silently ignoring a declared limit is worse than not offering it.
"""

import os
import signal
import subprocess
import sys
import time

__all__ = [
    "ExecutionResult",
    "ProcessExecutor",
    "memory_limit_supported",
    "KILL_GRACE_SECONDS",
]

#: How long a timed-out child gets between the polite signal and the fatal one.
KILL_GRACE_SECONDS = 5.0

try:  # pragma: no cover - platform dependent
    import resource
except ImportError:  # pragma: no cover - Windows
    resource = None


def memory_limit_supported():
    """True when :class:`ProcessExecutor` can enforce ``memory_mb``."""
    return resource is not None and hasattr(resource, "RLIMIT_AS")


class ExecutionResult(object):
    """The outcome of one subprocess: how it ended, how long it took, where the logs are."""

    def __init__(
        self,
        command,
        exit_code,
        timed_out,
        duration_s,
        stdout_path,
        stderr_path,
        killed=False,
        memory_limit_enforced=False,
        timeout_s=None,
        memory_mb=None,
    ):
        self.command = list(command)
        self.exit_code = exit_code
        self.timed_out = bool(timed_out)
        self.duration_s = duration_s
        self.stdout_path = stdout_path
        self.stderr_path = stderr_path
        self.killed = bool(killed)
        self.memory_limit_enforced = bool(memory_limit_enforced)
        self.timeout_s = timeout_s
        self.memory_mb = memory_mb

    @property
    def succeeded(self):
        return self.exit_code == 0 and not self.timed_out

    def as_json(self):
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "killed": self.killed,
            "duration_s": round(float(self.duration_s), 3),
            "timeout_s": self.timeout_s,
            "memory_mb": self.memory_mb,
            "memory_limit_enforced": self.memory_limit_enforced,
        }

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<ExecutionResult exit={} timed_out={} {:.3f}s>".format(
            self.exit_code, self.timed_out, self.duration_s
        )


def _memory_limit_preexec(memory_mb):  # pragma: no cover - POSIX child process
    """Return a ``preexec_fn`` that caps the child's address space."""
    limit = int(memory_mb * 1024 * 1024)

    def apply_limit():
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        # A new session makes the child a process group leader, so the whole
        # subtree can be signalled at once.
        os.setsid()

    return apply_limit


def _new_session():  # pragma: no cover - POSIX child process
    os.setsid()


def tail(path, limit=4000):
    """Last ``limit`` characters of a log file; '' when it is missing or unreadable.

    Used to put the reason a step failed into its diagnostic record instead of
    only into a file nobody opens.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    if len(text) <= limit:
        return text
    return "...\n" + text[-limit:]


class ProcessExecutor(object):
    """Runs one command with a wall-clock limit and retained logs.

    The runner talks to this object through :meth:`execute` only, which is what
    lets the unit tests drive the whole orchestrator with a stub executor and no
    HAL build.
    """

    def __init__(self, kill_grace_s=KILL_GRACE_SECONDS):
        self.kill_grace_s = kill_grace_s

    def execute(
        self,
        command,
        cwd=None,
        env=None,
        timeout_s=None,
        memory_mb=None,
        stdout_path=None,
        stderr_path=None,
    ):
        """Run ``command`` and return an :class:`ExecutionResult`.

        Never raises for a failing child: a nonzero exit, a timeout and a crash
        are all results, and the caller decides what they mean.
        """
        command = [str(part) for part in command]
        for path in (stdout_path, stderr_path):
            if path:
                parent = os.path.dirname(os.path.abspath(path))
                if parent:
                    os.makedirs(parent, exist_ok=True)

        popen_kwargs = {
            "cwd": cwd,
            "env": env,
            "stdin": subprocess.DEVNULL,
        }
        enforced = False
        if os.name == "posix":
            if memory_mb and memory_limit_supported():
                popen_kwargs["preexec_fn"] = _memory_limit_preexec(memory_mb)
                enforced = True
            else:
                popen_kwargs["preexec_fn"] = _new_session
        elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):  # pragma: no cover - Windows
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

        stdout_handle = open(stdout_path, "wb") if stdout_path else None
        stderr_handle = open(stderr_path, "wb") if stderr_path else None
        started = time.time()
        try:
            process = subprocess.Popen(
                command,
                stdout=stdout_handle or subprocess.DEVNULL,
                stderr=stderr_handle or subprocess.DEVNULL,
                **popen_kwargs
            )
        except OSError as exc:
            if stdout_handle:
                stdout_handle.close()
            if stderr_handle:
                stderr_handle.close()
            if stderr_path:
                with open(stderr_path, "a", encoding="utf-8") as handle:
                    handle.write(
                        "hal_runner could not start {!r}: {}\n".format(command[0], exc)
                    )
            return ExecutionResult(
                command,
                exit_code=127,
                timed_out=False,
                duration_s=time.time() - started,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                memory_limit_enforced=enforced,
                timeout_s=timeout_s,
                memory_mb=memory_mb,
            )

        timed_out = False
        killed = False
        try:
            exit_code = process.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            killed = self._terminate(process)
            exit_code = process.returncode
            if exit_code is None:  # pragma: no cover - the child outlived SIGKILL
                exit_code = -signal.SIGKILL if hasattr(signal, "SIGKILL") else -9
        finally:
            duration = time.time() - started
            if stdout_handle:
                stdout_handle.close()
            if stderr_handle:
                stderr_handle.close()

        if timed_out and stderr_path:
            with open(stderr_path, "a", encoding="utf-8") as handle:
                handle.write(
                    "\n[hal_runner] step exceeded its {}s limit and was {} after "
                    "{:.1f}s\n".format(
                        timeout_s, "killed" if killed else "terminated", duration
                    )
                )

        return ExecutionResult(
            command,
            exit_code=exit_code,
            timed_out=timed_out,
            duration_s=duration,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            killed=killed,
            memory_limit_enforced=enforced,
            timeout_s=timeout_s,
            memory_mb=memory_mb,
        )

    def _terminate(self, process):
        """Stop ``process`` and its group; return True if it took SIGKILL."""
        self._signal_group(process, signal.SIGTERM)
        try:
            process.wait(timeout=self.kill_grace_s)
            return False
        except subprocess.TimeoutExpired:
            pass
        self._signal_group(process, getattr(signal, "SIGKILL", signal.SIGTERM))
        try:
            process.wait(timeout=self.kill_grace_s)
        except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
            pass
        return True

    @staticmethod
    def _signal_group(process, sig):
        """Signal the child's whole process group, falling back to the child alone."""
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            if sig == getattr(signal, "SIGKILL", None):
                process.kill()
            else:
                process.terminate()
        except OSError:  # pragma: no cover - already gone
            pass
        if os.name == "nt":  # pragma: no cover - Windows
            # terminate() only ends the child; taskkill takes the tree with it.
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                pass


def python_command(script, *arguments):  # pragma: no cover - test helper
    """A command running ``script`` with the current interpreter."""
    return [sys.executable, str(script)] + [str(argument) for argument in arguments]
