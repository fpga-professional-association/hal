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

#include <fstream>
#include <map>
#include <string>
#include <unordered_map>
#include <vector>

#include "hal_core/defines.h"

namespace hal {

    class Net;
    class WaveData;
    class WaveSaleaeData;
    class SaleaeOutputFile;
    class SaleaeWriter;
    class NetlistSimulatorController;

    /**
     * One net of a VCD file, i.e., its waveform together with the abbreviation used for it in the file.
     */
    class VcdSerializerElement
    {
        int mIndex;
        const WaveData* mData;
        u64 mTime;
        int mValue;
    public:
        VcdSerializerElement(int inx, const WaveData* wd);
        u64 time() const {return mTime; }
        int value() const { return mValue; }
        void setEvent(u64 t, int val) { mTime = t; mValue = val; }
        bool hasData() const;
        void reset();
        std::string name() const;
        std::string charCode() const;
    };

    /**
     * Reads and writes waveform data in the value change dump (VCD) format.
     */
    class VcdSerializer
    {
        int mTime;
        u64 mFirstTimestamp;
        u64 mLastTimestamp;
        u64 mTimeShift;
        std::map<std::string,SaleaeOutputFile*> mSaleaeFiles;
        SaleaeWriter* mSaleaeWriter;
        std::vector<VcdSerializerElement*> mWriteElements;
        std::string mWorkdir;
        std::string mSaleaeDirectoryFilename;
        std::map<std::string,std::string> mAbbrevByName;
        std::vector<int> mLastValue;
        int mErrorCount[9];
        bool mSaleae;
        int mLastProgress;
        NetlistSimulatorController* mController;

        bool parseVcdDataline(char* buf, int len);
        bool parseVcdDataNonDecimal(const std::string& line, int base);
        void storeValue(int val, const std::string& abrev);
        bool parseCsvHeader(char* buf);
        bool parseCsvDataline(char* buf, int dataLineIndex);
        bool parseVcdInternal(std::ifstream& ff, const std::string& filename, const std::vector<const Net *>& onlyNets);
        bool parseCsvInternal(std::ifstream& ff, const std::string& filename, const std::vector<const Net *>& onlyNets);

        void writeVcdEvent(std::ofstream& of);

        void deleteFiles();
        void createSaleaeDirectory();
        void emitProgress(double step, double max);
        void emitImportDone();

    public:
        VcdSerializer(const std::string& workdir=std::string(), bool saleae_cli=false, NetlistSimulatorController* controller = nullptr);
        std::string get_saleae_directory_filename() const { return mSaleaeDirectoryFilename; }
        bool exportVcd(const std::string& filename, const std::vector<const WaveData*>& waves, u32 startTime, u32 endTime, u32 timeShift=0);
        bool exportCsv(const std::string& filename, const std::vector<const WaveData*>& waves);
        bool importVcd(const std::string& vcdFilename, const std::string& workdir=std::string(), const std::vector<const Net*>& onlyNets = std::vector<const Net*>());
        bool importCsv(const std::string& csvFilename, const std::string& workdir=std::string(), const std::vector<const Net*>& onlyNets = std::vector<const Net*>(), u64 timeScale = 1000000000);
        bool importSaleae(const std::string& saleaeDirecotry, const std::unordered_map<Net*,int>& lookupTable, const std::string& workdir=std::string(),  u64 timeScale = 1000000000);
        u64 maxTime() const { return mTime; }
    };

}
