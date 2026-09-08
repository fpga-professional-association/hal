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

#include <algorithm>
#include <cctype>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <istream>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace hal
{
    /**
     * Small string, number and stream helpers used throughout the simulator plugin.
     *
     * The helpers replicate the semantics of the Qt calls that the plugin used before it was
     * ported to the C++ standard library (`QString::trimmed()`, `QString::split()`,
     * `QString::toInt(&ok)`, `QString::arg()` with field width and base, `QFile::readLine()`).
     * They are collected here so that the semantics are defined in exactly one place.
     */
    namespace simutil
    {
        /// Remove leading and trailing white space, equivalent to QString::trimmed().
        inline std::string trim(const std::string& s)
        {
            size_t p0 = 0;
            size_t p1 = s.size();
            while (p0 < p1 && (unsigned char)s[p0] <= ' ')
            {
                ++p0;
            }
            while (p1 > p0 && (unsigned char)s[p1 - 1] <= ' ')
            {
                --p1;
            }
            return s.substr(p0, p1 - p0);
        }

        /// Split string at delimiter. If `skipEmpty` is set empty parts are dropped (Qt::SkipEmptyParts).
        inline std::vector<std::string> split(const std::string& s, char delim, bool skipEmpty = false)
        {
            std::vector<std::string> retval;
            std::string current;
            for (char cc : s)
            {
                if (cc == delim)
                {
                    if (!skipEmpty || !current.empty())
                    {
                        retval.push_back(current);
                    }
                    current.clear();
                }
                else
                {
                    current += cc;
                }
            }
            if (!skipEmpty || !current.empty())
            {
                retval.push_back(current);
            }
            return retval;
        }

        /// Join list of strings, inserting `joiner` between the elements.
        inline std::string join(const std::vector<std::string>& parts, const std::string& joiner)
        {
            std::string retval;
            bool first = true;
            for (const std::string& s : parts)
            {
                if (!first)
                {
                    retval += joiner;
                }
                retval += s;
                first = false;
            }
            return retval;
        }

        /// Lower case conversion for ASCII characters.
        inline std::string to_lower(const std::string& s)
        {
            std::string retval(s);
            std::transform(retval.begin(), retval.end(), retval.begin(), [](unsigned char cc) { return (char)std::tolower(cc); });
            return retval;
        }

        /// Test whether `s` starts with `prefix`.
        inline bool starts_with(const std::string& s, const std::string& prefix)
        {
            return s.size() >= prefix.size() && s.compare(0, prefix.size(), prefix) == 0;
        }

        /// Test whether `s` ends with `suffix`.
        inline bool ends_with(const std::string& s, const std::string& suffix)
        {
            return s.size() >= suffix.size() && s.compare(s.size() - suffix.size(), suffix.size(), suffix) == 0;
        }

        /**
         * Convert string to integer, equivalent to QString::toInt(bool* ok, int base).
         * The whole string (after trimming) must be consumed, otherwise `ok` is set to `false`.
         */
        inline int64_t to_int(const std::string& s, bool* ok = nullptr, int base = 10)
        {
            std::string txt = trim(s);
            if (txt.empty())
            {
                if (ok)
                {
                    *ok = false;
                }
                return 0;
            }
            errno      = 0;
            char* endp = nullptr;
            long long value = std::strtoll(txt.c_str(), &endp, base);
            bool good       = (errno == 0 && endp && *endp == 0);
            if (ok)
            {
                *ok = good;
            }
            return good ? (int64_t)value : 0;
        }

        /// Convert string to unsigned integer, equivalent to QString::toUInt(bool* ok, int base).
        inline uint64_t to_uint(const std::string& s, bool* ok = nullptr, int base = 10)
        {
            std::string txt = trim(s);
            if (txt.empty() || txt[0] == '-')
            {
                if (ok)
                {
                    *ok = false;
                }
                return 0;
            }
            errno      = 0;
            char* endp = nullptr;
            unsigned long long value = std::strtoull(txt.c_str(), &endp, base);
            bool good                = (errno == 0 && endp && *endp == 0);
            if (ok)
            {
                *ok = good;
            }
            return good ? (uint64_t)value : 0;
        }

        /// Convert string to double, equivalent to QString::toDouble(bool* ok).
        inline double to_double(const std::string& s, bool* ok = nullptr)
        {
            std::string txt = trim(s);
            if (txt.empty())
            {
                if (ok)
                {
                    *ok = false;
                }
                return 0.;
            }
            errno      = 0;
            char* endp = nullptr;
            double value = std::strtod(txt.c_str(), &endp);
            bool good    = (errno == 0 && endp && *endp == 0);
            if (ok)
            {
                *ok = good;
            }
            return good ? value : 0.;
        }

        /**
         * Render number in given base, equivalent to QString::number(int value, int base).
         * Digits above 9 are rendered in lower case, negative values get a leading minus sign.
         */
        inline std::string number(int64_t value, int base = 10)
        {
            if (base == 10)
            {
                return std::to_string(value);
            }
            bool negative   = value < 0;
            uint64_t absval = negative ? (uint64_t)(-value) : (uint64_t)value;
            std::string digits;
            if (!absval)
            {
                digits = "0";
            }
            while (absval)
            {
                int dig = (int)(absval % (uint64_t)base);
                digits += (char)(dig < 10 ? ('0' + dig) : ('a' + dig - 10));
                absval /= (uint64_t)base;
            }
            std::reverse(digits.begin(), digits.end());
            return negative ? ("-" + digits) : digits;
        }

        /**
         * Render unsigned number in given base, zero padded to `width` characters.
         * Equivalent to QString::arg(uint value, int width, int base, QLatin1Char('0')).
         */
        inline std::string number_padded(uint64_t value, int width, int base)
        {
            std::string digits;
            if (!value)
            {
                digits = "0";
            }
            while (value)
            {
                int dig = (int)(value % (uint64_t)base);
                digits += (char)(dig < 10 ? ('0' + dig) : ('a' + dig - 10));
                value /= (uint64_t)base;
            }
            std::reverse(digits.begin(), digits.end());
            while ((int)digits.size() < width)
            {
                digits.insert(digits.begin(), '0');
            }
            return digits;
        }

        /// Lookup in map returning `defaultValue` if key was not found, equivalent to QMap::value(key,default).
        template<typename K, typename V, typename C, typename A>
        inline V map_value(const std::map<K, V, C, A>& container, const K& key, const V& defaultValue = V())
        {
            auto it = container.find(key);
            if (it == container.end())
            {
                return defaultValue;
            }
            return it->second;
        }

        /**
         * Read a single line from stream into `buf` (including the terminating newline character),
         * equivalent to QIODevice::readLine(char* data, qint64 maxSize).
         * @return Number of characters read, zero at end of file.
         */
        inline int read_line(std::istream& is, char* buf, int maxsize)
        {
            int i = 0;
            while (i < maxsize - 1)
            {
                int cc = is.get();
                if (cc < 0)
                {
                    break;
                }
                buf[i++] = (char)cc;
                if (cc == '\n')
                {
                    break;
                }
            }
            buf[i] = 0;
            return i;
        }

        /// Test whether stream reached end of file, equivalent to QIODevice::atEnd().
        inline bool at_end(std::istream& is)
        {
            return is.peek() == std::char_traits<char>::eof();
        }
    }    // namespace simutil
}    // namespace hal
