"""Verify desktop release packages start after extraction outside the source tree."""

import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
NODE = os.environ.get("AIRSHOWER_NODE") or os.environ.get("AIRSHOWER_TEST_NODE") or shutil.which("node")


def _builder():
    spec = importlib.util.spec_from_file_location("airshower_build_desktop", ROOT / "scripts" / "build_desktop.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PackagingTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="airshower packaging ")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.output = self.directory / "releases"
        _builder().build(self.output)

    def assert_archive(self, archive_path, allowed):
        with zipfile.ZipFile(archive_path) as archive:
            names = archive.namelist()
            self.assertEqual(len(names), len(set(names)), "Duplicate archive entries are ambiguous")
            self.assertEqual(set(names), allowed)
            for name in names:
                path = PurePosixPath(name)
                self.assertFalse(path.is_absolute())
                self.assertNotIn("..", path.parts)
            self.assertIsNone(archive.testzip())
            self.assertEqual(archive.read("server/server.mjs"),
                             (ROOT / "integrations" / "desktop" / "server.mjs").read_bytes())
            self.assertEqual(archive.read("LICENSE"), (ROOT / "LICENSE").read_bytes())

    def extract(self, archive_path):
        destination = self.directory / "portable plugin with spaces"
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(destination)
        return destination

    def assert_mcp_starts(self, server):
        self.assertEqual(server["command"], "node")
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("AIRSHOWER_")}
        environment.update(server["env"])
        environment["AIRSHOWER_STATE_DIR"] = str(self.directory / "private state")
        # The host's executable discovery is outside the package. Supplying a
        # known Node path here tests the extracted server without installing it.
        process = subprocess.Popen(
            [str(NODE)] + server["args"], cwd=self.directory, env=environment,
            text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        output = queue.Queue()

        def read_lines():
            for line in process.stdout:
                output.put(line)

        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()

        def send(message):
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        def receive(expected_id):
            try:
                line = output.get(timeout=5)
            except queue.Empty:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                self.fail("Packaged server did not answer MCP: " + process.stderr.read())
            response = json.loads(line)
            self.assertEqual(response.get("id"), expected_id)
            self.assertNotIn("error", response)
            return response["result"]

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "airshower-package-test", "version": "1.0.0"},
            }})
            self.assertEqual(receive(1)["protocolVersion"], "2025-11-25")
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            names = {tool["name"] for tool in receive(2)["tools"]}
            self.assertTrue({"ready", "peers", "request", "wait_for_request"}.issubset(names))
            self.assertNotIn("review_request", names,
                             "Human decisions must not be offered to a host without elicitation")
            process.stdin.close()
            self.assertEqual(process.wait(timeout=5), 0, process.stderr.read())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


class CodexPackagingTests(PackagingTestCase):
    def setUp(self):
        super().setUp()
        self.archive = self.output / "airshower-codex-0.2.0.zip"

    def test_archive_contains_only_distributable_plugin_files(self):
        self.assert_archive(self.archive, {
            "plugin.json", "mcp.json", "skills/airshower/SKILL.md",
            "server/server.mjs", "LICENSE",
        })

    def test_manifest_references_resolve_inside_relocated_plugin(self):
        destination = self.extract(self.archive)
        manifest = json.loads((destination / "plugin.json").read_text())
        mcp = json.loads((destination / "mcp.json").read_text())
        setup = manifest["extensions"]["com.openai"]["onboardingSkill"]
        self.assertTrue(setup.startswith("./"))
        self.assertNotIn("..", PurePosixPath(setup).parts)
        self.assertTrue((destination / setup).is_file())
        server = mcp["mcpServers"]["airshower"]
        self.assertEqual(server["type"], "stdio")
        self.assertEqual(server["env"]["AIRSHOWER_CLIENT_NAME"], "Codex")
        self.assertEqual(server["cwd"], "${PLUGIN_ROOT}")
        self.assertEqual(server["command"], "node")
        self.assertEqual(len(server["args"]), 1)
        launch_path = Path(server["args"][0].replace("${PLUGIN_ROOT}", str(destination)))
        self.assertTrue(launch_path.is_file())
        self.assertEqual(launch_path.resolve().parent.parent, destination.resolve())
        self.assertFalse(any("TOKEN" in key or "INVITATION" in key for key in server["env"]))

    @unittest.skipUnless(NODE, "Node.js unavailable; set AIRSHOWER_NODE for the packaged runtime smoke test")
    def test_relocated_packaged_server_negotiates_mcp_without_configuration(self):
        destination = self.extract(self.archive)
        mcp = json.loads((destination / "mcp.json").read_text())
        server = mcp["mcpServers"]["airshower"]
        server["args"] = [arg.replace("${PLUGIN_ROOT}", str(destination)) for arg in server["args"]]
        self.assert_mcp_starts(server)


class ClaudePackagingTests(PackagingTestCase):
    def setUp(self):
        super().setUp()
        self.archive = self.output / "airshower-0.2.0.mcpb"

    def manifest_server(self, destination):
        manifest = json.loads((destination / "manifest.json").read_text())
        substitutions = {"__dirname": str(destination)}
        # Simulate unconfigured installation fields. No actual invitation,
        # identity, or relay is used, and no ready/enrollment tool is called.
        substitutions.update({"user_config." + name: "" for name in manifest["user_config"]})

        def expand(value):
            if isinstance(value, str):
                return re.sub(r"\$\{([^}]+)\}", lambda match: substitutions[match.group(1)], value)
            if isinstance(value, list):
                return [expand(item) for item in value]
            if isinstance(value, dict):
                return {key: expand(item) for key, item in value.items()}
            return value

        return manifest, expand(manifest["server"]["mcp_config"])

    def test_mcpb_contains_only_distributable_extension_files(self):
        self.assert_archive(self.archive, {"manifest.json", "server/server.mjs", "LICENSE", "PRIVACY.md"})
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(archive.read("PRIVACY.md"), (ROOT / "docs" / "privacy.md").read_bytes())

    def test_mcpb_manifest_interpolates_paths_and_private_configuration(self):
        destination = self.extract(self.archive)
        manifest, server = self.manifest_server(destination)
        self.assertEqual(manifest["server"]["type"], "node")
        entry_point = PurePosixPath(manifest["server"]["entry_point"])
        self.assertFalse(entry_point.is_absolute())
        self.assertNotIn("..", entry_point.parts)
        self.assertTrue((destination / entry_point).is_file())
        self.assertEqual(server["command"], "node")
        self.assertEqual(server["args"], [str(destination / entry_point)])
        self.assertEqual(server["env"]["AIRSHOWER_CLIENT_NAME"], "Claude Desktop")
        for name, env_name in (("invitation", "AIRSHOWER_INVITATION"),
                               ("approval_password", "AIRSHOWER_APPROVAL_PASSWORD")):
            setting = manifest["user_config"][name]
            self.assertTrue(setting["sensitive"])
            self.assertNotIn("default", setting, "Never ship a default credential")
            self.assertEqual(manifest["server"]["mcp_config"]["env"][env_name],
                             "${user_config." + name + "}")
            self.assertEqual(server["env"][env_name], "")
        self.assertNotIn("${", json.dumps(server), "Every host placeholder must be supported and resolved")

    @unittest.skipUnless(NODE, "Node.js unavailable; set AIRSHOWER_NODE for the packaged runtime smoke test")
    def test_relocated_mcpb_negotiates_mcp_without_configuration(self):
        destination = self.extract(self.archive)
        _, server = self.manifest_server(destination)
        self.assert_mcp_starts(server)


if __name__ == "__main__":
    unittest.main()
