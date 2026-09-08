#include "netlist_simulator_controller/vcd_serializer.h"

#include "hal_core/netlist/net.h"
#include "hal_core/utilities/log.h"
#include "netlist_simulator_controller/netlist_simulator_controller.h"
#include "netlist_simulator_controller/saleae_file.h"
#include "netlist_simulator_controller/saleae_parser.h"
#include "netlist_simulator_controller/saleae_writer.h"
#include "netlist_simulator_controller/string_utils.h"
#include "netlist_simulator_controller/wave_data.h"

#include <cassert>
#include <cstring>
#include <filesystem>
#include <math.h>
#include <regex>

namespace hal
{

    const int maxErrorMessages = 3;

    namespace
    {
        /// Absolute path of `dirname`, resolved against the current working directory if needed (QDir::absoluteFilePath).
        std::filesystem::path absoluteDirectory(const std::string& dirname)
        {
            std::filesystem::path retval(dirname);
            if (retval.is_relative())
            {
                hal::error_code ec;
                std::filesystem::path cwd = std::filesystem::current_path(ec);
                if (!ec)
                {
                    retval = cwd / retval;
                }
            }
            return retval;
        }
    }    // namespace

    VcdSerializerElement::VcdSerializerElement(int inx, const WaveData* wd) : mIndex(inx), mData(wd), mTime(0), mValue(SaleaeDataTuple::sReadError)
    {
        ;
    }

    std::string VcdSerializerElement::name() const
    {
        return mData->name();
    }

    bool VcdSerializerElement::hasData() const
    {
        return mValue != SaleaeDataTuple::sReadError;
    }

    void VcdSerializerElement::reset()
    {
        mValue = SaleaeDataTuple::sReadError;
        mTime  = 0;
    }

    std::string VcdSerializerElement::charCode() const
    {
        std::string retval;
        int z          = mIndex;
        char firstChar = '!';
        do
        {
            retval += (char)(firstChar + z % 92);
            z /= 92;
            if (z < 92)
            {
                firstChar = ' ';    // most significant digit must be non-zero
            }
        } while (z > 0);
        return retval;
    }

    //----------------------------------
    VcdSerializer::VcdSerializer(const std::string& workdir, bool saleae_cli, NetlistSimulatorController* controller)
        : mTime(0), mFirstTimestamp(0), mLastTimestamp(0), mTimeShift(0), mSaleaeWriter(nullptr), mWorkdir(workdir), mSaleae(false), mLastProgress(-1),
          mController(controller)
    {
        memset(mErrorCount, 0, sizeof(mErrorCount));
        if (!mWorkdir.empty() && !saleae_cli)
        {
            mSaleaeDirectoryFilename = (absoluteDirectory(mWorkdir) / "saleae" / "saleae.json").string();
        }
        else
        {
            mSaleaeDirectoryFilename = workdir + "/saleae.json";
        }
    }

    void VcdSerializer::deleteFiles()
    {
        mSaleaeFiles.clear();
        mAbbrevByName.clear();
        memset(mErrorCount, 0, sizeof(mErrorCount));
    }

    void VcdSerializer::writeVcdEvent(std::ofstream& of)
    {
        if ((u64) mTime < mFirstTimestamp || (u64) mTime > mLastTimestamp)
        {
            return;
        }
        bool first = true;
        for (VcdSerializerElement* vse : mWriteElements)
        {
            if (vse->hasData())
            {
                uint64_t ts = (vse->time() == 0) ? 0 : vse->time() - mTimeShift;
                if (first)
                {
                    of << '#' << std::to_string(ts) << '\n';
                    first = false;
                }
                of << std::to_string(vse->value()) << vse->charCode() << '\n';
                vse->reset();
            }
        }
    }

    bool VcdSerializer::exportCsv(const std::string& filename, const std::vector<const WaveData*>& waves)
    {
        if (waves.empty())
        {
            return false;
        }
        SaleaeParser parser(mSaleaeDirectoryFilename);
        std::ofstream of(filename, std::ios::binary);
        if (!of.good())
        {
            return false;
        }

        mTime = 0;
        int n = (int) waves.size();

        int* values = new int [n];
        memset(values, 0, n*sizeof(int));

        of << "Time";
        for (int i = 0; i < n; i++)
        {
            const WaveData* wd = waves.at(i);
            of << ",\"" << wd->name() << "\"";
            parser.register_callback(
                wd->name(),
                wd->id(),
                [this,&of,values,n](const void* obj, uint64_t t, int val) {
                    if (t != (uint64_t) mTime)
                    {
                        of << std::to_string((uint64_t)mTime);
                        for (int j=0; j<n; j++)
                        {
                            of << ",";
                            of << std::to_string(values[j]);
                        }
                        of << "\n";
                        mTime = t;
                    }
                    *((int*)obj) = val;
                },
                values+i);
        }

        while (parser.next_event())
        {;}

        if (mTime)
        {
            of << std::to_string((uint64_t)mTime);
            for (int j=0; j<n; j++)
            {
                of << ",";
                of << std::to_string(values[j]);
            }
        }

        of << "\n";

        delete [] values;

        return true;
    }


    bool VcdSerializer::exportVcd(const std::string& filename, const std::vector<const WaveData*>& waves, u32 startTime, u32 endTime, u32 timeSift)
    {
        mTimeShift      = timeSift;
        mFirstTimestamp = startTime;
        mLastTimestamp  = endTime - mTimeShift;
        if (waves.empty())
        {
            return false;
        }
        SaleaeParser parser(mSaleaeDirectoryFilename);
        std::ofstream of(filename, std::ios::binary);
        if (!of.good())
        {
            return false;
        }

        mTime = 0;
        of << "$scope module top_module $end\n";

        int n = (int) waves.size();

        for (int i = 0; i < n; i++)
        {
            const WaveData* wd        = waves.at(i);
            VcdSerializerElement* vse = new VcdSerializerElement(i, wd);
            mWriteElements.push_back(vse);
            parser.register_callback(
                wd->name(),
                wd->id(),
                [this, &of](const void* obj, uint64_t t, int val) {
                    VcdSerializerElement* vse = (VcdSerializerElement*)obj;
                    if ((int)t - (int)mTimeShift < 0)
                    {
                        vse->setEvent(0, val);
                    }
                    else
                    {
                        if (t != (uint64_t) mTime)
                        {
                            writeVcdEvent(of);
                            mTime = t - mTimeShift;
                        }
                        vse->setEvent(t, val);
                    }
                },
                vse);
            of << "$var wire 1 " << vse->charCode() << " " << vse->name() << " $end\n";
        }

        of << "$upscope $end\n$enddefinitions $end\n";

        while (parser.next_event())
        {
            ;
        }
        for (VcdSerializerElement* vse : mWriteElements)
        {
            delete vse;
        }
        mWriteElements.clear();

        return true;
    }

    bool VcdSerializer::parseVcdDataNonDecimal(const std::string& line, int base)
    {
        std::vector<std::string> sl = simutil::split(line, ' ');
        if (sl.size() != 2)
        {
            return false;
        }
        bool ok;
        int val = (int) simutil::to_uint(sl.at(0), &ok, base);
        if (!ok)
        {
            if (mErrorCount[0]++ < maxErrorMessages)
            {
                log_warning("waveform", "Cannot parse VCD data value '{}'", sl.at(0));
            }
            val = 0;
        }
        storeValue(val, sl.at(1));
        // [return ok] return statement if we want to bail out upon data parse error
        return true;    // ignore parse errors
    }

    bool VcdSerializer::parseVcdDataline(char* buf, int len)
    {
        int pos = 0;
        while (len)
        {
            int val = -1;
            switch (*(buf + pos))
            {
                case 'b':
                    return true;    // parseVcdDataNonDecimal(std::string(buf+pos+1,len-1),2);
                case 'o':
                    return true;    //parseVcdDataNonDecimal(std::string(buf+pos+1,len-1),8);
                case 'h':
                    return true;    // parseVcdDataNonDecimal(std::string(buf+pos+1,len-1),16);
                case '$': {
                    std::string testKeyword = std::string(buf + pos + 1, len - 1);
                    if (simutil::starts_with(testKeyword, "dumpvars") || simutil::starts_with(testKeyword, "end"))
                    {
                        return true;
                    }
                    return false;
                }
                case '#': {
                    bool ok;
                    mTime = (int) simutil::to_uint(std::string(buf + pos + 1, len - 1), &ok);
                    assert(ok);
                    return true;
                }
                case 'x':
                    val = -1;
                    break;
                case 'z':
                    val = -2;
                    break;
                case '0':
                case '1':
                case '2':
                case '3':
                case '4':
                case '5':
                case '6':
                case '7':
                    val = *(buf + pos) - '0';
                    break;
                default:
                    log_warning("waveform", "Cannot parse dataline entries starting with '{}' : '{}'", *(buf + pos), buf);
                    return false;
            }
            int p = pos + 1;
            while (p < len && buf[p] > ' ')
            {
                ++p;
            }
            assert(p > pos + 1);
            int abbrevLen = p - pos - 1;
            storeValue(val, std::string(buf + pos + 1, abbrevLen));
            pos = p;
            len -= (abbrevLen + 1);
            while (buf[pos] == ' ' && len > 0)
            {
                pos++;
                len--;
            }
        }
        return true;
    }

    void VcdSerializer::storeValue(int val, const std::string& abrev)
    {
        SaleaeOutputFile* sof = simutil::map_value(mSaleaeFiles, abrev, (SaleaeOutputFile*)nullptr);
        if (!sof)
        {
            return;
        }
        sof->writeTimeValue(mTime, val);
    }

    bool VcdSerializer::parseCsvHeader(char* buf)
    {
        int icol  = 0;
        char* pos = buf;
        bool loop = (*pos != 0);
        std::string abbrev;
        while (loop)
        {
            std::string header;
            while (*pos && *pos != ',' && *pos != '\n')
            {
                header += *(pos++);
            }
            loop = (*(pos++) == ',');
            if (!icol)
            {
                if (mSaleae)
                {
                    abbrev = header;
                }
            }
            else
            {
                bool ok;
                if (!mSaleae)
                {
                    abbrev = std::to_string(icol);
                }
                std::string name;
                u32 id = (u32) simutil::to_uint(simutil::trim(header), &ok);
                if (ok && id)
                {
                    name = "net[" + std::to_string(id) + "]";
                }
                else
                {
                    name  = simutil::trim(header);
                    int n = (int) name.size() - 1;
                    if (n < 2 || name.at(0) != '"' || name.at(n) != '"')
                    {
                        return false;
                    }
                    name = name.substr(1, n - 1);
                    id   = 0;
                }
                if (!name.empty() || id)
                {
                    SaleaeOutputFile* sof = mSaleaeWriter->add_or_replace_waveform(name, id);
                    if (!sof)
                    {
                        return false;
                    }
                    mSaleaeFiles[abbrev] = sof;
                }
                else
                {
                    return false;
                }
            }
            icol++;
        }

        return true;
    }

    bool VcdSerializer::parseCsvDataline(char* buf, int dataLineIndex)
    {
        int icol = 0;
        bool ok;
        char* pos = buf;
        bool loop = (*pos != 0);
        while (loop)
        {
            std::string value;
            while (*pos && *pos != ',' && *pos != '\n')
            {
                value += *(pos++);
            }
            loop = (*(pos++) == ',');
            if (!value.empty())
            {
                if (icol)
                {
                    int ival = 0;
                    ok       = true;
                    if (value.size() == 1)
                    {
                        switch (value.at(0))
                        {
                            case '0':
                                break;
                            case '1':
                                ival = 1;
                                break;
                            default:
                                ival = (int) simutil::to_int(value, &ok);
                                break;
                        }
                    }
                    else
                    {
                        ival = (int) simutil::to_int(value, &ok);
                    }
                    if (!ok)
                    {
                        return false;
                    }

                    bool wdInsert = false;
                    if (icol >= (int) mLastValue.size())
                    {
                        while (icol > (int) mLastValue.size())
                        {
                            mLastValue.push_back(-99);
                        }
                        mLastValue.push_back(ival);
                        wdInsert = true;
                    }
                    else if (mLastValue.at(icol) != ival)
                    {
                        mLastValue[icol] = ival;
                        wdInsert         = true;
                    }

                    if (wdInsert)
                    {
                        SaleaeOutputFile* sof = simutil::map_value(mSaleaeFiles, std::to_string(icol), (SaleaeOutputFile*)nullptr);
                        if (!sof)
                        {
                            return false;
                        }
                        sof->writeTimeValue(mTime, ival);
                    }
                }
                else
                {
                    // time
                    double tDouble = simutil::to_double(value, &ok);
                    if (!ok)
                    {
                        return false;
                    }
                    u64 tInt = (u64)floor(tDouble * SaleaeParser::sTimeScaleFactor + 0.5);
                    if (!dataLineIndex)
                    {
                        mFirstTimestamp = tInt;
                        mTime           = 0;
                    }
                    else
                    {
                        mTime = tInt - mFirstTimestamp;
                    }
                }
            }
            icol++;
        }
        return true;
    }

    bool VcdSerializer::importCsv(const std::string& csvFilename, const std::string& workdir, const std::vector<const Net*>& onlyNets, u64 timeScale)
    {
        mWorkdir = workdir.empty() ? std::filesystem::current_path().string() : workdir;
        mLastValue.clear();
        deleteFiles();
        mTime   = 0;
        mSaleae = false;

        SaleaeParser::sTimeScaleFactor = timeScale;

        std::ifstream ff(csvFilename, std::ios::binary);
        if (!ff.good())
        {
            log_warning("waveform", "Cannot open CSV input file '{}'.", csvFilename);
            return false;
        }

        createSaleaeDirectory();
        mSaleaeWriter = new SaleaeWriter(mSaleaeDirectoryFilename);

        bool retval = parseCsvInternal(ff, csvFilename, onlyNets);

        delete mSaleaeWriter;
        mSaleaeWriter = nullptr;
        mSaleaeFiles.clear();

        if (retval)
        {
            emitImportDone();
        }
        return retval;
    }

    void VcdSerializer::emitProgress(double step, double max)
    {
        if (!mController)
        {
            return;
        }
        int percent = floor(step * 100 / max + 0.5);
        if (percent == mLastProgress)
        {
            return;
        }
        mController->emitLoadProgress(percent);
        mLastProgress = percent;
    }

    void VcdSerializer::emitImportDone()
    {
        if (!mController)
        {
            return;
        }
        mController->emitLoadProgress(-1);
        mLastProgress = -1;
    }

    void VcdSerializer::createSaleaeDirectory()
    {
        std::filesystem::path saleaeDir = absoluteDirectory(mWorkdir) / "saleae";
        hal::error_code ec;
        std::filesystem::create_directories(saleaeDir, ec);
        mSaleaeDirectoryFilename = (saleaeDir / "saleae.json").string();
    }

    bool VcdSerializer::parseCsvInternal(std::ifstream& ff, const std::string& filename, const std::vector<const Net*>& onlyNets)
    {
        std::map<std::string, const Net*> netNames;
        for (const Net* n : onlyNets)
        {
            netNames[n->get_name()] = n;
        }

        static const int bufsize = 65535;
        char buf[bufsize + 1];

        bool parseHeader  = true;
        int dataLineIndex = 0;
        while (!simutil::at_end(ff))
        {
            int sizeRead = simutil::read_line(ff, buf, bufsize);
            if (sizeRead >= bufsize)
            {
                if (mErrorCount[1]++ < maxErrorMessages)
                {
                    log_warning("waveform", "CSV line {} exceeds buffer size {}.", dataLineIndex, bufsize);
                }
                return false;
            }

            if (sizeRead < 0)
            {
                if (mErrorCount[2]++ < maxErrorMessages)
                {
                    log_warning("waveform", "CSV parse error reading line {} from file '{}'.", dataLineIndex, filename);
                }
                return false;
            }
            if (!sizeRead)
            {
                continue;
            }

            if (parseHeader)
            {
                if (!parseCsvHeader(buf))
                {
                    if (mErrorCount[3]++ < maxErrorMessages)
                    {
                        log_warning("waveform", "Cannot parse CSV header line '{}'.", buf);
                    }
                    return false;
                }
                parseHeader = false;
            }
            else
            {
                if (!parseCsvDataline(buf, dataLineIndex++))
                {
                    if (mErrorCount[4]++ < maxErrorMessages)
                    {
                        log_warning("waveform", "Cannot parse CSV data line '{}'.", buf);
                    }
                    return false;
                }
            }
        }

        return true;
    }

    bool VcdSerializer::importVcd(const std::string& vcdFilename, const std::string& workdir, const std::vector<const Net*>& onlyNets)
    {
        mWorkdir = workdir.empty() ? std::filesystem::current_path().string() : workdir;
        deleteFiles();
        mTime = 0;
        std::ifstream ff(vcdFilename, std::ios::binary);
        if (!ff.good())
        {
            log_warning("waveform", "Cannot open VCD input file '{}'.", vcdFilename);
            return false;
        }

        createSaleaeDirectory();
        mSaleaeWriter = new SaleaeWriter(mSaleaeDirectoryFilename);

        bool retval = parseVcdInternal(ff, vcdFilename, onlyNets);

        delete mSaleaeWriter;
        mSaleaeWriter = nullptr;
        mSaleaeFiles.clear();
        mAbbrevByName.clear();

        if (retval)
        {
            emitImportDone();
        }
        return retval;
    }

    bool VcdSerializer::parseVcdInternal(std::ifstream& ff, const std::string& filename, const std::vector<const Net*>& onlyNets)
    {
        bool parseHeader = true;

        std::map<std::string, const Net*> netNames;
        for (const Net* n : onlyNets)
        {
            netNames[n->get_name()] = n;
        }

        std::regex reHead("\\$(\\w*) (.*)\\$end");
        std::regex reWire("wire\\s+(\\d+) ([^ ]+) (.*)$");

        hal::error_code ec;
        uint64_t fileSize  = std::filesystem::file_size(std::filesystem::path(filename), ec);
        uint64_t totalRead = 0;

        static const int bufsize = 4095;
        char buf[bufsize + 1];

        int iline = 0;
        while (!simutil::at_end(ff))
        {
            int sizeRead = simutil::read_line(ff, buf, bufsize);
            ++iline;
            totalRead += sizeRead;
            if (fileSize)
            {
                emitProgress(totalRead, fileSize);
            }
            if (sizeRead >= bufsize)
            {
                if (mErrorCount[5]++ < maxErrorMessages)
                {
                    log_warning("waveform", "VCD line {} exceeds buffer size {}.", iline, bufsize);
                }
                return false;
            }

            if (sizeRead < 0)
            {
                if (mErrorCount[6]++ < maxErrorMessages)
                {
                    log_warning("waveform", "VCD parse error reading line {} from file '{}'.", iline, filename);
                }
                return false;
            }
            if (sizeRead > 0 && buf[sizeRead - 1] == '\n')
            {
                --sizeRead;
            }
            if (sizeRead > 0 && buf[sizeRead - 1] == '\r')
            {
                --sizeRead;
            }
            if (!sizeRead)
            {
                continue;
            }

            if (parseHeader)
            {
                std::string line(buf, sizeRead);
                std::smatch mHead;
                if (std::regex_search(line, mHead, reHead))
                {
                    if (mHead[1].str() == "enddefinitions")
                    {
                        parseHeader = false;
                    }
                    else if (mHead[1].str() == "var")
                    {
                        std::string varDefinition = mHead[2].str();
                        std::smatch mWire;
                        std::string wireName;
                        std::string wireAbbrev;
                        std::string wireBitsTxt;
                        if (std::regex_search(varDefinition, mWire, reWire))
                        {
                            wireBitsTxt = mWire[1].str();
                            wireAbbrev  = mWire[2].str();
                            wireName    = mWire[3].str();
                        }
                        bool ok;
                        if (!wireName.empty() && wireName.at(0) == '\\')
                        {
                            wireName.erase(0, 1);
                        }
                        wireName       = simutil::trim(wireName);
                        const Net* net = simutil::map_value(netNames, wireName, (const Net*)nullptr);

                        if (!netNames.empty() && !net)
                        {
                            continue;    // net not found in given name list
                        }

                        if (mAbbrevByName.find(wireName) != mAbbrevByName.end())
                        {
                            if (mErrorCount[7]++ < maxErrorMessages)
                            {
                                log_warning("waveform", "Waveform duplicate for '{}' in VCD file '{}'.", wireName, filename);
                            }
                            continue;
                        }
                        mAbbrevByName[wireName] = wireAbbrev;
                        int wireBits            = (int) simutil::to_uint(wireBitsTxt, &ok);
                        if (!ok)
                        {
                            wireBits = 1;
                        }
                        if (wireBits > 1)
                        {
                            continue;    // TODO : decision whether we will be able to handle VCD with more bits
                        }

                        u32 netId = net ? net->get_id() : 0;

                        SaleaeOutputFile* sof = nullptr;
                        auto itAbbrev         = mSaleaeFiles.find(wireAbbrev);
                        if (itAbbrev != mSaleaeFiles.end())
                        {
                            // output file already exists, need name entry
                            sof = itAbbrev->second;
                            if (sof)
                            {
                                mSaleaeWriter->add_directory_entry(sof->index(), wireName, netId);
                            }
                        }
                        else
                        {
                            sof = mSaleaeWriter->add_or_replace_waveform(wireName, netId);
                            if (sof)
                            {
                                mSaleaeFiles[wireAbbrev] = sof;
                            }
                        }
                    }
                }
            }
            else
            {
                if (!parseVcdDataline(buf, sizeRead))
                {
                    if (mErrorCount[8]++ < maxErrorMessages)
                    {
                        log_warning("waveform", "Cannot parse VCD data line '{}'.", std::string(buf, sizeRead));
                    }
                    return false;
                }
            }
        }
        return true;
    }

    bool VcdSerializer::importSaleae(const std::string& saleaeDirecotry, const std::unordered_map<hal::Net*, int>& lookupTable, const std::string& workdir, u64 timeScale)
    {
        mWorkdir = workdir.empty() ? std::filesystem::current_path().string() : workdir;
        deleteFiles();
        mTime                          = 0;
        SaleaeParser::sTimeScaleFactor = timeScale;
        int nstep                      = lookupTable.size() + 1;
        int istep                      = 0;

        emitProgress(istep++, nstep);
        createSaleaeDirectory();
        SaleaeDirectory sd(get_saleae_directory_filename());
        SaleaeDirectoryStoreRequest save(&sd);
        std::filesystem::path sourceDir(saleaeDirecotry);
        std::filesystem::path targetDir = std::filesystem::path(mSaleaeDirectoryFilename).parent_path();
        emitProgress(istep++, nstep);

        for (auto it = lookupTable.begin(); it != lookupTable.end(); ++it)
        {
            assert(it->first);
            int inx = sd.get_datafile_index(it->first->get_name(), it->first->get_id());
            if (inx < 0)
            {
                // create new file in import direcotry
                inx = sd.get_next_available_index();
            }
            std::filesystem::path source = sourceDir / ("digital_" + std::to_string(it->second) + ".bin");
            std::filesystem::path target = targetDir / ("digital_" + std::to_string(inx) + ".bin");
            hal::error_code ec;
            std::filesystem::remove(target, ec);
            if (!std::filesystem::copy_file(source, target, std::filesystem::copy_options::overwrite_existing, ec))
            {
                return false;
            }
            SaleaeInputFile sif(target.string());
            if (!sif.header())
            {
                return false;
            }
            SaleaeDirectoryNetEntry sdne(it->first->get_name(), it->first->get_id());
            sdne.addIndex(SaleaeDirectoryFileIndex(inx, sif.header()->beginTime(), sif.header()->endTime(), sif.header()->numTransitions() + 1));
            sd.add_or_replace_net(sdne);
            emitProgress(istep++, nstep);
        }
        emitImportDone();
        return true;
    }
}    // namespace hal
