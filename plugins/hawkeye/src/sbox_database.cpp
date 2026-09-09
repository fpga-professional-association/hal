#include "hawkeye/sbox_database.h"

#include "hal_core/utilities/log.h"
#include "rapidjson/document.h"
#include "rapidjson/filereadstream.h"
#include "rapidjson/stringbuffer.h"
#include "rapidjson/writer.h"
#include "smallset.h"

#include <cmath>
#include <fstream>
#include <iostream>
#include <vector>

namespace hal
{
    namespace hawkeye
    {
        namespace
        {
            /**
             * The number of recursive steps that computing one linear representative may spend before the search is
             * abandoned, see the budget check in `subroutine`. Defined here because the S-box database consults it
             * before the search itself is defined.
             *
             * Measured against every stored variant of every S-box of the shipped database, the most expensive search
             * of a real S-box is AES at roughly 67 000 steps, while Ascon needs 2 600 and the 4-bit S-boxes 370. The
             * limit therefore leaves a real S-box a margin of more than three times its worst observed case, and no
             * S-box of the database comes anywhere near it. Tables that are close to linear, on the other hand, run
             * into it immediately: looking up an 8-bit table that glues two 4-bit S-boxes together takes 99 seconds
             * without the limit and half a second with it, and the identity permutation does not finish at all. Both
             * are tables a round function can produce, and neither can be a real S-box, which is why giving up on
             * them costs nothing.
             */
            constexpr u64 LINEAR_REPRESENTATIVE_BUDGET = 250000;

            std::vector<u8> compute_linear_representative_bounded(const std::vector<u8>& sbox, u64& budget);
        }    // namespace

        SBoxDatabase::SBoxDatabase(const std::map<std::string, std::vector<u8>>& sboxes)
        {
            add(sboxes).is_ok();
        }

        Result<SBoxDatabase> SBoxDatabase::from_file(const std::filesystem::path& file_path)
        {
            auto db = SBoxDatabase();
            if (const auto res = db.load(file_path); res.is_ok())
            {
                return OK(db);
            }
            else
            {
                return ERR(res.get_error());
            }
        }

        Result<std::monostate> SBoxDatabase::add(const std::string& name, const std::vector<u8>& sbox)

        {
            u32 bit_size = std::log2(sbox.size());

            if (bit_size > 8)
            {
                return ERR("S-box '" + name + "' has bit-size greater 8, but only S-boxes of up to 8 bits are supported");
            }

            for (size_t alpha = 0; alpha < sbox.size(); alpha++)
            {
                std::vector<u8> sbox_alpha;
                for (u32 i = 0; i < sbox.size(); i++)
                {
                    sbox_alpha.push_back(sbox.at(i) ^ alpha);
                }
                u64 budget   = LINEAR_REPRESENTATIVE_BUDGET;
                auto lin_rep = compute_linear_representative_bounded(sbox_alpha, budget);
                if (budget == 0)
                {
                    // storing a representative from a truncated search would silently break every lookup against this
                    // entry, and a real S-box is never degenerate enough to exhaust the search in the first place
                    return ERR("cannot add S-box '" + name + "' to the database: the canonical form search was abandoned, the S-box is too close to linear");
                }
                m_data[bit_size][lin_rep].push_back(std::make_pair(name, alpha));
            }
            return OK({});
        }

        Result<std::monostate> SBoxDatabase::add(const std::map<std::string, std::vector<u8>>& sboxes)
        {
            for (const auto& [name, sbox] : sboxes)
            {
                if (const auto res = add(name, sbox); res.is_error())
                {
                    return ERR(res.get_error());
                }
            }
            return OK({});
        }

        Result<std::monostate> SBoxDatabase::load(const std::filesystem::path& file_path, bool overwrite)
        {
            FILE* fp = fopen(file_path.string().c_str(), "r");
            if (fp == NULL)
            {
                return ERR("could not parse S-box database file '" + file_path.string() + "' : unable to open file");
            }

            char buffer[65536];
            rapidjson::FileReadStream is(fp, buffer, sizeof(buffer));
            rapidjson::Document document;
            document.ParseStream<0, rapidjson::UTF8<>, rapidjson::FileReadStream>(is);
            fclose(fp);

            if (document.HasParseError())
            {
                return ERR("could not parse S-box database file '" + file_path.string() + "': failed parsing JSON format");
            }

            if (overwrite)
            {
                m_data.clear();
            }

            for (auto size_it = document.MemberBegin(); size_it != document.MemberEnd(); ++size_it)
            {
                u32 bit_size                       = std::stoul(std::string(size_it->name.GetString()));
                const rapidjson::Value& cipher_val = size_it->value;

                for (auto cipher_it = cipher_val.MemberBegin(); cipher_it != cipher_val.MemberEnd(); ++cipher_it)
                {
                    std::string cipher_name           = cipher_it->name.GetString();
                    const rapidjson::Value& const_val = cipher_it->value;

                    for (auto const_it = const_val.MemberBegin(); const_it != const_val.MemberEnd(); ++const_it)
                    {
                        u8 const_alpha                      = (u8)std::stoul(std::string(const_it->name.GetString()));
                        const rapidjson::Value& lin_rep_val = const_it->value;

                        std::vector<u8> lin_rep;
                        for (u32 i = 0; i < lin_rep_val.Size(); i++)
                        {
                            lin_rep.push_back((u8)(lin_rep_val[i].GetUint()));
                        }

                        m_data[bit_size][lin_rep].push_back(std::make_pair(cipher_name, const_alpha));
                    }
                }
            }

            return OK({});
        }

        Result<std::monostate> SBoxDatabase::store(const std::filesystem::path& file_path) const
        {
            FILE* fp = fopen(file_path.string().c_str(), "w");
            if (fp == NULL)
            {
                return ERR("could not write S-box database file '" + file_path.string() + "' : unable to open file");
            }

            rapidjson::Document document;
            document.SetObject();

            rapidjson::Document::AllocatorType& allocator = document.GetAllocator();

            for (const auto& [bit_size, lin_rep_map] : m_data)
            {
                std::map<std::string, std::map<u8, std::vector<u8>>> pretty_data;
                for (const auto& [lin_rep, cipher_vec] : lin_rep_map)
                {
                    for (const auto& [name, alpha] : cipher_vec)
                    {
                        pretty_data[name][alpha] = lin_rep;
                    }
                }

                rapidjson::Value cipher_json(rapidjson::kObjectType);
                for (const auto& [cipher_name, lin_rep_map] : pretty_data)
                {
                    rapidjson::Value alpha_json(rapidjson::kObjectType);
                    for (const auto& [const_alph, lin_rep] : lin_rep_map)
                    {
                        rapidjson::Value lin_rep_json(rapidjson::kArrayType);
                        for (const auto val : lin_rep)
                        {
                            lin_rep_json.PushBack(val, allocator);
                        }
                        alpha_json.AddMember(rapidjson::Value(std::to_string(const_alph).c_str(), allocator).Move(), lin_rep_json, allocator);
                    }
                    cipher_json.AddMember(rapidjson::Value(cipher_name.c_str(), allocator).Move(), alpha_json, allocator);
                }
                document.AddMember(rapidjson::Value(std::to_string(bit_size).c_str(), allocator).Move(), cipher_json, allocator);
            }

            rapidjson::StringBuffer buffer;
            rapidjson::Writer<rapidjson::StringBuffer> writer(buffer);

            document.Accept(writer);

            std::ofstream file(file_path);
            if (!file.is_open())
            {
                return ERR("could not store the S-box database: failed to open file '" + file_path.string() + "'");
            }
            file << buffer.GetString();
            file.close();

            return OK({});
        }

        Result<std::string> SBoxDatabase::lookup(const std::vector<u8>& sbox) const
        {
            u32 bit_size = std::log2(sbox.size());

            if (bit_size > 8)
            {
                return ERR("S-box has bit-size greater 8, but only S-boxes of up to 8 bits are supported");
            }

            const auto size_it = m_data.find(bit_size);
            if (size_it == m_data.end())
            {
                return ERR("no S-box of matching bit-size of " + std::to_string(bit_size) + " bits contained in database");
            }

            // beta has to count beyond the largest table index, so it must be wider than a table entry: a u8 stays
            // below a size of 256 forever, which made this loop endless for every 8-bit S-box not in the database
            for (u32 beta = 0; beta < sbox.size(); beta++)
            {
                std::vector<u8> sbox_beta;
                for (u32 i = 0; i < sbox.size(); i++)
                {
                    sbox_beta.push_back(sbox.at(i) ^ beta);
                }

                u64 budget   = LINEAR_REPRESENTATIVE_BUDGET;
                auto lin_rep = compute_linear_representative_bounded(sbox_beta, budget);
                if (budget == 0)
                {
                    // XORing a constant onto the outputs does not change how close to linear the table is, so if the
                    // search degenerates for one beta it degenerates for all of them, and no real S-box ever does
                    log_info("hawkeye", "giving up the S-box lookup, as the table is too close to linear to be a real S-box.");
                    break;
                }

                const auto& matching_size_data = std::get<1>(*size_it);
                const auto rep_it              = matching_size_data.find(lin_rep);
                if (rep_it != matching_size_data.end())
                {
                    return OK(rep_it->second.front().first);
                }
            }

            return ERR("no match found within database");
        }

        void SBoxDatabase::print() const
        {
            for (const auto& [bit_size, lin_rep_map] : m_data)
            {
                std::cout << std::endl;
                std::cout << "### WIDTH: " << bit_size << std::endl;
                std::cout << "#######################" << std::endl;

                std::map<std::string, std::map<u8, std::vector<u8>>> pretty_data;
                for (const auto& [lin_rep, cipher_vec] : lin_rep_map)
                {
                    for (const auto& [name, alpha] : cipher_vec)
                    {
                        pretty_data[name][alpha] = lin_rep;
                    }
                }

                for (const auto& [cipher_name, lin_rep_map] : pretty_data)
                {
                    std::cout << "* " << cipher_name << std::endl;

                    for (const auto& [const_alph, lin_rep] : lin_rep_map)
                    {
                        std::cout << " - " << (u32)const_alph << ": [" << (u32)(lin_rep.at(0));
                        for (u32 i = 1; i < lin_rep.size(); i++)
                        {
                            std::cout << ", " << (u32)(lin_rep.at(i));
                        }
                        std::cout << "]" << std::endl;
                    }
                }
            }

            std::cout << std::endl;
        }

        namespace
        {
            using namespace detail;

            // state of the linear_representative algorithm
            typedef struct
            {
                std::vector<u8> A;
                std::vector<u8> B;
                std::vector<u8> R_S;
                smallset_t D_A;
                smallset_t D_B;
                smallset_t C_A;
                smallset_t C_B;
                smallset_t N_A;
                smallset_t N_B;
                smallset_t U_A;
                smallset_t U_B;
            } state_t;

            // lexicographically compare R_S and R_S_best
            bool is_greater(const std::vector<u8>& R_S, const std::vector<u8>& R_S_best, const u32 len)
            {
                if ((R_S_best[0] == 0) && (R_S_best[1] == 0))
                    return false;

                for (u32 x = 0; x < len; x++)
                {
                    // special case: R_S[x] not defined (=> 0) and R_S_best[x] = 0
                    // works out with this
                    if (R_S[x] > R_S_best[x])
                        return true;
                    if (R_S[x] < R_S_best[x])
                        return false;
                }
                // can happen if there are self equivalences (?)
                return false;
            }

            bool update_linear(std::vector<u8>& A, u8 new_x, const u32 len)
            {
                u8 new_y = A[new_x];
                for (u32 i = 1; i < len; i++)
                {
                    u8 e = A[i];
                    if (e == 0)
                        continue;
                    else if (A[new_x ^ i] == 0)
                        A[new_x ^ i] = e ^ new_y;
                    else if (A[new_x ^ i] != (e ^ new_y))
                    {
                        return false;
                    }
                }
                return true;
            }

            bool subroutine(const std::vector<u8>& S, const std::vector<u8>& S_inv, const state_t& state, std::vector<u8>& R_S_best, const u32 len, u64& budget)
            {
                // The search backtracks over guesses of a linear map, which finishes quickly for anything that looks
                // like a real S-box but degenerates on tables that are close to linear, as those have too many linear
                // self-equivalences to enumerate. Such a table cannot be a real S-box, so give up on it instead:
                // R_S_best then holds the best representative found so far, which still belongs to the equivalence
                // class of S, so a truncated search can only miss a match in the database, never invent one.
                if (budget == 0)
                {
                    return false;
                }
                budget--;

                std::vector<u8> A(state.A);
                std::vector<u8> B(state.B);
                std::vector<u8> R_S(state.R_S);

                smallset_t D_A = (state.D_A);
                smallset_t D_B = (state.D_B);
                smallset_t C_A = (state.C_A);
                smallset_t C_B = (state.C_B);
                smallset_t N_A = (state.N_A);
                smallset_t N_B = (state.N_B);
                smallset_t U_A = (state.U_A);
                smallset_t U_B = (state.U_B);

                while (!smallset_is_empty(N_A))
                {
                    u8 x = smallset_least_element(N_A);
                    u8 y = smallset_least_element(U_B);

                    B[y] = S[A[x]];
                    if (!update_linear(B, y, len))
                        return false;
                    smallset_t D_B_new = smallset_shift(D_B, y);
                    D_B                = smallset_union(D_B, D_B_new);
                    U_B                = smallset_setminus(U_B, D_B_new, len);

                    smallset_t SoA_N_A = smallset_init_empty();
                    for (u8 x : smallset_get_elements(N_A))
                    {
                        SoA_N_A = smallset_add_element(SoA_N_A, S[A[x]]);
                    }
                    smallset_t B_D_B_new = smallset_init_empty();
                    for (u8 d : smallset_get_elements(D_B_new))
                    {
                        B_D_B_new = smallset_add_element(B_D_B_new, B[d]);
                        if (smallset_elm_is_in_set(B[d], SoA_N_A))
                        {
                            C_B = smallset_add_element(C_B, d);
                        }
                        else
                        {
                            N_B = smallset_add_element(N_B, d);
                        }
                    }
                    smallset_t C_A_new = smallset_init_empty();
                    for (u8 x : smallset_get_elements(N_A))
                    {
                        if (smallset_elm_is_in_set(S[A[x]], B_D_B_new))
                        {
                            C_A_new = smallset_add_element(C_A_new, x);
                        }
                    }
                    C_A = smallset_union(C_A, C_A_new);
                    N_A = smallset_setminus(N_A, C_A_new, len);
                    for (u8 x : smallset_get_elements(C_A_new))
                    {
                        u8 y = 0;
                        for (u32 i = 0; i < len; i++)
                        {
                            if (B[i] == S[A[x]])
                            {
                                y = i;
                                break;
                            }
                        }
                        R_S[x] = y;
                    }
                    if (is_greater(R_S, R_S_best, len))
                    {
                        return false;
                    }

                    while (smallset_is_empty(N_A) && !smallset_is_empty(N_B))
                    {
                        u8 x = smallset_least_element(U_A);
                        u8 y = smallset_least_element(N_B);
                        A[x] = S_inv[B[y]];
                        if (!update_linear(A, x, len))
                        {
                            return false;
                        }
                        smallset_t D_A_new    = smallset_shift(D_A, x);
                        D_A                   = smallset_union(D_A, D_A_new);
                        U_A                   = smallset_setminus(U_A, D_A_new, len);
                        smallset_t SinvoB_N_B = smallset_init_empty();
                        for (u8 y : smallset_get_elements(N_B))
                        {
                            SinvoB_N_B = smallset_add_element(SinvoB_N_B, S_inv[B[y]]);
                        }
                        smallset_t A_D_A_new = smallset_init_empty();
                        for (u8 d : smallset_get_elements(D_A_new))
                        {
                            A_D_A_new = smallset_add_element(A_D_A_new, A[d]);
                            if (smallset_elm_is_in_set(A[d], SinvoB_N_B))
                            {
                                C_A = smallset_add_element(C_A, d);
                            }
                            else
                            {
                                N_A = smallset_add_element(N_A, d);
                            }
                        }
                        smallset_t C_B_new = smallset_init_empty();
                        for (u8 y : smallset_get_elements(N_B))
                        {
                            if (smallset_elm_is_in_set(S_inv[B[y]], A_D_A_new))
                            {
                                C_B_new = smallset_add_element(C_B_new, y);
                            }
                        }
                        C_B = smallset_union(C_B, C_B_new);
                        N_B = smallset_setminus(N_B, C_B_new, len);
                        for (u8 y : smallset_get_elements(C_B_new))
                        {
                            u8 x = 0;
                            for (u32 i = 0; i < len; i++)
                            {
                                if (A[i] == S_inv[B[y]])
                                {
                                    x = i;
                                    break;
                                }
                            }
                            R_S[x] = y;
                        }
                        if (is_greater(R_S, R_S_best, len))
                        {
                            return false;
                        }
                    }
                }
                if (smallset_is_empty(U_A) && smallset_is_empty(U_B))
                {
                    for (u32 i = 0; i < len; i++)
                    {
                        // new best
                        R_S_best[i] = R_S[i];
                    }
                    return true;
                }
                else
                {
                    u8 x               = smallset_least_element(U_A);
                    smallset_t D_A_new = smallset_shift(D_A, x);
                    U_A                = smallset_setminus(U_A, D_A_new, len);
                    D_A                = smallset_union(D_A, D_A_new);
                    N_A                = smallset_union(N_A, D_A_new);
                    bool flag          = false;
                    smallset_t Y       = smallset_init_full(len);
                    smallset_t A_set   = smallset_init_empty();
                    for (u32 i = 0; i < len; i++)
                    {
                        A_set = smallset_add_element(A_set, A[i]);
                    }
                    Y = smallset_setminus(Y, A_set, len);
                    for (u8 y : smallset_get_elements(Y))
                    {
                        std::vector<u8> A_next_guess(len);

                        for (u32 i = 0; i < len; i++)
                        {
                            A_next_guess[i] = A[i];
                        }
                        A_next_guess[x] = y;
                        if (!update_linear(A_next_guess, x, len))
                            continue;
                        state_t state_next;
                        state_next.A   = A_next_guess;
                        state_next.B   = B;
                        state_next.R_S = R_S;
                        state_next.D_A = D_A;
                        state_next.D_B = D_B;
                        state_next.C_A = C_A;
                        state_next.C_B = C_B;
                        state_next.N_A = N_A;
                        state_next.N_B = N_B;
                        state_next.U_A = U_A;
                        state_next.U_B = U_B;

                        if (subroutine(S, S_inv, state_next, R_S_best, len, budget))
                        {
                            flag = true;
                        }
                    }

                    return flag;
                }
            }
            std::vector<u8> compute_linear_representative_bounded(const std::vector<u8>& sbox, u64& budget)
            {
            u32 len = sbox.size();

            // variable for current best candidate
            std::vector<u8> R_S_best(len, 0);

            // invert sbox
            std::vector<u8> S_inv(len, 0);
            for (u32 x = 0; x < len; x++)
            {
                u8 y     = sbox[x];
                S_inv[y] = x;
            }

            // init the state of the algorithm
            state_t state;
            state.A   = std::vector<u8>(len, 0);
            state.B   = std::vector<u8>(len, 0);
            state.R_S = std::vector<u8>(len, 0);

            state.D_A = smallset_add_element(smallset_init_empty(), 0);
            state.D_B = smallset_add_element(smallset_init_empty(), 0);

            state.C_A = smallset_init_empty();
            state.C_B = smallset_init_empty();

            state.N_A = smallset_add_element(smallset_init_empty(), 0);
            state.N_B = smallset_add_element(smallset_init_empty(), 0);

            state.U_A = smallset_setminus(smallset_init_full(len), state.D_A, len);
            state.U_B = smallset_setminus(smallset_init_full(len), state.D_A, len);

            // init in special case S[0] == 0
            if (sbox[0] == 0)
            {
                state.C_A = smallset_add_element(smallset_init_empty(), 0);
                state.C_B = smallset_add_element(smallset_init_empty(), 0);

                state.N_A = smallset_init_empty();
                state.N_B = smallset_init_empty();
            }

            // compute linear representative recursively
            subroutine(sbox, S_inv, state, R_S_best, len, budget);

            return R_S_best;
            }
        }    // namespace

        std::vector<u8> SBoxDatabase::compute_linear_representative(const std::vector<u8>& sbox)
        {
            u64 budget = LINEAR_REPRESENTATIVE_BUDGET;
            auto res   = compute_linear_representative_bounded(sbox, budget);
            if (budget == 0)
            {
                log_info("hawkeye", "gave up computing the canonical form of a table of {} entries, as it is too close to linear to be a real S-box.", sbox.size());
            }
            return res;
        }
    }    // namespace hawkeye
}    // namespace hal
