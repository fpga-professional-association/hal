#include "hal_core/utilities/log.h"
#include "netlist_simulator_controller/simulation_process.h"

#include "netlist_simulator_controller/netlist_simulator_controller.h"
#include "netlist_simulator_controller/saleae_directory.h"
#include "netlist_simulator_controller/string_utils.h"

#include <cerrno>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <iomanip>
#include <poll.h>
#include <random>
#include <signal.h>
#include <sstream>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

namespace hal
{

    std::string SimulationProcessLog::sLogFilename = "engine_log.html";

    namespace
    {
        /// Generate 32 random hex characters, replacement for QUuid::createUuid().toString(QUuid::Id128).
        std::string randomId128()
        {
            std::random_device dev;
            std::mt19937_64 prng(dev());
            std::uniform_int_distribution<uint64_t> rand(0);
            std::ostringstream oss;
            oss << std::hex << std::setfill('0') << std::setw(16) << rand(prng) << std::setw(16) << rand(prng);
            return oss.str();
        }

        /// Split accumulated stream data into complete lines, keeping the incomplete remainder in `buffer`.
        std::vector<std::string> extractLines(std::string& buffer, bool flushRemainder)
        {
            std::vector<std::string> retval;
            size_t pos = 0;
            size_t eol;
            while ((eol = buffer.find('\n', pos)) != std::string::npos)
            {
                retval.push_back(buffer.substr(pos, eol - pos));
                pos = eol + 1;
            }
            buffer = buffer.substr(pos);
            if (flushRemainder && !buffer.empty())
            {
                retval.push_back(buffer);
                buffer.clear();
            }
            return retval;
        }
    }    // namespace

    SimulationProcessLog::SimulationProcessLog(const std::string& workdir)
        : mLogReceiver(nullptr)
    {
        std::string filename = (std::filesystem::path(workdir) / sLogFilename).string();
        mFile.open(filename, std::ios::binary);
    }

    SimulationProcessLog::~SimulationProcessLog()
    {
        if (mFile.is_open())
        {
            mFile.flush();
            mFile.close();
        }
    }

    void SimulationProcessLog::setLogReceiver(SimulationLogReceiver *logReceiver)
    {
        if (!logReceiver) return;
        mLogReceiver = logReceiver;
    }

    void SimulationProcessLog::flush()
    {
        if (mFile.is_open())
            mFile.flush();
    }

    void SimulationProcessLog::operator<< (const std::string& txt)
    {
        if (mFile.is_open())
            mFile << txt;
        if (mLogReceiver)
            mLogReceiver->handleLog(txt);
    }

    SimulationProcess::SimulationProcess(NetlistSimulatorController* controller, SimulationEngineScripted* engine)
        : mController(controller), mEngine(engine), mLineIndex(0), mNumberLines(0),
          mSaleaeDirectoryFilename(controller->get_saleae_directory_filename()), mProcessLog(nullptr)
    {
        mProcessLog = new SimulationProcessLog(mEngine->get_working_directory());
    }

    SimulationProcess::~SimulationProcess()
    {
        if (mThread.joinable()) mThread.detach();
        if (mProcessLog) delete mProcessLog;
    }

    void SimulationProcess::start()
    {
        mThread = std::thread([this]() { this->run(); });
    }

    void SimulationProcess::processFinished(bool success)
    {
        if (mController) mController->handleRunFinished(success);
    }

    void SimulationProcess::abortOnError()
    {
        mEngine->failed();
        processFinished(false);
    }

    void SimulationProcess::run()
    {
        if (mEngine->get_engine_property("ssh_server").empty())
            runLocal();
        else
            runRemote();
    }

    void SimulationProcess::runRemote()
    {
        SaleaeDirectory sd(mSaleaeDirectoryFilename);
        std::filesystem::path saleaeDirectoryPath(mSaleaeDirectoryFilename);
        std::string saleaeFilesToCopy = saleaeDirectoryPath.filename().string();
        for (int inx = 0; inx < sd.get_next_available_index(); inx++)
        {
            saleaeFilesToCopy += " digital_" + std::to_string(inx) + ".bin";
        }

        std::filesystem::path localDir(mEngine->get_working_directory());
        std::string remoteDir  = "hal_simul_" + randomId128();
        std::string shellScriptName = (localDir / "remote.sh").string();
        std::string hostname   = mEngine->get_engine_property("ssh_server");
        std::string resultFile = "waveform.vcd";
        {
            std::ofstream ff(shellScriptName, std::ios::binary);
            if (!ff.good())
            {
                log_warning("simulation_plugin", "cannot open remote script '{}' for writing", shellScriptName);
                return abortOnError();
            }
            ff << "set -x\n";    // echo commands
            ff << "HOSTNAME=" << hostname << "\n";
            ff << "LOCALDIR=" << localDir.string() << "\n";
            ff << "if ssh ${HOSTNAME} mkdir -p /tmp/" << remoteDir << "; then\n";
            ff << "   REMOTEDIR=/tmp/" << remoteDir << "\n";
            ff << "else\n";
            ff << "   ssh ${HOSTNAME} mkdir " << remoteDir << "\n";
            ff << "   REMOTEDIR=" << remoteDir << "\n";
            ff << "fi\n";
            ff << "set -e\n";    // bailout on error
            ff << "tar -czf - ${LOCALDIR} | ssh ${HOSTNAME} \"cd ${REMOTEDIR} ; tar -xzf -\"\n";
            ff << "ssh ${HOSTNAME} \"mkdir -p ${REMOTEDIR}${LOCALDIR}/saleae\"\n";
            ff << "cd " << saleaeDirectoryPath.parent_path().string() << "; tar -czf - " << saleaeFilesToCopy
               << " | ssh ${HOSTNAME} \"cd ${REMOTEDIR}${LOCALDIR}/saleae ; tar -xzf -\"\n";
            mNumberLines = mEngine->numberCommandLines();
            for (int i = 0; i < mNumberLines; i++)
            {
                std::string remoteCmd("ssh ${HOSTNAME} \"cd ${REMOTEDIR}${LOCALDIR} ;");
                for (const std::string s : mEngine->commandLine(i))
                {
                    remoteCmd += " " + s;
                }
                remoteCmd += "\"\n";
                ff << remoteCmd;
            }
            ff << "ssh " << hostname << " \"cd ${REMOTEDIR}" << localDir.string() << " ; gzip -c " << resultFile << "\" | gzip -dc > "
               << (localDir / resultFile).string() << "\n";
            ff.close();
        }
        hal::error_code ec;
        std::filesystem::permissions(shellScriptName,
                                     std::filesystem::perms::owner_read | std::filesystem::perms::owner_write | std::filesystem::perms::owner_exec,
                                     std::filesystem::perm_options::replace,
                                     ec);

        std::vector<std::string> args;
        args.push_back(shellScriptName);

        if (!runProcess("bash", args))
            return abortOnError();

        if (!mEngine->finalize())
            return abortOnError();

        processFinished(true);
    }

    void SimulationProcess::runLocal()
    {
        mNumberLines = mEngine->numberCommandLines();
        while (mLineIndex < mNumberLines)
        {
            std::vector<std::string> args;
            std::string prog;

            bool first = true;
            for (const std::string& s : mEngine->commandLine(mLineIndex))
            {
                if (first)
                    prog = s;
                else
                    args.push_back(s);
                first = false;
            }
            if (prog.empty())
                return abortOnError();

            if (!runProcess(prog, args))
                return abortOnError();

            ++mLineIndex;
        }

        if (!mEngine->finalize())
            return abortOnError();

        processFinished(true);
    }

    std::string SimulationProcess::toHtml(const std::string& txt)
    {
        std::string retval;
        for (char cc : txt)
        {
            switch (cc)
            {
                case '<':
                    retval += "&lt;";
                    break;
                case '>':
                    retval += "&gt;";
                    break;
                case '&':
                    retval += "&amp;";
                    break;
                default:
                    retval += cc;
                    break;
            }
        }
        return retval;
    }

    bool SimulationProcess::runProcess(const std::string& prog, const std::vector<std::string>& args)
    {
        std::string logCommand = "<html><body bgcolor=\"#000000\">\n<h1><font color=\"#ffffff\">" + prog;
        for (const std::string& arg : args)
            logCommand +=  " " + arg;
        logCommand += "</font></h1>\n";

        (*mProcessLog) << logCommand;
        mProcessLog->flush();

        int msecs = 30000;
        std::string timeoutAfterSec = mEngine->get_engine_property("timeout_after_sec");
        if (!timeoutAfterSec.empty())
        {
            bool ok;
            msecs = (int) simutil::to_int(timeoutAfterSec, &ok) * 1000;
            if (!ok || msecs <= 0) msecs = -1;
        }

        int outPipe[2];
        int errPipe[2];
        if (pipe(outPipe) < 0)
        {
            log_warning("simulation_plugin", "Cannot create pipe to launch process '{}'", prog);
            return false;
        }
        if (pipe(errPipe) < 0)
        {
            ::close(outPipe[0]);
            ::close(outPipe[1]);
            log_warning("simulation_plugin", "Cannot create pipe to launch process '{}'", prog);
            return false;
        }

        std::string workdir = mEngine->get_working_directory();

        pid_t pid = fork();
        if (pid < 0)
        {
            ::close(outPipe[0]);
            ::close(outPipe[1]);
            ::close(errPipe[0]);
            ::close(errPipe[1]);
            log_warning("simulation_plugin", "Cannot fork process '{}'", prog);
            return false;
        }

        if (!pid)
        {
            // child process
            ::close(outPipe[0]);
            ::close(errPipe[0]);
            if (dup2(outPipe[1], STDOUT_FILENO) < 0) _exit(127);
            if (dup2(errPipe[1], STDERR_FILENO) < 0) _exit(127);
            ::close(outPipe[1]);
            ::close(errPipe[1]);
            if (!workdir.empty() && chdir(workdir.c_str()) != 0) _exit(127);

            std::vector<char*> argv;
            argv.push_back(const_cast<char*>(prog.c_str()));
            for (const std::string& arg : args)
                argv.push_back(const_cast<char*>(arg.c_str()));
            argv.push_back(nullptr);
            execvp(prog.c_str(), argv.data());
            _exit(127);
        }

        // parent process
        ::close(outPipe[1]);
        ::close(errPipe[1]);

        std::string outBuffer;
        std::string errBuffer;
        bool timeout = false;

        auto startTime = std::chrono::steady_clock::now();

        struct pollfd pfd[2];
        pfd[0].fd = outPipe[0];
        pfd[1].fd = errPipe[0];
        bool open0 = true;
        bool open1 = true;

        char readBuffer[4096];

        while (open0 || open1)
        {
            int nfd = 0;
            if (open0)
            {
                pfd[nfd].fd     = outPipe[0];
                pfd[nfd].events = POLLIN;
                ++nfd;
            }
            if (open1)
            {
                pfd[nfd].fd     = errPipe[0];
                pfd[nfd].events = POLLIN;
                ++nfd;
            }

            int remaining = -1;
            if (msecs > 0)
            {
                int elapsed = (int) std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now() - startTime).count();
                remaining   = msecs - elapsed;
                if (remaining <= 0)
                {
                    timeout = true;
                    break;
                }
            }

            int rc = poll(pfd, nfd, remaining);
            if (rc < 0)
            {
                if (errno == EINTR) continue;
                break;
            }
            if (!rc)
            {
                timeout = true;
                break;
            }

            for (int i = 0; i < nfd; i++)
            {
                if (!(pfd[i].revents & (POLLIN | POLLHUP | POLLERR))) continue;
                bool isStdout = (pfd[i].fd == outPipe[0]);
                ssize_t nread = read(pfd[i].fd, readBuffer, sizeof(readBuffer));
                if (nread > 0)
                {
                    if (isStdout)
                        outBuffer += std::string(readBuffer, nread);
                    else
                        errBuffer += std::string(readBuffer, nread);
                }
                else
                {
                    if (isStdout)
                        open0 = false;
                    else
                        open1 = false;
                }
            }

            // report complete lines
            std::string outTxt;
            for (const std::string& line : extractLines(outBuffer, !open0))
            {
                outTxt += "<p><font color=\"#e0e0e0\">" + toHtml(line) + "</font></p>\n";
            }
            if (!outTxt.empty()) (*mProcessLog) << outTxt;

            std::string errTxt;
            for (const std::string& line : extractLines(errBuffer, !open1))
            {
                if (simutil::starts_with(line, "%Error"))
                    errTxt += "<p><font color=\"#ff4040\">";
                else if (simutil::starts_with(line, "%Warning"))
                    errTxt += "<p><font color=\"#ffe050\">";
                else
                    errTxt += "<p><font color=\"#ffffff\">";
                errTxt += toHtml(line) + "</font></p>\n";
            }
            if (!errTxt.empty()) (*mProcessLog) << errTxt;
        }

        ::close(outPipe[0]);
        ::close(errPipe[0]);

        if (timeout)
        {
            kill(pid, SIGKILL);
            int status = 0;
            waitpid(pid, &status, 0);
            (*mProcessLog) << "<p><font color=\"#ff4040\">Process timeout after " + std::to_string(msecs/1000) + " sec.</font></p>\n";
            mProcessLog->flush();
            log_warning("simulation_plugin", "Process '{}' did not terminate within {} seconds.", prog, msecs/1000);
            return false;
        }

        int status = 0;
        while (waitpid(pid, &status, 0) < 0)
        {
            if (errno != EINTR) break;
        }

        int exitCode = -99;
        if (WIFEXITED(status))
        {
            exitCode = WEXITSTATUS(status);
        }

        (*mProcessLog) << "<p>exit code " + std::to_string(exitCode) + "</p></body></html>\n";
        mProcessLog->flush();

        if (exitCode == 127)
        {
            log_warning("simulation_plugin", "Cannot start process '{}' with args '{}'", prog, simutil::join(args, " "));
            if (prog == "verilator")
                log_warning("simulation_plugin", "You might want to check whether verilator has been installed on system");
            return false;
        }

        if (exitCode != 0)
        {
            log_warning("simulation_plugin", "Process '{}' terminated with exit code {}.", prog, exitCode);
            return false;
        }

        return true;
    }
}    // namespace hal
