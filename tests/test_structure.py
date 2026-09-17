import unittest

import server
from android_autodev.tools import TOOL_MODULES


class ProjectStructureTests(unittest.TestCase):
    def test_each_tool_is_registered_once_from_a_domain_module(self):
        grouped_tools = [tool for module in TOOL_MODULES for tool in module.TOOLS]
        grouped_names = [tool.__name__ for tool in grouped_tools]

        self.assertEqual(len(grouped_names), 38)
        self.assertEqual(len(grouped_names), len(set(grouped_names)))
        self.assertEqual(set(grouped_names), set(server.mcp._tool_manager._tools))

    def test_launcher_does_not_define_tool_implementations(self):
        with open("server.py") as handle:
            launcher = handle.read()

        self.assertNotIn("@mcp.tool", launcher)
        self.assertNotIn("async def ", launcher)


if __name__ == "__main__":
    unittest.main()
