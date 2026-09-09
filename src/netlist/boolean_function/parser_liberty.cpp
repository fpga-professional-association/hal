#include "hal_core/netlist/boolean_function.h"
#include "hal_core/netlist/boolean_function/parser.h"
#include "hal_core/utilities/log.h"

#include <boost/fusion/include/at_c.hpp>
#include <boost/fusion/sequence/intrinsic/at_c.hpp>
#include <boost/spirit/home/x3.hpp>
#include <set>
#include <utility>
#include <vector>

namespace hal
{
    namespace BooleanFunctionParser
    {
        Result<std::vector<Token>> parse_with_liberty_grammar(const std::string& expression)
        {
            // stores the list of tokens that are generated and filled during the
            // parsing process adn the different semantic actions
            std::vector<Token> tokens;

            // stores the indices of all AND tokens that were generated from
            // whitespace, i.e., from the implicit AND operation of the liberty
            // grammar. Such a token is only kept if it actually connects two
            // operands, see (4) below.
            std::set<u64> implicit_and_indices;

            ////////////////////////////////////////////////////////////////////////
            // (1) Semantic actions to generate tokens
            ////////////////////////////////////////////////////////////////////////

            const auto AndAction         = [&tokens](auto& /* ctx */) { tokens.emplace_back(BooleanFunctionParser::Token::And()); };
            const auto ImplicitAndAction = [&tokens, &implicit_and_indices](auto& /* ctx */) {
                implicit_and_indices.insert((u64)tokens.size());
                tokens.emplace_back(BooleanFunctionParser::Token::And());
            };
            const auto NotAction = [&tokens](auto& /* ctx */) { tokens.emplace_back(BooleanFunctionParser::Token::Not()); };
            const auto OrAction  = [&tokens](auto& /* ctx */) { tokens.emplace_back(BooleanFunctionParser::Token::Or()); };
            const auto XorAction = [&tokens](auto& /* ctx */) { tokens.emplace_back(BooleanFunctionParser::Token::Xor()); };

            const auto NotSuffixAction = [&tokens](auto& /* ctx */) { tokens.emplace_back(BooleanFunctionParser::Token::NotSuffix()); };

            const auto BracketOpenAction  = [&tokens](auto& /* ctx */) { tokens.emplace_back(BooleanFunctionParser::Token::BracketOpen()); };
            const auto BracketCloseAction = [&tokens](auto& /* ctx */) { tokens.emplace_back(BooleanFunctionParser::Token::BracketClose()); };

            const auto VariableAction = [&tokens](auto& ctx) {
                // combines the first matched character with the remaining string
                std::stringstream name;
                name << std::string(1, boost::fusion::at_c<0>(_attr(ctx))) << boost::fusion::at_c<1>(_attr(ctx));

                tokens.emplace_back(BooleanFunctionParser::Token::Variable(name.str(), 1));
            };
            const auto VariableIndexAction = [&tokens](auto& ctx) {
                // combines the first matched character with the remaining string
                std::stringstream name;
                name << std::string(1, boost::fusion::at_c<0>(_attr(ctx))) << boost::fusion::at_c<1>(_attr(ctx)) << boost::fusion::at_c<2>(_attr(ctx)) << boost::fusion::at_c<3>(_attr(ctx))
                     << boost::fusion::at_c<4>(_attr(ctx));
                tokens.emplace_back(BooleanFunctionParser::Token::Variable(name.str(), 1));
            };
            const auto ConstantAction = [&tokens](auto& ctx) {
                auto value = (_attr(ctx) == '0') ? BooleanFunction::Value::ZERO : BooleanFunction::Value::ONE;
                tokens.emplace_back(BooleanFunctionParser::Token::Constant({value}));
            };

            ////////////////////////////////////////////////////////////////////////
            // (2) Rules
            ////////////////////////////////////////////////////////////////////////

            namespace x3 = boost::spirit::x3;

            const auto AndRule         = x3::char_("&*")[AndAction];
            const auto ImplicitAndRule = x3::char_(" \t\r\n")[ImplicitAndAction];
            const auto NotRule         = x3::char_("!~")[NotAction];
            const auto NotSuffixRule = x3::lit("'")[NotSuffixAction];
            const auto OrRule        = x3::char_("|+")[OrAction];
            const auto XorRule       = x3::lit("^")[XorAction];

            const auto BracketOpenRule  = x3::lit("(")[BracketOpenAction];
            const auto BracketCloseRule = x3::lit(")")[BracketCloseAction];

            const auto VariableRule      = x3::lexeme[(x3::char_("a-zA-Z") >> *x3::char_("a-zA-Z0-9_"))][VariableAction];
            const auto VariableIndexRule = x3::lexeme[(x3::char_("a-zA-Z") >> *x3::char_("a-zA-Z0-9_") >> x3::char_("(") >> x3::int_ >> x3::char_(")"))][VariableIndexAction];
            const auto ConstantRule      = x3::lexeme[x3::char_("0-1")][ConstantAction];

            auto iter     = expression.begin();
            const auto ok = x3::phrase_parse(iter,
                                             expression.end(),
                                             ////////////////////////////////////////////////////////////////////
                                             // (3) Parsing Expression Grammar
                                             ////////////////////////////////////////////////////////////////////
                                             +(AndRule | ImplicitAndRule | NotRule | NotSuffixRule | OrRule | XorRule | VariableIndexRule | VariableRule | ConstantRule | BracketOpenRule
                                               | BracketCloseRule),
                                             // we use an invalid a.k.a. non-printable ASCII character in order
                                             // to prevent the skipping of space characters as they are defined
                                             // as an and operation
                                             x3::char_(0x00));

            if (!ok || (iter != expression.end()))
            {
                return ERR("could not to parse Boolean function '" + expression + "': " + std::string(iter, expression.end()));
            }

            ////////////////////////////////////////////////////////////////////////
            // (4) Filter implicit AND operations
            ////////////////////////////////////////////////////////////////////////

            // In the liberty grammar whitespace denotes an AND operation, i.e.,
            // 'A B' is 'A & B'. Whitespace that surrounds another operator or a
            // bracket does not denote an operation, though, which is why 'A * B'
            // or '(A) + B' would otherwise produce a sequence of two operators
            // and hence fail to translate. Such an implicit AND is therefore only
            // kept if it actually connects the end of an operand to the beginning
            // of another one.
            const auto is_operand_end = [](const Token& token) {
                return token.is(TokenType::Variable) || token.is(TokenType::Constant) || token.is(TokenType::BracketClose) || token.is(TokenType::NotSuffix);
            };
            const auto is_operand_begin = [](const Token& token) {
                return token.is(TokenType::Variable) || token.is(TokenType::Constant) || token.is(TokenType::BracketOpen) || token.is(TokenType::Not);
            };

            // (4.1) collapse runs of whitespace into a single implicit AND token
            std::vector<std::pair<Token, bool>> annotated;
            annotated.reserve(tokens.size());
            for (u64 i = 0; i < (u64)tokens.size(); i++)
            {
                const bool is_implicit = (implicit_and_indices.find(i) != implicit_and_indices.end());
                if (is_implicit && !annotated.empty() && annotated.back().second)
                {
                    continue;
                }
                annotated.emplace_back(tokens.at(i), is_implicit);
            }

            // (4.2) drop implicit AND tokens that do not connect two operands
            std::vector<Token> filtered;
            filtered.reserve(annotated.size());
            for (u64 i = 0; i < (u64)annotated.size(); i++)
            {
                if (annotated.at(i).second)
                {
                    if ((i == 0) || ((i + 1) >= (u64)annotated.size()))
                    {
                        continue;
                    }
                    if (!is_operand_end(annotated.at(i - 1).first) || !is_operand_begin(annotated.at(i + 1).first))
                    {
                        continue;
                    }
                }
                filtered.emplace_back(annotated.at(i).first);
            }

            return OK(filtered);
        }
    }    // namespace BooleanFunctionParser
}    // namespace hal
