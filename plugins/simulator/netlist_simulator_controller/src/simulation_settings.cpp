#include "netlist_simulator_controller/simulation_settings.h"

#include "hal_core/defines.h"
#include "netlist_simulator_controller/string_utils.h"

#include <filesystem>
#include <fstream>

namespace hal {

    const char* SimulationSettings::sColorSettingTag[MaxColorSetting] = {"wv_regular", "wv_selected", "wv_undefined", "value_x", "value_0", "value_1" };
    const char* SimulationSettings::sDefaultColor[MaxColorSetting] = {"#10E0FF", "#F0F8FF", "#C08010", "#707071", "#102080", "#802010" };

    SimulationSettings::SimulationSettings(const std::string &filename)
        : mFilename(filename), mDirty(false)
    {
        parseIniFile();
    }

    void SimulationSettings::parseIniFile()
    {
        std::ifstream ff(mFilename, std::ios::binary);
        if (!ff.good()) return;
        std::string section;
        std::string line;
        while (std::getline(ff, line))
        {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            std::string trimmed = simutil::trim(line);
            if (trimmed.empty() || trimmed[0] == ';' || trimmed[0] == '#') continue;
            if (trimmed[0] == '[')
            {
                size_t pos = trimmed.find(']');
                if (pos == std::string::npos) continue;
                section = trimmed.substr(1, pos - 1);
                continue;
            }
            size_t pos = trimmed.find('=');
            if (pos == std::string::npos) continue;
            std::string key = simutil::trim(trimmed.substr(0, pos));
            std::string val = simutil::trim(trimmed.substr(pos + 1));
            if (key.empty()) continue;
            mValues[section.empty() ? key : (section + "/" + key)] = val;
        }
    }

    std::string SimulationSettings::value(const std::string& tag, const std::string& defaultValue) const
    {
        auto it = mValues.find(tag);
        if (it == mValues.end()) return defaultValue;
        return it->second;
    }

    void SimulationSettings::setValue(const std::string& tag, const std::string& val)
    {
        auto it = mValues.find(tag);
        if (it != mValues.end() && it->second == val) return;
        mValues[tag] = val;
        mDirty       = true;
    }

    void SimulationSettings::sync()
    {
        if (!mDirty) return;

        std::filesystem::path path(mFilename);
        if (path.has_parent_path())
        {
            hal::error_code ec;
            std::filesystem::create_directories(path.parent_path(), ec);
        }

        std::ofstream of(mFilename, std::ios::binary);
        if (!of.good()) return;

        std::string currentSection;
        for (auto it = mValues.begin(); it != mValues.end(); ++it)
        {
            std::string section;
            std::string key = it->first;
            size_t pos      = it->first.find('/');
            if (pos != std::string::npos)
            {
                section = it->first.substr(0, pos);
                key     = it->first.substr(pos + 1);
            }
            if (section != currentSection)
            {
                if (!currentSection.empty()) of << "\n";
                if (!section.empty()) of << "[" << section << "]\n";
                currentSection = section;
            }
            of << key << "=" << it->second << "\n";
        }
        mDirty = false;
    }

    std::string SimulationSettings::color(ColorSetting cs) const
    {
        std::string tagname = std::string("color/") + sColorSettingTag[cs];
        return value(tagname, sDefaultColor[cs]);
    }

    void SimulationSettings::setColor(ColorSetting cs, const std::string& colName)
    {
        std::string tagname = std::string("color/") + sColorSettingTag[cs];
        setValue(tagname, colName);
    }

    std::map<std::string,std::string> SimulationSettings::engineProperties() const
    {
        std::map<std::string,std::string> retval;
        std::string propertyList = value("engine/properties");
        if (propertyList.empty()) return retval;
        std::vector<std::string> engProp = simutil::split(propertyList, ',');
        int n = (int) engProp.size();
        for (int i=0; i<n; i+=2)
            retval[simutil::trim(engProp.at(i))] = (i+1<n) ? simutil::trim(engProp.at(i+1)) : std::string();
        return retval;
    }

    void SimulationSettings::setEngineProperties(const std::map<std::string,std::string>& engProp)
    {
        std::vector<std::string> slist;
        for (auto it = engProp.begin(); it != engProp.end(); ++it)
        {
            slist.push_back(it->first);
            slist.push_back(it->second);
        }
        setValue("engine/properties", simutil::join(slist, ", "));
    }

    int SimulationSettings::maxSizeLoadable() const
    {
        bool ok;
        int retval = (int) simutil::to_int(value("global/max_loadable"), &ok);
        return ok ? retval : 100000;
    }

    void SimulationSettings::setMaxSizeLoadable(int msl)
    {
        setValue("global/max_loadable", std::to_string(msl));
    }

    int SimulationSettings::maxSizeEditor() const
    {
        bool ok;
        int retval = (int) simutil::to_int(value("global/max_editor"), &ok);
        return ok ? retval : 1000;
    }

    void SimulationSettings::setMaxSizeEditor(int mse)
    {
        setValue("global/max_editor", std::to_string(mse));
    }

    std::string SimulationSettings::baseDirectory() const
    {
        return value("global/base_directory");
    }

    void SimulationSettings::setBaseDirectory(const std::string& dir)
    {
        setValue("global/base_directory", dir);
    }
}
