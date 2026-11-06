#include "report_json.hpp"

#include <string>

#include <gtest/gtest.h>

TEST(ReportJsonTest, QuotesSpecialAndControlCharacters)
{
  const std::string value = "quote\" slash\\ back\b form\f line\n return\r tab\t low\x01";

  EXPECT_EQ(
    runtime_history::json_quote(value),
    "\"quote\\\" slash\\\\ back\\b form\\f line\\n return\\r tab\\t low\\u0001\"");
}
