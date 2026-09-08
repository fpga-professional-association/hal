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

#include <map>
#include <set>
#include <string>
#include <vector>
#include "hal_core/defines.h"
#include "hal_core/netlist/boolean_function.h"
#include "netlist_simulator_controller/simulation_input.h"
#include "netlist_simulator_controller/saleae_directory.h"

namespace hal {

    class WaveData;
    class NetlistSimulator;
    class SaleaeInputFile;
    class Net;

    /**
     * The time interval that is currently loaded and displayed, i.e., the simulated range and the visible cursor range.
     */
    class WaveDataTimeframe
    {
        friend class WaveDataList;
        u64 mSceneMaxTime;
        u64 mSimulateMaxTime;
        u64 mUserdefMaxTime;
        u64 mUserdefMinTime;
        static const int sMinSceneWidth = 1000;
    public:
        WaveDataTimeframe();
        u64 sceneMaxTime() const;
        u64 simulateMaxTime() const;
        u64 sceneMinTime() const;
        u64 sceneWidth() const;
        bool hasUserTimeframe() const;
        void setUserTimeframe(u64 t0=0, u64 t1=0);
        void setSceneMaxTime(u64 t);
        void setSimulateMaxTime(u64 t) { mSimulateMaxTime = t; }
    };

    class WaveDataList;
    class WaveDataTrigger;

    /**
     * The waveform of a single net, i.e., the sequence of value changes over time together with its display properties.
     */
    class WaveData
    {
    public:
        enum NetType { RegularNet, InputNet, OutputNet, ClockNet, BooleanNet, TriggerTime, NetGroup };
        enum LoadPolicy { TooBigToLoad, LoadTimeframe, LoadAllData };
    private:
        u32 mId;
        int mFileIndex;
        u64 mFileSize;
        u64 mTimeframeSize;
        std::string mName;
        NetType mNetType;
        int mBits;
        int mSubscriber;
    protected:
        int mValueBase;
        std::map<u64,int> mData;
        bool mDirty;
        WaveDataList* mWaveDataList;

        std::map<u64,int>::const_iterator timeIterator(double t) const;
        void resetWave();
    public:
        WaveData(const WaveData& other);
        WaveData(u32 id_, const std::string& nam, NetType tp = RegularNet,
                 const std::map<u64,int>& dat = std::map<u64,int>() );
        WaveData(const Net* n, NetType tp = RegularNet);
        virtual ~WaveData() {;}
        u32     id()                        const { return mId; }
        std::string name()                  const { return mName; }
        NetType netType()                   const { return mNetType; }
        virtual int bits()                  const { return mBits; }
        bool    isDirty()                   const { return mDirty; }
        const std::map<u64,int>& data()     const { return mData; }
        int     fileIndex()                 const { return mFileIndex; }
        u64     fileSize()                  const { return mFileSize; }
        int     valueBase()                 const { return mValueBase; }
        std::string fileName()              const;
        SaleaeDirectoryNetEntry::Type composedType() const;
        void setId(u32 id_);
        bool rename(const std::string& nam);
        void setBits(int bts);
        void setDirty(bool dty)                     { mDirty = dty; }
        void setFileIndex(int saleaIndex)           { mFileIndex = saleaIndex; }
        void setFileSize(u64 siz);
        void setTimeframeSize(u64 siz)              { mTimeframeSize = siz; }
        void setWaveDataList(WaveDataList* wdList)  { mWaveDataList = wdList; }
        virtual LoadPolicy loadPolicy() const;
        int dataIndex() const;

        virtual u64 neighborTransition(double t, bool next) const;
        void loadDataUnlessAlreadyLoaded();
        bool loadSaleae(const WaveDataTimeframe& tframe = WaveDataTimeframe());
        void saveSaleae();
        void setData(const std::map<u64,int>& dat);
        virtual int  intValue(double t) const;
        int get_value_at(u64 t);
        std::string get_name() const { return mName; }
        std::vector<std::pair<u64,int>> get_events(u64 t0 = 0) const;
        std::vector<std::pair<u64,int>> get_triggered_events(const WaveDataTrigger* wdTrig, u64 t0 = 0);
        u64  maxTime() const;
        void clear() { mData.clear(); }
        void insert(u64 t, int val) { mData[t] = val; }
        void insertBooleanValueWithoutSync(u64 t, BooleanFunction::Value bval);
        std::string strValue(int val) const;
        std::string strValue(double t) const;
        std::string strValue(const std::map<u64,int>::const_iterator& it) const;
        void setValueBase(int bas) { mValueBase = bas; }
        bool isEqual(const WaveData& other, int tolerance=0) const;
        static std::string stringValue(int val, int bits, int base);
        bool hasSubscriber() const { return mSubscriber > 0; }
        void addSubscriber() { ++mSubscriber; }
        void removeSubscriber() { if (mSubscriber) -- mSubscriber; }
    };

    /**
     * The waveform of a clock net, which is generated from a period rather than read from simulation results.
     */
    class WaveDataClock : public WaveData
    {
        SimulationInput::Clock mClock;
        u64 mMaxTime;
        void dataFactory();
    public:
        WaveDataClock(const Net* n, const SimulationInput::Clock& clk, u64 tmax);
        WaveDataClock(const Net* n, int start, u64 period, u64 tmax);
        void setMaxTime(u64 tmax);
        SimulationInput::Clock clock() const { return mClock; }
    };

    class WaveDataGroup;
    class WaveDataBoolean;

    /**
     * The list of all waveforms of a simulation, which also owns them.
     */
    class WaveDataList : public std::vector<WaveData*>
    {
        friend class WaveDataGroup;
        friend class WaveDataBoolean;
        friend class WaveDataTrigger;

        std::map<u32,int>     mIds;
        WaveDataTimeframe mTimeframe;
        SaleaeDirectory   mSaleaeDirectory;
        u32               mMaxGroupId;
        u32               mMaxBooleanId;
        u32               mMaxTriggerid;
        std::vector<WaveData*>  mTrashCan;
        std::set<std::string>   mNotInNetlist;
        void testDoubleCount();
        void restoreIndex();
        void updateMaxTime();
        void setMaxTime(u64 tmax);

        using std::vector<WaveData*>::push_back;
        using std::vector<WaveData*>::insert;

        void replaceWaveData(int inx, WaveData *wdNew);
        void registerGroup(WaveDataGroup* grp);
        void registerBoolean(WaveDataBoolean* wdBool);
        void registerTrigger(WaveDataTrigger* wdTrig);
    public:
        /**
         * Map of groups indexed by non-zero group id
         */

        std::map<u32,WaveDataGroup*> mDataGroups;
        std::map<u32,WaveDataBoolean*> mDataBooleans;
        std::map<u32,WaveDataTrigger*> mDataTrigger;
        WaveDataList(const std::string& sdFilename);
        ~WaveDataList();

        u32  nextGroupId() { return ++mMaxGroupId; }
        u32  maxGroupId() const { return mMaxGroupId; }
        u32  nextBooleanId() { return ++ mMaxBooleanId; }
        u32  nextTriggerId() { return ++ mMaxTriggerid; }
        void addWavesToGroup(u32 grpId, const std::vector<WaveData*>& wds);
        void removeGroup(u32 grpId);

        void addOrReplace(WaveData* wd);
        void add(WaveData* wd, bool updateSaleae);
        void remove(u32 id);
        void incrementSimulTime(u64 deltaT);
        void clearAll();
        void updateClocks();
        void updateWaveData(int inx);

        WaveData* waveDataByNet(const Net* n);
        WaveData* waveDataByName(const std::string& nam) const;
        WaveData* waveDataById(const int id);
        int waveIndexByNetId(u32 id) const;
        void triggerAddToView(u32 id) const;
        bool hasNet(u32 id) const { return mIds.find(id) != mIds.end(); }
        std::set<u32> toSet() const;
        void updateWaveName(int iwave, const std::string& nam);
        void updateGroupName(u32 grpId, const std::string& nam);
        const WaveDataTimeframe& timeFrame() const { return mTimeframe; }
        void setValueForEmpty(int val);
        void dump() const;
        void emitWaveAdded(int inx);
        void emitWaveUpdated(int inx);
        void emitGroupUpdated(int grpId);
        void emitWaveRemovedFromGroup(int iwave, int grpId);
        void emitTimeframeChanged();
        void updateFromSaleae();
        SaleaeDirectory& saleaeDirectory() { return mSaleaeDirectory; }
        void insertBooleanValue(WaveData* wd, u64 t, BooleanFunction::Value bval);
        void setUserTimeframe(u64 t0=0, u64 t1=0);
        void emptyTrash();
    };

    /**
     * The key under which a waveform is stored in a group, derived from the ID and the kind of the waveform.
     */
    class WaveDataGroupIndex {
        u32 mCode;
        void construct(u32 id, bool isNet);
    public:
        WaveDataGroupIndex(const WaveData* wd);
        WaveDataGroupIndex(u32 id, bool isNet) { construct(id, isNet); }
        bool operator==(const WaveDataGroupIndex& other) const { return mCode == other.mCode; }
        bool operator<(const WaveDataGroupIndex& other) const { return mCode < other.mCode; }
        u32 code() const { return mCode; }
    };

    /**
     * A waveform that is computed from other waveforms by evaluating a Boolean function on them.
     */
    class WaveDataBoolean : public WaveData
    {
        int mInputCount;
        WaveData** mInputWaves;
        std::map<WaveDataGroupIndex,int> mIndex;
        char* mTruthTable;
    public:
        WaveDataBoolean(WaveDataList* wdList, const std::string& boolFunc);
        WaveDataBoolean(WaveDataList* wdList, const std::vector<WaveData*>& boolInput, const std::vector<int>& acceptMask);
        ~WaveDataBoolean();
        void recalcData();
        virtual LoadPolicy loadPolicy() const override;
        std::vector<WaveData*> children() const;
        const char* truthTable() const { return mTruthTable; }
        virtual int intValue(double t) const override;
    };

    /**
     * A waveform that marks the points in time at which a set of other waveforms shows a given transition.
     */
    class WaveDataTrigger : public WaveData
    {
        int mTriggerCount;
        WaveData** mTriggerWaves;
        WaveData* mFilterWave;
        std::map<WaveDataGroupIndex,int> mIndex;
        int* mToValue;
    public:
        WaveDataTrigger(WaveDataList* wdList, const std::vector<WaveData*>& wdTrigger, const std::vector<int>& toVal = std::vector<int>());
        ~WaveDataTrigger();
        void recalcData();
        virtual LoadPolicy loadPolicy() const override;
        std::vector<WaveData*> children() const;
        virtual u64 neighborTransition(double t, bool next) const override;
        virtual int intValue(double t) const override;
        void set_filter_wave(WaveData* wd);
        std::vector<int> toValueList() const;
        WaveData* get_filter_wave() const { return mFilterWave; }
    };

    /**
     * A group of waveforms that are displayed together and whose values form a single multi-bit value.
     */
    class WaveDataGroup : public WaveData
    {

    protected:
        std::vector<WaveData*> mGroupList;

        std::map<WaveDataGroupIndex,int> mIndex;
    public:
        WaveDataGroup(WaveDataList* wdList, int grpId, const std::string& nam);
        WaveDataGroup(WaveDataList* wdList, const std::string& nam = std::string());
        WaveDataGroup(WaveDataList* wdList, const WaveData* wdGrp);
        virtual ~WaveDataGroup();
        virtual int bits() const override;
        virtual int size() const { return (int) mGroupList.size(); }
        void addNet(const Net* n);
        virtual void insert(int inx, WaveData* wd);
        virtual void addWaves(const std::vector<WaveData*>& wds);
        void restoreIndex();
        virtual void recalcData();
        virtual bool hasNetId(u32 id) const;
        virtual std::vector<WaveData*> children() const;
        std::vector<int> childrenWaveIndex() const;
        virtual WaveData* childAt(int inx) const;
        virtual WaveData* removeAt(int inx);
        virtual bool isEmpty() const { return mGroupList.empty(); }
        virtual void updateWaveData(WaveData* wd);
        virtual int childIndex(WaveData* wd) const;
        virtual int netIndex(u32 id) const;
        virtual void replaceChild(WaveData* wd);
        virtual LoadPolicy loadPolicy() const override;
        void add_waveform(WaveData* wd);
        void remove_waveform(WaveData* wd);
        std::vector<WaveData*> get_waveforms() const;
        virtual int intValue(double t) const override;
    };
}
