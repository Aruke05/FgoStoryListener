import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import fgo_story_listener as app


class CodexDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"PATH": "", "LOCALAPPDATA": str(self.root / "local")})
        env.start()
        self.addCleanup(env.stop)

    def cli(self, directory, name="codex.exe"):
        path = self.root / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("test placeholder", encoding="utf-8")
        path.chmod(0o755)
        return path

    def run_version(self, versions):
        def run(command, **kwargs):
            value = versions[str(command[0])]
            if isinstance(value, Exception):
                raise value
            return subprocess.CompletedProcess(command, 0, "codex-cli " + value, "")
        return patch.object(app.subprocess, "run", side_effect=run)

    def test_newest_cli_wins_instead_of_first_path_entry(self):
        old = self.cli("old")
        new = self.cli("new")
        os.environ["PATH"] = os.pathsep.join([str(old.parent), str(new.parent)])
        with self.run_version({str(old): "0.147.0", str(new): "0.154.0-alpha.6.2"}):
            self.assertEqual(app._codex_command(), [str(new)])

    @unittest.skipUnless(os.name == "nt", "Windows desktop installation")
    def test_desktop_install_is_discovered_without_its_bin_on_path(self):
        old = self.cli("old")
        new = self.cli("local/OpenAI/Codex/bin/arbitrary-install-id")
        os.environ["PATH"] = str(old.parent)
        with self.run_version({str(old): "0.147.0", str(new): "0.154.0-alpha.6.2"}):
            self.assertEqual(app._codex_command(), [str(new)])

    def test_explicit_path_is_not_replaced_or_probed(self):
        explicit = self.cli("explicit")
        with patch.object(app.subprocess, "run", side_effect=AssertionError("must not probe explicit choice")):
            self.assertEqual(app._codex_command(str(explicit)), [str(explicit)])

    @unittest.skipUnless(os.name == "nt", "Windows npm launcher")
    def test_newer_npm_cli_can_win_and_launchers_are_deduplicated(self):
        exe = self.cli("desktop")
        cmd = self.cli("npm", "codex.cmd")
        self.cli("npm", "codex.ps1")
        node = self.cli("npm", "node.exe")
        script = self.cli("npm/node_modules/@openai/codex/bin", "codex.js")
        os.environ["PATH"] = os.pathsep.join([str(exe.parent), str(cmd.parent)])
        with self.run_version({str(exe): "0.154.0", str(node): "0.155.0"}) as run:
            self.assertEqual(app._codex_command(), [str(node), str(script)])
            self.assertEqual(run.call_count, 2)

    def test_broken_candidate_is_skipped(self):
        broken = self.cli("broken")
        good = self.cli("good")
        os.environ["PATH"] = os.pathsep.join([str(broken.parent), str(good.parent)])
        with self.run_version({str(broken): subprocess.TimeoutExpired("codex", 3), str(good): "0.154.0"}):
            self.assertEqual(app._codex_command(), [str(good)])

    def test_release_beats_prerelease_and_numeric_versions_sort_correctly(self):
        paths = [self.cli(str(i)) for i in range(3)]
        os.environ["PATH"] = os.pathsep.join(str(p.parent) for p in paths)
        with self.run_version(dict(zip(map(str, paths), ["0.99.0", "0.154.0-alpha.10", "0.154.0"]))):
            self.assertEqual(app._codex_command(), [str(paths[2])])

    def test_changed_binary_invalidates_probe_cache(self):
        first = self.cli("first")
        second = self.cli("second")
        os.environ["PATH"] = os.pathsep.join([str(first.parent), str(second.parent)])
        with self.run_version({str(first): "0.147.0", str(second): "0.154.0"}) as run:
            self.assertEqual(app._codex_command(), [str(second)])
            self.assertEqual(app._codex_command(), [str(second)])
            self.assertEqual(run.call_count, 2, "Unchanged binaries should not spawn a probe per translation")
        first.write_text("updated CLI with a different size", encoding="utf-8")
        with self.run_version({str(first): "0.155.0", str(second): "0.154.0"}):
            self.assertEqual(app._codex_command(), [str(first)])

    def test_no_working_cli_reports_failure_instead_of_guessing(self):
        bad = self.cli("bad")
        os.environ["PATH"] = str(bad.parent)
        with self.run_version({str(bad): "not a version"}):
            with self.assertRaisesRegex(RuntimeError, "Codex"):
                app._codex_command()

    @unittest.skipUnless(os.name == "nt", "Windows npm platform package")
    def test_npm_native_binary_update_invalidates_version_cache(self):
        exe = self.cli("desktop")
        cmd = self.cli("npm", "codex.cmd")
        node = self.cli("npm", "node.exe")
        script = self.cli("npm/node_modules/@openai/codex/bin", "codex.js")
        native = self.cli("npm/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin")
        os.environ["PATH"] = os.pathsep.join([str(exe.parent), str(cmd.parent)])
        with self.run_version({str(exe): "0.154.0", str(node): "0.147.0"}):
            self.assertEqual(app._codex_command(), [str(exe)])
        native.write_text("updated native CLI", encoding="utf-8")
        with self.run_version({str(exe): "0.154.0", str(node): "0.155.0"}):
            self.assertEqual(app._codex_command(), [str(node), str(script)])


if __name__ == "__main__":
    unittest.main()
