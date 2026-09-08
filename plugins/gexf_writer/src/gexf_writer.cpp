#include "gexf_writer/gexf_writer.h"

#include "hal_core/netlist/endpoint.h"
#include "hal_core/netlist/gate.h"
#include "hal_core/netlist/module.h"
#include "hal_core/netlist/net.h"
#include "hal_core/netlist/netlist.h"
#include "hal_core/utilities/log.h"
#include "hal_version.h"

#include <array>
#include <ctime>
#include <fstream>
#include <string>

namespace hal
{
    namespace
    {
        // indentation used for the generated XML, mirrors the auto formatting of the previous Qt based implementation
        std::string indent(int level)
        {
            return std::string(4 * level, ' ');
        }

        std::string escape(const std::string& str)
        {
            std::string res;
            res.reserve(str.size());
            for (char c : str)
            {
                switch (c)
                {
                    case '&':
                        res += "&amp;";
                        break;
                    case '<':
                        res += "&lt;";
                        break;
                    case '>':
                        res += "&gt;";
                        break;
                    case '"':
                        res += "&quot;";
                        break;
                    case '\'':
                        res += "&apos;";
                        break;
                    default:
                        res += c;
                        break;
                }
            }
            return res;
        }

        // deterministic fallback palette, used to color nodes by the module they belong to
        const std::array<std::array<u32, 3>, 12> palette = {{{{31, 119, 180}},
                                                             {{255, 127, 14}},
                                                             {{44, 160, 44}},
                                                             {{214, 39, 40}},
                                                             {{148, 103, 189}},
                                                             {{140, 86, 75}},
                                                             {{227, 119, 194}},
                                                             {{127, 127, 127}},
                                                             {{188, 189, 34}},
                                                             {{23, 190, 207}},
                                                             {{174, 199, 232}},
                                                             {{255, 187, 120}}}};

        std::string current_date()
        {
            const std::time_t now = std::time(nullptr);
            std::tm tm_buf {};
#ifdef _WIN32
            localtime_s(&tm_buf, &now);
#else
            localtime_r(&now, &tm_buf);
#endif
            char buffer[16] = {0};
            if (std::strftime(buffer, sizeof(buffer), "%Y-%m-%d", &tm_buf) == 0)
            {
                return std::string();
            }
            return std::string(buffer);
        }
    }    // namespace

    Result<std::monostate> GexfWriter::write(Netlist* netlist, const std::filesystem::path& file_path)
    {
        if (netlist == nullptr)
        {
            return ERR("could not write netlist to GEXF file '" + file_path.string() + "': netlist is a 'nullptr'");
        }
        mNetlist = netlist;

        if (file_path.empty())
        {
            return ERR("could not write netlist to GEXF file '" + file_path.string() + "': file path is empty");
        }

        std::filesystem::path out_path = file_path;
        if (out_path.extension() != ".gexf")
        {
            out_path.replace_extension(".gexf");
        }

        std::stringstream res_stream;
        res_stream << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>" << std::endl;
        res_stream << "<gexf xmlns=\"http://www.gexf.net/1.2draft\" xmlns:viz=\"http://www.gexf.net/1.2draft/viz\" xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\" "
                      "xsi:schemaLocation=\"http://www.gexf.net/1.2draft http://www.gexf.net/1.2draft/gexf.xsd\" version=\"1.2\">"
                   << std::endl;
        writeMeta(res_stream);
        writeGraph(res_stream);
        res_stream << "</gexf>" << std::endl;

        std::ofstream file;
        file.open(out_path.string(), std::ofstream::out);
        if (!file.is_open())
        {
            return ERR("could not write netlist to GEXF file '" + out_path.string() + "': failed to open file");
        }
        file << res_stream.str();
        file.close();

        return OK({});
    }

    void GexfWriter::writeMeta(std::stringstream& res_stream) const
    {
        res_stream << indent(1) << "<meta lastmodifieddate=\"" << escape(current_date()) << "\">" << std::endl;
        res_stream << indent(2) << "<creator>hal " << hal_version::major << "." << hal_version::minor << "." << hal_version::patch << "</creator>" << std::endl;
        res_stream << indent(1) << "</meta>" << std::endl;
    }

    void GexfWriter::writeGraph(std::stringstream& res_stream)
    {
        res_stream << indent(1) << "<graph defaultedgetype=\"directed\" mode=\"static\">" << std::endl;
        // Edge attributes
        res_stream << indent(2) << "<attributes mode=\"static\" class=\"edge\">" << std::endl;
        writeAttribute(res_stream, 3, "label", "string");
        writeAttribute(res_stream, 4, "hal_id", "long");
        writeAttribute(res_stream, 5, "source_pin", "string");
        writeAttribute(res_stream, 6, "destination_pin", "string");
        writeAttribute(res_stream, 7, "networkx_key", "long");
        res_stream << indent(2) << "</attributes>" << std::endl;
        // Node attributes
        res_stream << indent(2) << "<attributes mode=\"static\" class=\"node\">" << std::endl;
        writeAttribute(res_stream, 0, "type", "string");
        writeAttribute(res_stream, 1, "module", "string");
        writeAttribute(res_stream, 2, "INIT", "string");
        res_stream << indent(2) << "</attributes>" << std::endl;
        // end attributes

        res_stream << indent(2) << "<nodes>" << std::endl;
        for (const Gate* g : mNetlist->get_gates())
            writeNode(res_stream, g);
        res_stream << indent(2) << "</nodes>" << std::endl;

        res_stream << indent(2) << "<edges>" << std::endl;
        for (const Net* n : mNetlist->get_nets())
            writeEdge(res_stream, n);
        res_stream << indent(2) << "</edges>" << std::endl;

        res_stream << indent(1) << "</graph>" << std::endl;
    }

    void GexfWriter::writeAttribute(std::stringstream& res_stream, int id, const std::string& title, const std::string& type) const
    {
        res_stream << indent(3) << "<attribute id=\"" << id << "\" title=\"" << escape(title) << "\" type=\"" << escape(type) << "\"/>" << std::endl;
    }

    void GexfWriter::writeNode(std::stringstream& res_stream, const Gate* g) const
    {
        res_stream << indent(3) << "<node id=\"" << g->get_id() << "\" label=\"" << escape(g->get_name()) << "\">" << std::endl;
        writeColor(res_stream, g);
        res_stream << indent(4) << "<attvalues>" << std::endl;
        writeNodeAttribute(res_stream, g, 0);
        writeNodeAttribute(res_stream, g, 1);
        writeNodeAttribute(res_stream, g, 2);
        res_stream << indent(4) << "</attvalues>" << std::endl;
        res_stream << indent(3) << "</node>" << std::endl;
    }

    void GexfWriter::writeColor(std::stringstream& res_stream, const Gate* g) const
    {
        const Module* mod = g->get_module();
        if (mod == nullptr)
            return;

        // headless fallback: deterministic color derived from the module ID
        const std::array<u32, 3>& col = palette[mod->get_id() % palette.size()];
        res_stream << indent(4) << "<viz:color r=\"" << col[0] << "\" g=\"" << col[1] << "\" b=\"" << col[2] << "\"/>" << std::endl;
    }

    void GexfWriter::writeNodeAttribute(std::stringstream& res_stream, const Gate* g, int inx) const
    {
        std::string value;
        switch (inx)
        {
            case 0:
                value = g->get_type()->get_name();
                break;
            case 1:
                value = g->get_module()->get_name();
                break;
            case 2:
                for (const auto& [key, val] : g->get_data_map())
                {
                    if (std::get<1>(key) == "INIT")
                    {
                        value = std::get<1>(val);
                        break;
                    }
                }
                break;
        }

        if (value.empty())
            return;

        res_stream << indent(5) << "<attvalue for=\"" << inx << "\" value=\"" << escape(value) << "\"/>" << std::endl;
    }

    void GexfWriter::writeEdge(std::stringstream& res_stream, const Net* n)
    {
        for (const Endpoint* epSrc : n->get_sources())
        {
            Gate* gSrc = epSrc->get_gate();
            if (!gSrc)
                continue;

            for (const Endpoint* epDst : n->get_destinations())
            {
                Gate* gDst = epDst->get_gate();
                if (!gDst)
                    continue;

                res_stream << indent(3) << "<edge source=\"" << gSrc->get_id() << "\" target=\"" << gDst->get_id() << "\" id=\"" << mEdgeId++ << "\">" << std::endl;
                res_stream << indent(4) << "<attvalues>" << std::endl;
                writeEdgeAttribute(res_stream, n, 3);
                writeEdgeAttribute(res_stream, n, 4);
                writeEdgeAttribute(res_stream, n, 5, epSrc->get_pin()->get_name());
                writeEdgeAttribute(res_stream, n, 6, epDst->get_pin()->get_name());
                writeEdgeAttribute(res_stream, n, 7);
                res_stream << indent(4) << "</attvalues>" << std::endl;
                res_stream << indent(3) << "</edge>" << std::endl;
            }
        }
    }

    void GexfWriter::writeEdgeAttribute(std::stringstream& res_stream, const Net* n, int inx, const std::string& pin) const
    {
        std::string value;
        switch (inx)
        {
            case 3:
                value = n->get_name();
                break;
            case 4:
                value = std::to_string(n->get_id());
                break;
            case 5:
            case 6:
                value = pin;
                break;
            case 7:
                value = "0";
                break;    // networkx_key
        }

        if (value.empty())
            return;

        res_stream << indent(5) << "<attvalue for=\"" << inx << "\" value=\"" << escape(value) << "\"/>" << std::endl;
    }

}    // namespace hal
