#include "netlist_simulator_controller/wave_data.h"
#include "netlist_simulator_controller/saleae_file.h"
#include "netlist_simulator_controller/plugin_netlist_simulator_controller.h"
#include "netlist_simulator_controller/simulation_settings.h"
#include "netlist_simulator_controller/string_utils.h"
#include "netlist_simulator_controller/wave_data_provider.h"
#include "hal_core/netlist/net.h"
#include "hal_core/utilities/log.h"
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <map>
#include <math.h>
#include <stdio.h>
#include <string>
#include <vector>

namespace hal {

    WaveDataClock::WaveDataClock(const Net* n, const SimulationInput::Clock& clk, u64 tmax)
        : WaveData(n, WaveData::ClockNet), mClock(clk), mMaxTime(tmax)
    {
        dataFactory();
    }

    WaveDataClock::WaveDataClock(const Net* n, int start, u64 period, u64 tmax)
        : WaveData(n, WaveData::ClockNet), mMaxTime(tmax)
    {
        mClock.clock_net = n;
        mClock.switch_time = period / 2;
        mClock.start_at_zero = (start==0);
    }

    void WaveDataClock::setMaxTime(u64 tmax)
    {
        mMaxTime = tmax;
        resetWave();
        dataFactory();
    }

    void WaveDataClock::dataFactory()
    {
        int val = mClock.start_at_zero ? 0 : 1;
        for (u64 t=0; t<=mMaxTime; t+=mClock.switch_time)
        {
            mData[t] = val;
            val = val ? 0 : 1;
        }
    }

    WaveData::WaveData(const WaveData& other)
        : mId(other.mId), mFileIndex(other.mFileIndex), mFileSize(other.mFileSize), mTimeframeSize(other.mTimeframeSize),
          mName(other.mName), mNetType(other.mNetType), mBits(other.mBits), mSubscriber(0), mValueBase(other.mValueBase),
          mData(other.mData), mDirty(true), mWaveDataList(nullptr)
    {;}

    WaveData::WaveData(u32 id_, const std::string& nam, NetType tp, const std::map<u64,int> &dat)
        : mId(id_), mFileIndex(-1), mFileSize(0), mTimeframeSize(0), mName(nam), mNetType(tp), mBits(1), mSubscriber(0),
          mValueBase(16), mData(dat), mDirty(true), mWaveDataList(nullptr)
    {;}

    WaveData::WaveData(const Net* n, NetType tp)
        : mId(n->get_id()), mFileIndex(-1), mFileSize(0), mTimeframeSize(0),
          mName(n->get_name()),
          mNetType(tp), mBits(1), mSubscriber(0), mValueBase(16), mDirty(true), mWaveDataList(nullptr)
    {;}

    void WaveData::resetWave()
    {
        mData.clear();
    }

    void WaveData::setFileSize(u64 siz)
    {
        mFileSize = siz;
        mTimeframeSize = siz;
    }

    WaveData::LoadPolicy WaveData::loadPolicy() const
    {
        u64 maxSizeLoadable = NetlistSimulatorControllerPlugin::sSimulationSettings->maxSizeLoadable();
        if (mFileSize < maxSizeLoadable) return LoadAllData;
        if (mTimeframeSize && mTimeframeSize < maxSizeLoadable) return LoadTimeframe;
        return TooBigToLoad;
    }

    void WaveData::setId(u32 id_)
    {
        mId    = id_;
        mDirty = true;
    }

    bool WaveData::rename(const std::string& nam)
    {
        if (mName == nam) return false;
        mName  = nam;
        mDirty = true;
        return true;
    }

    void WaveData::setBits(int bts)
    {
        mBits  = bts;
        mDirty = true;
    }

    void WaveData::insertBooleanValueWithoutSync(u64 t, BooleanFunction::Value bval)
    {
        int val = (int) bval;
        if (!mData.empty())
        {
            auto it = mData.upper_bound(t);
            if (it != mData.cbegin())
            {
                --it;
                if (it->second == bval) return; // Nothing to do, previous value matches
            }
        }
        mData[t] = val;
        mDirty = true;
    }

    void WaveData::setData(const std::map<u64,int>& dat)
    {
        mData = dat;
        mDirty = true;
    }

    int WaveData::get_value_at(u64 t)
    {
        if (loadPolicy()==LoadAllData)
            loadDataUnlessAlreadyLoaded();
        return intValue(t);
    }

    SaleaeDirectoryNetEntry::Type WaveData::composedType() const
    {
        switch (mNetType)
        {
        case NetGroup: return SaleaeDirectoryNetEntry::Group;
        case BooleanNet: return SaleaeDirectoryNetEntry::Boolean;
        case TriggerTime: return SaleaeDirectoryNetEntry::Trigger;
        default: break;
        }
        return SaleaeDirectoryNetEntry::None;
    }

    bool WaveData::isEqual(const WaveData& other, int tolerance) const
    {
        if (mFileSize != other.mFileSize) return false;
        if (loadPolicy() == LoadAllData)
        {
            if (mData.size() != other.mData.size()) return false;
            auto jt = other.mData.cbegin();
            for (auto it = mData.begin(); it != mData.end(); ++it)
            {
                if (it->second != jt->second) return false;
                if (std::llabs((int64_t)it->first-(int64_t)jt->first) > tolerance) return false;
                ++jt;
            }
            return true;
        }
        u64 t0 = 0;
        for (;;)
        {
            std::vector<std::pair<u64,int>> evtsThis = get_events(t0);
            std::vector<std::pair<u64,int>> evtsOther = other.get_events(t0);
            if (evtsThis.empty()&&evtsOther.empty()) return true; // all tested
            if (evtsThis.size()!=evtsOther.size()) return false;
            auto jt = evtsOther.begin();
            for (auto it = evtsThis.begin(); it != evtsThis.end(); ++it)
            {
                if (it->second != jt->second) return false;
                if (std::llabs((int64_t)it->first-(int64_t)jt->first)> tolerance) return false;
                ++jt;
                t0 = it->first;
            }
            t0++;
        }
    }

    int WaveData::dataIndex() const
    {
        return mWaveDataList->waveIndexByNetId(mId);
    }

    std::vector<std::pair<u64,int>> WaveData::get_triggered_events(const WaveDataTrigger* wdTrig, u64 t0)
    {
        std::vector<std::pair<u64,int>> retval = wdTrig->get_events(t0);
        for (auto it=retval.begin(); it!=retval.end(); ++it)
        {
            u64 t = it->first;
            it->second = intValue(t);
        }
        return retval;
    }

    std::vector<std::pair<u64,int>> WaveData::get_events(u64 t0) const
    {
        std::vector<std::pair<u64,int>> retval;
        if (loadPolicy() == LoadAllData)
        {
            for (auto it = mData.lower_bound(t0); it != mData.cend(); ++it)
                retval.push_back(std::make_pair(it->first,it->second));
        }
        else
        {
            WaveDataProvider* wdp = nullptr;
            std::string saleaeDirectory = mWaveDataList->saleaeDirectory().get_filename();
            switch (mNetType)
            {
            case WaveData::NetGroup:
            {
                const WaveDataGroup* wdGrp = static_cast<const WaveDataGroup*>(this);
                wdp = new WaveDataProviderGroup(saleaeDirectory, wdGrp->children());
                break;
            }
            case WaveData::BooleanNet:
            {
                const WaveDataBoolean* wdBool = static_cast<const WaveDataBoolean*>(this);
                wdp = new WaveDataProviderBoolean(saleaeDirectory, wdBool->children(), wdBool->truthTable());
                break;
            }
            case WaveData::TriggerTime:
            {
                const WaveDataTrigger* wdTrig = static_cast<const WaveDataTrigger*>(this);
                wdp = new WaveDataProviderTrigger(saleaeDirectory, wdTrig->children(), wdTrig->toValueList(), wdTrig->get_filter_wave());
                break;
            }
            default:
                break;
            }

            if (wdp)
            {
                SaleaeDataTuple sdt = wdp->startValue(t0);
                while (sdt.mValue != SaleaeDataTuple::sReadError)
                {
                    retval.push_back(std::make_pair(sdt.mTime,sdt.mValue));
                    sdt = wdp->nextPoint();
                }
                delete wdp;
                return retval;
            }
            if (mWaveDataList && mFileIndex>=0)
            {
                std::filesystem::path path = mWaveDataList->saleaeDirectory().get_datafile_path(mFileIndex);
                if (!path.empty())
                {
                    SaleaeInputFile sif(path);
                    if (t0)
                    {
                        if (sif.get_file_position(t0,true) < 0)
                            return retval;
                    }
                    SaleaeDataBuffer* sdb = sif.get_buffered_data(NetlistSimulatorControllerPlugin::sSimulationSettings->maxSizeLoadable());
                    if (sdb)
                    {
                        for (u64 i=0; i<sdb->mCount; i++)
                            retval.push_back(std::make_pair(sdb->mTimeArray[i],sdb->mValueArray[i]));
                        delete sdb;
                    }
                }
            }
        }
        return retval;
    }

    void WaveData::loadDataUnlessAlreadyLoaded()
    {
        if ((u64)mData.size() >= mFileSize) return;
        loadSaleae();
    }

    bool WaveData::loadSaleae(const WaveDataTimeframe& tframe)
    {
        resetWave();
        if (!mWaveDataList || mFileIndex<0) return false;
        std::filesystem::path path = mWaveDataList->saleaeDirectory().get_datafile_path(mFileIndex);
        if (path.empty()) return false;
        SaleaeInputFile sif(path);
        if (!sif.good()) return false;
        u64 t0 = tframe.hasUserTimeframe() ? tframe.sceneMinTime() : 0;
        u64 t1 = tframe.hasUserTimeframe() ? tframe.sceneMaxTime() : 0;
        assert(t0 <= t1);

        int lastVal = BooleanFunction::X;
        bool valuePending = false;
        while (sif.good())
        {
            SaleaeDataBuffer* sdb = sif.get_buffered_data(NetlistSimulatorControllerPlugin::sSimulationSettings->maxSizeLoadable());
            if (!sdb) break;
            for (u64 i=0; i<sdb->mCount; i++)
            {
                u64 t = sdb->mTimeArray[i];
                if (t < t0)
                {
                    lastVal = sdb->mValueArray[i];
                    valuePending = true;
                }
                else if (t == t0)
                {
                    mData[t] = sdb->mValueArray[i];
                    valuePending = false;
                }
                else if (!t1 || t <= t1)
                {
                    if (valuePending) mData[t0] = lastVal;
                    mData[t] = sdb->mValueArray[i];
                    valuePending = false;
                }
                else
                {
                    if (valuePending) mData[t0] = lastVal;
                    valuePending = false;
                    break;
                }
            }
            delete sdb;
        }
        mDirty = true;
        return true;
    }

    void WaveData::saveSaleae()
    {
        if (!mWaveDataList) return;
        SaleaeDirectory& sd = mWaveDataList->saleaeDirectory();
        SaleaeDirectoryStoreRequest save(&sd);
        std::string nam = mName;
        mFileIndex = sd.get_datafile_index(nam,mId);
        if (mFileIndex < 0)
        {
            mFileIndex = sd.get_next_available_index();
            std::filesystem::path saleaeDir(sd.get_directory());
            hal::error_code ec;
            if (!std::filesystem::exists(saleaeDir, ec))
                std::filesystem::create_directories(saleaeDir, ec);
        }

        SaleaeDirectoryNetEntry sdne(nam,mId);
        sdne.addIndex(SaleaeDirectoryFileIndex(mFileIndex,0,maxTime(),mData.size()));
        sd.add_or_replace_net(sdne);

        mFileSize = mData.size();

        SaleaeDataBuffer sdb(mFileSize);
        int j = 0;
        for (auto it = mData.cbegin(); it != mData.cend(); ++it)
        {
            sdb.mTimeArray[j] = it->first;
            sdb.mValueArray[j] = it->second;
            ++j;
        }
        SaleaeOutputFile sof(sd.get_datafile_path(mFileIndex),mFileIndex);
        sof.put_data(&sdb);
    }

    std::map<u64,int>::const_iterator WaveData::timeIterator(double t) const
    {
        if (t<0) return mData.cend();
        std::map<u64,int>::const_iterator retval = mData.upper_bound((u64)floor(t));
        if (retval != mData.cbegin()) --retval;
        return retval;
    }

    u64 WaveData::neighborTransition(double t, bool next) const
    {
        u64 notFound = (u64) floor(t);
        LoadPolicy lpol = loadPolicy();
        if (lpol ==LoadAllData ||
                (lpol == LoadTimeframe && !mData.empty() && t>=mData.cbegin()->first && t < mData.crbegin()->first ))
        {
            if (next)
            {
                std::map<u64,int>::const_iterator it = mData.upper_bound((u64)floor(t));
                if (it == mData.end()) return notFound;
                return it->first;
            }
            else
            {
                std::map<u64,int>::const_iterator it = mData.lower_bound((u64)floor(t));
                if (it == mData.begin()) return notFound;
                --it;
                return it->first;
            }
        }

        std::vector<WaveData*> childList;
        switch (mNetType)
        {
        case NetGroup:
        {
            const WaveDataGroup* wdGrp = static_cast<const WaveDataGroup*>(this);
            childList = wdGrp->children();
            break;
        }
        case BooleanNet:
        {
            const WaveDataBoolean* wdBool = static_cast<const WaveDataBoolean*>(this);
            childList = wdBool->children();
            break;
        }
            // case TriggerNet: overwrite
        default:
            break;
        }

        if (!childList.empty())
        {
            bool first = true;
            u64 retval = notFound;
            for (const WaveData* wd : childList)
            {
                double tChild = wd->neighborTransition(t,next);
                if (tChild == notFound) continue;
                if (first)
                {
                    retval = tChild;
                    first = false;
                }
                else if (next)
                {
                    if (tChild < retval) retval = tChild;
                }
                else
                {
                    if (tChild > retval) retval = tChild;
                }
            }
            return retval;
        }


        if (!mWaveDataList) return notFound;
        SaleaeInputFile sif(mWaveDataList->saleaeDirectory().get_datafile_path(mFileIndex));
        if (!sif.good()) return notFound;

        int64_t pos = sif.get_file_position(t,next);

        while (pos >= 0 && pos <= (int64_t) sif.header()->numTransitions())
        {
            SaleaeDataTuple sdt = sif.get_next_value();
            if (sdt.readError()) return notFound;
            if (next)
            {
                if (sdt.mTime > t) return sdt.mTime;
                ++pos;
            }
            else
            {
                if (sdt.mTime < t) return sdt.mTime;
                --pos;
            }
            sif.set_file_position(pos);
        }

        return notFound;
    }

    int WaveData::intValue(double t) const
    {
        LoadPolicy lpol = loadPolicy();
        if (lpol == LoadAllData ||
                (lpol == LoadTimeframe && !mData.empty() && t>=mData.cbegin()->first && t < mData.crbegin()->first ))
        {
            if (mData.empty()) return -1;
            std::map<u64,int>::const_iterator it = timeIterator(t);
            if (it == mData.cend()) return -1;
            return it->second;
        }

        if (!mWaveDataList) return -1;
        SaleaeInputFile sif(mWaveDataList->saleaeDirectory().get_datafile_path(mFileIndex));
        if (!sif.good()) return -1;
        return sif.get_int_value(t);
    }

    std::string WaveData::strValue(double t) const
    {
        if (mData.empty()) return "x";
        std::map<u64,int>::const_iterator it = timeIterator(t);
        return strValue(it);
    }

    std::string WaveData::strValue(const std::map<u64,int>::const_iterator& it) const
    {
        if (it == mData.cend()) return std::string();
        return strValue(it->second);
    }

    std::string WaveData::strValue(int val) const
    {
        return stringValue(val,bits(),mValueBase);
    }

    std::string WaveData::stringValue(int val, int bits, int base)
    {
        switch (val) {
        case -2 : return "z";
        case -1 : return "x";
        }
        if (bits <= 1 || !val)
            return std::to_string(val);
        if (base<0)
        {
            int mask = 1 << (bits-1);
            if (val&mask)
                return "-" + std::to_string((1 << bits) - val);
            else
                return std::to_string(val);
        }
        int nDigits = 0;
        switch (base)
        {
        case 2:
            nDigits = bits;
            return "0b" + simutil::number_padded((u32)val,nDigits,2);
        case 16:
            nDigits = bits / 4;
            return "0x" + simutil::number_padded((u32)val,nDigits,16);
        default: break;
        }
        return simutil::number(val,base);
    }

    std::string WaveData::fileName() const
    {
        if (!mWaveDataList || mFileIndex < 0) return std::string();
        return mWaveDataList->saleaeDirectory().get_datafile_path(mFileIndex);
    }

    u64 WaveData::maxTime() const
    {
        if (mData.empty()) return 0;
        return mData.crbegin()->first;
    }

//--------------------------------------------
    WaveDataGroupIndex::WaveDataGroupIndex(const WaveData* wd)
    {
        if (!wd->id()) mCode = 0;
        else
        {
            const WaveDataGroup* grp = dynamic_cast<const WaveDataGroup*>(wd);
            if (grp) construct (grp->id(), false);
            else     construct (wd->id(),  true);
        }
    }

    void WaveDataGroupIndex::construct(u32 id, bool isNet)
    {
        if (!id) mCode = 0;
        else
            mCode = (id << 1) | (isNet?0:1);
    }

    //--------------------------------------------
    WaveDataBoolean::WaveDataBoolean(WaveDataList* wdList, const std::vector<WaveData*>& boolInput, const std::vector<int>& acceptMask)
        : WaveData(wdList->nextBooleanId(),"",WaveData::BooleanNet), mInputCount(boolInput.size()),
          mInputWaves(nullptr), mTruthTable(nullptr)
    {
        mWaveDataList = wdList;
        if (!mInputCount) return;
        if (mInputCount > 16)
        {
            mInputCount = 0;
            return;
        }

        mInputWaves = new WaveData*[mInputCount];
        for (int i = 0; i<mInputCount; i++)
            mInputWaves[i] = boolInput.at(i);
        rename("boolean_" + std::to_string(id()));


        int truthTableLen = (1 << mInputCount);
        int nByte = (truthTableLen+7) / 8;
        mTruthTable = new char[nByte];
        memset (mTruthTable, 0, nByte);

        for (int accept : acceptMask)
        {
            int j = accept/8;
            int k = accept%8;
            mTruthTable[j] |= (1<<k);
        }
        mWaveDataList->registerBoolean(this);
    }

    WaveDataBoolean::WaveDataBoolean(WaveDataList* wdList, const std::string& boolFunc)
        : WaveData(wdList->nextBooleanId(),boolFunc,WaveData::BooleanNet),
          mInputCount(0), mInputWaves(nullptr), mTruthTable(nullptr)
    {
        mWaveDataList = wdList;
        auto bf = BooleanFunction::from_string(boolFunc);
        if (bf.is_error()) return;
        std::vector<std::string> netNames;
        for (std::string netName : bf.get().get_variable_names())
            netNames.push_back(netName);
        if ((mInputCount = netNames.size()) <= 0) return;
        if (mInputCount > 16)
        {
            mInputCount = 0;
            return;
        }
        mInputWaves = new WaveData*[mInputCount];

        bool failed = false;
        for (int i = 0; i<mInputCount; i++)
        {
            const std::string& netName = netNames.at(i);
            WaveData* wd = mWaveDataList->waveDataByName(netName);
            if (!wd)
            {
                failed = true;
                break;
            }
            mInputWaves[i] = wd;
        }

        if (!failed)
        {
            auto tt = bf.get().compute_truth_table(netNames);
            if (tt.is_error())
                failed = true;
            else
            {
                int truthTableLen = (1 << mInputCount);
                int nByte = (truthTableLen+7) / 8;
                mTruthTable = new char[nByte];
                memset (mTruthTable, 0, nByte);
                for (int i=0; i<truthTableLen; i++)
                {
                    switch (tt.get().at(0).at(i))
                    {
                    case BooleanFunction::Z:
                    case BooleanFunction::X:
                        failed = true;
                        break;
                    case BooleanFunction::ZERO:
                        break;
                    case BooleanFunction::ONE:
                        int j = i/8;
                        int k = i%8;
                        mTruthTable[j] |= (1<<k);
                        break;
                    }
                    if (failed) break;
                }
            }
        }

        if (failed)
        {
            delete [] mInputWaves;
            mInputWaves = 0;
            mInputCount = 0;
            return;
        }
        else
            mWaveDataList->registerBoolean(this);
    }

    WaveDataBoolean::~WaveDataBoolean()
    {
        if (mInputWaves) delete [] mInputWaves;
        if (mTruthTable) delete [] mTruthTable;
    }

    void WaveDataBoolean::recalcData()
    {
        mData.clear();
        switch (loadPolicy())
        {
        case WaveData::TooBigToLoad:
            return;
        case WaveData::LoadTimeframe:
            for (int i=0; i<mInputCount; i++)
                if (mInputWaves[i]->data().empty())
                    return;
            break;
        default:
            break;
        }

        std::map<u64,int> transitionTime;
        // TODO : not loadable
        for (int i=0; i<mInputCount; i++)
        {
            mInputWaves[i]->loadDataUnlessAlreadyLoaded();
            for (auto it = mInputWaves[i]->data().cbegin(); it != mInputWaves[i]->data().cend(); ++it)
                ++transitionTime[it->first];
        }

        int lastval = SaleaeDataTuple::sReadError;
        for (auto tt = transitionTime.cbegin(); tt != transitionTime.cend(); ++tt)
        {
            u64 t = tt->first;
            int ttInx = 0;
            for (int i=0; i<mInputCount; i++)
            {
                int val = mInputWaves[i]->get_value_at(t);
                if (val < 0 || val > 1)
                {
                    ttInx = 1;
                    break;
                }
                if (val == 1)
                    ttInx |= (1<<i);
            }
            int nextval = SaleaeDataTuple::sReadError;
            if (ttInx < 0)
                nextval = -1;
            else
            {
                int j = ttInx / 8;
                int k = ttInx % 8;
                nextval = (mTruthTable[j] & (1<<k)) ? 1 : 0;
            }
            if (nextval != lastval)
            {
                mData[t] = nextval;
                lastval = nextval;
            }
        }
    }

    std::vector<WaveData*> WaveDataBoolean::children() const
    {
        std::vector<WaveData*> retval;
        for (int i=0; i<mInputCount; i++)
            retval.push_back(mInputWaves[i]);
        return retval;
    }

    WaveData::LoadPolicy WaveDataBoolean::loadPolicy() const
    {
        LoadPolicy retval = LoadAllData;
        for (int i=0; i<mInputCount; i++)
        {
            const WaveData* wd = mInputWaves[i];
            switch (wd->loadPolicy())
            {
            case TooBigToLoad:
                return TooBigToLoad;
            case LoadTimeframe:
                retval = LoadTimeframe;
                break;
            default:
                break;
            }
        }
        return retval;
    }

    int WaveDataBoolean::intValue(double t) const
    {
        u32 mask = 1;
        int val = 0;
        for (int i=0; i<mInputCount; i++)
        {
            int childVal = mInputWaves[i]->intValue(t);
            if (childVal < 0) return childVal;
            if (childVal) val |= mask;
            mask <<= 1;
        }
        return (mTruthTable[val/8] & (1<<val%8)) ? 1 : 0;
    }

//--------------------------------------------
    WaveDataTrigger::WaveDataTrigger(WaveDataList* wdList, const std::vector<WaveData *> &wdTrigger, const std::vector<int>& toVal)
        : WaveData(wdList->nextTriggerId(),"",TriggerTime),
          mTriggerCount(wdTrigger.size()), mTriggerWaves(nullptr), mFilterWave(nullptr), mToValue(nullptr)
    {
        mWaveDataList = wdList;
        rename("trigger" + std::to_string(id()));
        if (!mTriggerCount) return;
        mTriggerWaves = new WaveData*[mTriggerCount];
        mToValue = new int[mTriggerCount];
        int nVal = toVal.size();
        for (int i=0; i<mTriggerCount; i++)
        {
            mTriggerWaves[i] = wdTrigger.at(i);
            mToValue[i] = i < nVal ? toVal[i] : -1;
        }
        wdList->registerTrigger(this);
    }

    WaveDataTrigger::~WaveDataTrigger()
    {
        if (mTriggerWaves) delete [] mTriggerWaves;
        if (mToValue) delete [] mToValue;
    }

    void WaveDataTrigger::set_filter_wave(WaveData* wd)
    {
        if (wd && (wd->netType() == WaveData::NetGroup || wd->netType() == WaveData::TriggerTime)) return;
        mFilterWave = wd;
        recalcData();
        SaleaeDirectoryComposedEntry sdce = mWaveDataList->saleaeDirectory().get_composed(id(),SaleaeDirectoryNetEntry::Trigger);
        if (!sdce.isNull())
        {
            SaleaeDirectoryStoreRequest save(&mWaveDataList->saleaeDirectory());
            SaleaeDirectoryNetEntry trigEntry(wd->name(),wd->id(),wd->composedType());
            sdce.set_filter_entry(trigEntry.uniqueKey());
            mWaveDataList->saleaeDirectory().add_or_replace_composed(sdce);
        }
    }

    int WaveDataTrigger::intValue(double t) const
    {
        if (loadPolicy() == LoadAllData)
        {
            for (int i=0; i<mTriggerCount; i++)
            {
                mTriggerWaves[i]->loadDataUnlessAlreadyLoaded();
                auto it = mTriggerWaves[i]->data().find((u64)floor(t+0.5));
                if (it != mTriggerWaves[i]->data().cend())
                {
                    if (mToValue[i] < 0 || mToValue[i] == it->second) return 1;
                }
            }
            return 0;
        }
        // TODO : check disk file
        return 0;
    }

    void WaveDataTrigger::recalcData()
    {
        mData.clear();
        switch (loadPolicy())
        {
        case TooBigToLoad:
            return;
        case LoadTimeframe:
            for (int i=0; i<mTriggerCount; i++)
                if (mTriggerWaves[i]->data().empty())
                    return;
            break;
        default:
            for (int i=0; i<mTriggerCount; i++)
                mTriggerWaves[i]->loadDataUnlessAlreadyLoaded();
            break;
        }

        for (int i=0; i<mTriggerCount; i++)
            for (auto it = mTriggerWaves[i]->data().cbegin(); it != mTriggerWaves[i]->data().cend(); ++it)
                if (mToValue[i] < 0 || mToValue[i] == it->second)
                    if (!mFilterWave || mFilterWave->intValue(it->first)==1)
                        mData[it->first] = 1;
    }

    WaveData::LoadPolicy WaveDataTrigger::loadPolicy() const
    {
        LoadPolicy retval = WaveData::LoadAllData;
        for (int i=0; i<=mTriggerCount; i++)
        {
            const WaveData* wd = nullptr;
            if (i<mTriggerCount)
                wd = mTriggerWaves[i];
            else
                wd = mFilterWave;
            if (!wd) continue;
            switch (wd->loadPolicy())
            {
            case TooBigToLoad:
                return TooBigToLoad;
            case LoadTimeframe:
                retval = LoadTimeframe;
                break;
            default:
                break;
            }
        }
        return retval;
    }

    std::vector<WaveData*> WaveDataTrigger::children() const
    {
        std::vector<WaveData*> retval;
        for (int i=0; i<mTriggerCount; i++)
            retval.push_back(mTriggerWaves[i]);
        return retval;
    }

    u64 WaveDataTrigger::neighborTransition(double t, bool next) const
    {
        if (!mData.empty() && loadPolicy() != TooBigToLoad)
        {
            if (next)
            {
                auto it = mData.upper_bound((u64)t);
                if (it != mData.cend()) return it->first;
                return (u64) t;
            }
            auto it = mData.lower_bound((u64)t);
            if (it == mData.cend()) --it;
            while (it != mData.cbegin() && it->first>=(u64)t) --it;
            return it->first>=(u64)t ? (u64)t : it->first;
        }
        //TODO : from file
        return (u64) t;
    }

    std::vector<int> WaveDataTrigger::toValueList() const
    {
        std::vector<int> retval;
        for (int i=0; i<mTriggerCount; i++)
            retval.push_back(mToValue[i]);
        return retval;
    }

//--------------------------------------------
    WaveDataGroup::WaveDataGroup(WaveDataList *wdList, int grpId, const std::string& nam)
        : WaveData(grpId,nam,WaveData::NetGroup)
    {
        mWaveDataList = wdList;
        mWaveDataList->registerGroup(this);
    }

    WaveDataGroup::WaveDataGroup(WaveDataList* wdList, const std::string& nam)
        : WaveData(wdList->nextGroupId(),nam,WaveData::NetGroup)
    {
        mWaveDataList = wdList;
        if (nam.empty()) rename("group_" + std::to_string(id()));
        mWaveDataList->registerGroup(this);
    }

    WaveDataGroup::WaveDataGroup(WaveDataList* wdList, const WaveData* wdGrp)
        : WaveData(wdList->nextGroupId(),wdGrp->name(),WaveData::NetGroup)
    {
        mWaveDataList = wdList;
        int n = wdGrp->bits();
        if (!n) return;

        std::map<u64,int>** bitValue = new std::map<u64,int>*[n];
        for (int i=0; i<n; i++) bitValue[i] = new std::map<u64,int>();

        bool first = true;
        int lastV = 0;
        for (auto it = wdGrp->data().cbegin(); it != wdGrp->data().cend(); ++it)
        {
            u64 t = it->first;
            int v = it->second;
            for (int i=0; i<n ; i++)
            {
                int mask = 1 << i;
                if ( (v&mask) != (lastV&mask) || first )
                    (*bitValue[i])[t] = (v&mask) ? 1 : 0;
            }
            first = false;
            lastV = v;
        }
        for (int i=0; i<n; i++)
        {
            // Create Fake Wave Entries
            WaveData* wd = new WaveData(id()*10000+i, name() + "_bit" + std::to_string(i), WaveData::RegularNet, *bitValue[i]);
            wd->setBits(1);
            mGroupList.push_back(wd);
            mIndex[WaveDataGroupIndex(wd)] = i;
            mWaveDataList->add(wd,false);
            delete bitValue[i];
        }
        delete [] bitValue;
        mWaveDataList->registerGroup(this);
    }

    WaveDataGroup::~WaveDataGroup()
    {
        auto it = mWaveDataList->mDataGroups.find(id());
        if (it != mWaveDataList->mDataGroups.end())
            mWaveDataList->mDataGroups.erase(it);
    }

    void WaveDataGroup::add_waveform(WaveData* wd)
    {
        std::vector<WaveData*> wds;
        wds.push_back(wd);
        mWaveDataList->addWavesToGroup(id(),wds);
    }

    void WaveDataGroup::remove_waveform(WaveData* wd)
    {
        int irow = simutil::map_value(mIndex,WaveDataGroupIndex(wd),-1);
        if (irow < 0) return;
        removeAt(irow);
        restoreIndex();
        recalcData();
        int iwave = mWaveDataList->waveIndexByNetId(wd->id());
        if (iwave >= 0) mWaveDataList->emitWaveRemovedFromGroup(iwave,id());
    }

    std::vector<WaveData*> WaveDataGroup::get_waveforms() const
    {
        std::vector<WaveData*> retval;
        for (WaveData* wd : mGroupList) retval.push_back(wd);
        return retval;
    }

    void WaveDataGroup::replaceChild(WaveData* wd)
    {
        int inx = childIndex(wd);
        if (inx<0) return;
        mGroupList[inx] = wd;
        recalcData();
    }

    void WaveDataGroup::restoreIndex()
    {
        SaleaeDirectoryStoreRequest save(&mWaveDataList->saleaeDirectory());
        SaleaeDirectoryComposedEntry sdce(get_name(),id(),SaleaeDirectoryNetEntry::Group);
        mIndex.clear();
        int inx = 0;
        for (const WaveData* wd : mGroupList)
        {
            sdce.add_child(wd->id());
            mIndex[WaveDataGroupIndex(wd)] = inx++;
        }
        mWaveDataList->saleaeDirectory().add_or_replace_composed(sdce);
    }

    void WaveDataGroup::addNet(const Net* n)
    {
        if (bits() >= 30) return;
        u32 netId = n->get_id();
        WaveData* wd = nullptr;
        if (!mWaveDataList->hasNet(netId))
            mWaveDataList->add((wd = new WaveData(n)),false);
        else
            wd = mWaveDataList->waveDataByNet(n);
        int inx = mGroupList.size();
        mGroupList.push_back(wd);
        mIndex[WaveDataGroupIndex(wd)] = inx;
    }

    std::vector<WaveData*> WaveDataGroup::children() const
    {
        return mGroupList;
    }

    std::vector<int> WaveDataGroup::childrenWaveIndex() const
    {
        std::vector<int> retval;
        for (const WaveData* wd : mGroupList)
        {
            retval.push_back(mWaveDataList->waveIndexByNetId(wd->id()));
        }
        return retval;
    }

    WaveData* WaveDataGroup::childAt(int inx) const
    {
        if (inx >= (int) mGroupList.size()) return nullptr;
        return mGroupList.at(inx);
    }

    int WaveDataGroup::childIndex(WaveData* wd) const
    {
        return simutil::map_value(mIndex,WaveDataGroupIndex(wd),-1);
    }

    int WaveDataGroup::netIndex(u32 id) const
    {
        return simutil::map_value(mIndex,WaveDataGroupIndex(id,true),-1);
    }

    bool WaveDataGroup::hasNetId(u32 id) const
    {
        return mIndex.find(WaveDataGroupIndex(id,true)) != mIndex.end();
    }

    int WaveDataGroup::bits() const
    {
        int n = mGroupList.size();
        if (n < 1) n=1;
        return n;
    }

    WaveData* WaveDataGroup::removeAt(int inx)
    {
        if (inx >= (int) mGroupList.size()) return nullptr;
        WaveData* wd = mGroupList.at(inx);
        mGroupList.erase(mGroupList.begin()+inx);
        restoreIndex();
        recalcData();
        return wd;
    }

    void WaveDataGroup::insert(int inx, WaveData* wd)
    {
        if (inx >= (int) mGroupList.size())
            mGroupList.push_back(wd);
        else
            mGroupList.insert(mGroupList.begin()+inx,wd);
        restoreIndex();
        recalcData();
    }

    void WaveDataGroup::addWaves(const std::vector<WaveData*>& wds)
    {
        mGroupList.insert(mGroupList.end(), wds.begin(), wds.end());
        restoreIndex();
        recalcData();
    }

    void WaveDataGroup::updateWaveData(WaveData* wd)
    {
        int inx = childIndex(wd);
        if (inx < 0) return;
        mGroupList[inx] = wd;
        recalcData();
    }

    WaveData::LoadPolicy WaveDataGroup::loadPolicy() const
    {
        WaveData::LoadPolicy retval = WaveData::LoadAllData;
        for (const WaveData* wd : mGroupList)
        {
            switch (wd->loadPolicy())
            {
            case WaveData::TooBigToLoad:
                return WaveData::TooBigToLoad;
            case WaveData::LoadTimeframe:
                retval = WaveData::LoadTimeframe;
                break;
            default:
                break;
            }
        }
        return retval;
    }

    void WaveDataGroup::recalcData()
    {
        mData.clear();
        std::multimap<u64,int> tIndex;
        u32 value = 0;
        u32 undef = 0;
        int nChildren = mGroupList.size();
        if (mGroupList.empty())
        {
            mData[0] = -1;
            mDirty = true;
            return;
        }
        WaveData** wdArray = new WaveData*[nChildren];
        for (int ibit = 0; ibit < nChildren; ibit++)
        {
            undef |= (1 << ibit);
            WaveData* wd = mGroupList.at(ibit);
            if (wd->loadPolicy()==WaveData::TooBigToLoad)
            {
                // Would block, must determine group values in background thread
                delete [] wdArray;
                return;
            }
            else if (wd->data().size() < wd->fileSize())
            {
                // Loadable but nut loaded yet
                wd->loadSaleae(mWaveDataList->timeFrame());
            }
            wdArray[ibit] = wd;
            if (!wd) continue;
            for (auto it = wd->data().cbegin(); it != wd->data().cend(); ++it)
                tIndex.insert(std::make_pair(it->first,ibit));
        }
        u64 t0 = 0;
        for (auto it = tIndex.cbegin(); it != tIndex.cend(); ++it)
        {
            if (it->first != t0)
            {
                mData[t0] = undef ? -1 : (int) value;
                t0 = it->first;
            }
            int ibit = it->second;
            int v = simutil::map_value(wdArray[ibit]->data(),t0,0);
            int mask = (1 << (nChildren - ibit - 1));
            if (v<0)
                undef |= mask;
            else
            {
                undef &= ( ~mask );
                if (v)
                    value |= mask;
                else
                    value &= ( ~mask );
            }
        }
        mData[t0] = undef ? -1 : (int) value;
        delete [] wdArray;
        mDirty = true;
        mWaveDataList->emitGroupUpdated(id());
    }

    int WaveDataGroup::intValue(double t) const
    {
        u32 mask = 1 << (mGroupList.size()-1);
        int retval = 0;
        for (const WaveData* wd : mGroupList)
        {
            int childVal = wd->intValue(t);
            if (childVal < 0) return childVal;
            if (childVal) retval |= mask;
            mask >>= 1;
        }
        return retval;
    }

//--------------------------------------------
    WaveDataTimeframe::WaveDataTimeframe()
            : mSceneMaxTime(sMinSceneWidth), mSimulateMaxTime(0), mUserdefMaxTime(0), mUserdefMinTime(0) {;}

    u64 WaveDataTimeframe::simulateMaxTime() const
    {
        return mSimulateMaxTime;
    }

    u64 WaveDataTimeframe::sceneMaxTime() const
    {
        if (hasUserTimeframe()) return mUserdefMaxTime;
        return mSceneMaxTime;
    }

    void WaveDataTimeframe::setSceneMaxTime(u64 t)
    {
        if (t < sMinSceneWidth)
            mSceneMaxTime = sMinSceneWidth;
        else
            mSceneMaxTime = t;
    }

    u64 WaveDataTimeframe::sceneMinTime() const
    {
        if (hasUserTimeframe()) return mUserdefMinTime;
        return 0;
    }

    u64 WaveDataTimeframe::sceneWidth() const
    {
        u64 x0 = sceneMinTime();
        u64 x1 = sceneMaxTime();
        if (x1 <= x0) return 1;
        return (x1-x0);
    }

    bool WaveDataTimeframe::hasUserTimeframe() const
    {
        return mUserdefMaxTime > 0;
    }

    void WaveDataTimeframe::setUserTimeframe(u64 t0, u64 t1)
    {
        mUserdefMinTime = t0;
        mUserdefMaxTime = t1;
    }

//--------------------------------------------
    WaveDataList::WaveDataList(const std::string& sdFilename)
        : mSaleaeDirectory(sdFilename), mMaxGroupId(0), mMaxBooleanId(0), mMaxTriggerid(0)
    {;}

    WaveDataList::~WaveDataList()
    {
        clearAll();
    }

    int WaveDataList::waveIndexByNetId(u32 id) const
    {
        return simutil::map_value(mIds,id,-1);
    }

    void WaveDataList::setMaxTime(u64 tmax)
    {
        if (mTimeframe.mSceneMaxTime == tmax) return;

        // adjust clock settings
        bool mustUpdateClocks = (tmax > mTimeframe.mSceneMaxTime);

        mTimeframe.setSceneMaxTime(tmax);
        if (mustUpdateClocks) updateClocks();
    }

    void WaveDataList::emitTimeframeChanged()
    {
    }

    void WaveDataList::incrementSimulTime(u64 deltaT)
    {
        mTimeframe.mSimulateMaxTime += deltaT;
        if (mTimeframe.mSimulateMaxTime > mTimeframe.mSceneMaxTime)
            setMaxTime(mTimeframe.mSimulateMaxTime);
    }

    void WaveDataList::setUserTimeframe(u64 t0, u64 t1)
    {
        if (t0 == mTimeframe.mUserdefMinTime && t1 == mTimeframe.mUserdefMaxTime) return;
        mTimeframe.setUserTimeframe(t0,t1);
        for (auto it=begin(); it!=end(); ++it)
        {
            WaveData* wd = *it;
            // invalidate memory data
            if (wd->loadPolicy()==WaveData::LoadTimeframe)
            {

                wd->setTimeframeSize(0);
                wd->clear();
            }
        }
    }

    void WaveDataList::clearAll()
    {
        mIds.clear();
        setMaxTime(0);
        for (auto it=begin(); it!=end(); ++it)
            mTrashCan.push_back(*it);
        clear();
        emptyTrash();
    }

    void WaveDataList::dump() const
    {
        fprintf(stderr, "WaveDataList:_________%8u______________\n", (unsigned int) mTimeframe.mSimulateMaxTime);
        for (auto it = cbegin(); it!= cend(); ++it)
        {
            fprintf(stderr, "  %4d <%s>:", (*it)->id(), (*it)->name().c_str());
            for (auto jt = (*it)->data().begin(); jt != (*it)->data().end(); ++jt)
                fprintf(stderr, " <%u,%d>", (unsigned int) jt->first, jt->second);
            fprintf(stderr, "\n");
        }
        fflush(stderr);

    }

    void WaveDataList::updateWaveName(int iwave, const std::string& nam)
    {
        if (at(iwave)->rename(nam))
        {
            SaleaeDirectoryStoreRequest save(&mSaleaeDirectory);
            mSaleaeDirectory.rename_net(at(iwave)->id(),nam);
        }
    }

    void WaveDataList::updateGroupName(u32 grpId, const std::string &nam)
    {
        WaveDataGroup* grp = simutil::map_value(mDataGroups,grpId,(WaveDataGroup*)nullptr);
        if (grp && grp->rename(nam))
        {
            SaleaeDirectoryStoreRequest save(&mSaleaeDirectory);
        }
    }

    void WaveDataList::emitWaveRemovedFromGroup(int iwave, int grpId)
    {
        UNUSED(iwave);
        UNUSED(grpId);
    }

    void WaveDataList::emitWaveAdded(int inx)
    {
        UNUSED(inx);
    }

    void WaveDataList::emitWaveUpdated(int inx)
    {
        UNUSED(inx);
    }

    void WaveDataList::emitGroupUpdated(int grpId)
    {
        UNUSED(grpId);
    }

    void WaveDataList::emptyTrash()
    {
        auto it = mTrashCan.begin();
        while (it != mTrashCan.end())
        {
            if ((*it)->hasSubscriber())
                ++it;
            else
            {
                delete (*it);
                it = mTrashCan.erase(it);
            }
        }
    }

    void WaveDataList::updateWaveData(int inx)
    {
        WaveData* wd = at(inx);
        wd->saveSaleae();
        u32 netId = wd->id();
        for (auto it = mDataGroups.begin(); it != mDataGroups.end(); ++it)
            if (it->second->hasNetId(netId))
            {
                it->second->recalcData();
            }
    }

    void WaveDataList::updateClocks()
    {
        for (auto it=begin(); it!=end(); ++it)
            if ((*it)->netType() == WaveData::ClockNet)
            {
                WaveDataClock* wdc = static_cast<WaveDataClock*>(*it);
                wdc->setMaxTime(mTimeframe.mSceneMaxTime);
            }
    }

    void WaveDataList::updateMaxTime()
    {
        u64 tmax = 0;
        for (auto it = cbegin(); it!= cend(); ++it)
        {
            u64 tmaxWave = (*it)->maxTime();
            if (tmaxWave > tmax) tmax = tmaxWave;
        }
        if (tmax>mTimeframe.mSceneMaxTime)
            setMaxTime(tmax);
    }

    void WaveDataList::add(WaveData* wd, bool updateSaleae)
    {
        int n = size();

        mIds[wd->id()] = n;
        push_back(wd);
        wd->setWaveDataList(this);
        updateMaxTime();
        if (updateSaleae) wd->saveSaleae();
        testDoubleCount();
    }

    void WaveDataList::triggerAddToView(u32 id) const
    {
        UNUSED(id);
    }

    void WaveDataList::registerTrigger(WaveDataTrigger *wdTrig)
    {
        u32 trigId = wdTrig->id();
        assert(mDataTrigger.find(trigId) == mDataTrigger.end());
        mDataTrigger[trigId] = wdTrig;
        SaleaeDirectoryComposedEntry sdce(wdTrig->name(),trigId,SaleaeDirectoryNetEntry::Trigger);
        for (WaveData* wd : wdTrig->children())
        {
            sdce.add_child(wd->id());
        }
        std::vector<int> toValue;
        for (int tval : wdTrig->toValueList())
            toValue.push_back(tval);
        sdce.set_data(toValue);

        WaveData* wdFilt = wdTrig->get_filter_wave();
        if (wdFilt)
        {
            SaleaeDirectoryNetEntry filterEntry(wdFilt->name(),wdFilt->id(),wdFilt->composedType());
            sdce.set_filter_entry(filterEntry.uniqueKey());
        }
        mSaleaeDirectory.add_or_replace_composed(sdce);
    }

    void WaveDataList::registerBoolean(WaveDataBoolean *wdBool)
    {
       u32 boolId = wdBool->id();
       assert(mDataBooleans.find(boolId) == mDataBooleans.end());
       mDataBooleans[boolId] = wdBool;
       SaleaeDirectoryComposedEntry sdce(wdBool->name(),boolId,SaleaeDirectoryNetEntry::Boolean);
       int n = 1;
       for (WaveData* wd : wdBool->children())
       {
           n <<= 1;
           sdce.add_child(wd->id());
       }
       std::vector<int> acceptVal;
       const char* ttable = wdBool->truthTable();
       for (int i=0; i<n; i++)
       {
           int j = i/8;
           int k = i%8;
           if (ttable[j] & (1<<k)) acceptVal.push_back(i);
       }
       sdce.set_data(acceptVal);
       mSaleaeDirectory.add_or_replace_composed(sdce);
    }

    void WaveDataList::registerGroup(WaveDataGroup *grp)
    {
        u32 grpId = grp->id();
        if (!grpId) return;
        assert(mDataGroups.find(grpId) == mDataGroups.end());
        mDataGroups[grpId] = grp;
        if (grpId)
        {
            mSaleaeDirectory.add_or_replace_composed(SaleaeDirectoryComposedEntry(grp->name(),grpId,SaleaeDirectoryNetEntry::Group));
            updateMaxTime();
        }
    }

    void WaveDataList::addWavesToGroup(u32 grpId, const std::vector<WaveData*>& wds)
    {
        SaleaeDirectoryStoreRequest save(&mSaleaeDirectory);
        WaveDataGroup* grp = simutil::map_value(mDataGroups,grpId,(WaveDataGroup*)nullptr);
        if (!grp) return;
        int inx = grp->size();
        for (WaveData* wd : wds)
        {
            grp->insert(inx++,wd);
        }
        grp->restoreIndex();
        grp->recalcData();
    }

    void WaveDataList::insertBooleanValue(WaveData *wd, u64 t, BooleanFunction::Value bval)
    {
        wd->insertBooleanValueWithoutSync(t, bval);
        wd->saveSaleae();
    }

    void WaveDataList::removeGroup(u32 grpId)
    {
        WaveDataGroup* grp = simutil::map_value(mDataGroups,grpId,(WaveDataGroup*)nullptr);
        if (!grp) return;
        mSaleaeDirectory.remove_composed(grpId,SaleaeDirectoryNetEntry::Group);
        mTrashCan.push_back(grp);
    }

    void WaveDataList::replaceWaveData(int inx, WaveData* wdNew)
    {
        // replace existing
        WaveData* wdOld = at(inx);
        assert(wdOld);
        assert(wdOld != wdNew);
        wdNew->rename(wdOld->name());
        operator[](inx) = wdNew;
        wdNew->setWaveDataList(this);
        for (auto it = mDataGroups.begin(); it != mDataGroups.end(); ++it)
            if (it->second->hasNetId(wdNew->id()))
                it->second->replaceChild(wdNew);
        if (wdNew->maxTime() > mTimeframe.mSceneMaxTime)
            updateMaxTime();

        wdNew->saveSaleae();

        emitWaveUpdated(inx);
        mTrashCan.push_back(wdOld);
    }

    void WaveDataList::updateFromSaleae()
    {
        mSaleaeDirectory.parse_json();
        u64 sdMaxTime = mSaleaeDirectory.get_max_time();
        if (sdMaxTime > mTimeframe.mSimulateMaxTime) incrementSimulTime(sdMaxTime-mTimeframe.mSimulateMaxTime);

        // create empty WaveData instances for all SALEAE waves ...
        std::map<std::string, WaveData*> saleaeWaves;
        for (const SaleaeDirectory::ListEntry& sdle : mSaleaeDirectory.get_net_list())
        {
            WaveData* wd = new WaveData(sdle.id, sdle.name);
            wd->setFileIndex(sdle.fileIndex);
            wd->setFileSize(sdle.size);
            auto itExist = saleaeWaves.find(sdle.name);
            if (itExist != saleaeWaves.end())
            {
                mTrashCan.push_back(itExist->second);
                itExist->second = wd;
            }
            else
                saleaeWaves.insert(std::make_pair(sdle.name,wd));
        }

        // ... but delete waves if already existing in container
        for (int i=0; i<(int)size(); i++)
        {
            WaveData* wd = at(i);
            auto it = saleaeWaves.find(wd->name());
            if (it == saleaeWaves.end()) continue;
            wd->setFileSize(it->second->fileSize());
            wd->setFileIndex(it->second->fileIndex());
            if (wd->loadPolicy() == WaveData::LoadAllData)
                wd->loadSaleae(mTimeframe);
            emitWaveUpdated(i);
            mTrashCan.push_back(it->second);
            saleaeWaves.erase(it);
        }

        for (auto it=saleaeWaves.begin(); it!=saleaeWaves.end(); ++it)
        {
            add(it->second,false);
        }
        setMaxTime(mSaleaeDirectory.get_max_time());

        for (auto it = mDataGroups.begin(); it != mDataGroups.end(); ++it)
        {
            it->second->recalcData();
        }
    }

    void WaveDataList::addOrReplace(WaveData* wd)
    {
        assert(wd);
        int inx = simutil::map_value(mIds,wd->id(),-1);
        if (inx >= 0)
            replaceWaveData(inx, wd);
        else
            add(wd,true);
    }

    WaveData* WaveDataList::waveDataByNet(const Net *n)
    {
        if (!n) return nullptr;
        int inx = simutil::map_value(mIds,n->get_id(),-1);
        if (inx >= 0)
        {
            WaveData* wd = at(inx);
            if (wd->loadPolicy()==WaveData::LoadAllData)
                wd->loadDataUnlessAlreadyLoaded();
            return wd;
        }
        WaveData* wd = new WaveData(n);
        wd->setWaveDataList(this);
        if (wd->loadSaleae(mTimeframe))
        {
            add(wd,false);
            return wd;
        }
        delete wd;
        return nullptr;
    }

    WaveData* WaveDataList::waveDataByName(const std::string& nam) const
    {
        for (WaveData* wd : *this)
            if (wd->name()==nam)
                return wd;
        for (auto it = mDataBooleans.begin(); it != mDataBooleans.end(); ++it)
            if (it->second->name()==nam)
                return it->second;
        return nullptr;
    }

    WaveData* WaveDataList::waveDataById(const int id)
    {
        for (WaveData* wd : *this)
            if (wd->id() == (u32) id)
                return wd;
        for (auto it = mDataBooleans.begin(); it != mDataBooleans.end(); ++it)
            if (it->second->id() == (u32) id)
                return it->second;
        return nullptr;
    }

    void WaveDataList::testDoubleCount()
    {
        std::map<u32,int> doubleCount;
        std::set<std::string> notInNetlist;
        for (const WaveData* wd : *this)
        {
            if (!wd->id())
            {
                if (mNotInNetlist.find(wd->name()) == mNotInNetlist.end())
                    notInNetlist.insert(wd->name());
            }
            else
                ++doubleCount[wd->id()];
        }
        for (auto it=doubleCount.cbegin(); it!=doubleCount.cend(); ++it)
        {
            if (it->second > 1)
            {
                log_warning("simulation_plugin", "Duplicate waveform ({}x) found : '{}'", it->second, at(simutil::map_value(mIds,it->first,0))->name());
            }
        }
        if (!notInNetlist.empty())
        {
            for (const std::string& name : notInNetlist)
            {
                log_warning("simulation_plugin", "Waveform not in (partial) netlist : '{}'", name);
            }
            mNotInNetlist.insert(notInNetlist.begin(), notInNetlist.end());
        }
    }

    void WaveDataList::restoreIndex()
    {
        mIds.clear();
        int inx = 0;

        for (const WaveData* wd : *this)
            mIds[wd->id()] = inx++;
        testDoubleCount();
    }

    std::set<u32> WaveDataList::toSet() const
    {
        std::set<u32> retval;
        for (auto it = mIds.cbegin(); it != mIds.cend(); ++it)
            retval.insert(it->first);
        return retval;
    }

    void WaveDataList::remove(u32 id)
    {
        auto it = mIds.find(id);
        if (it == mIds.end()) return;
        int inx = it->second;
        WaveData* toDelete = at(inx);
        erase(begin()+inx);
        restoreIndex();
        mTrashCan.push_back(toDelete);
    }

    void WaveDataList::setValueForEmpty(int val)
    {
        int inx = 0;

        for (auto it = cbegin(); it!= cend(); ++it)
        {
            if ((*it)->data().empty())
            {
                (*it)->insert(0,val);
                emitWaveUpdated(inx);
            }
            ++inx;
        }
    }

}
