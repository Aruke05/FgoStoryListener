import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

import fgo_story_listener as app


class BlueStacksBridgeTests(unittest.TestCase):
    def test_blank_serial_uses_default_and_preserves_model(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps({"serial": "", "translation_model": "my-model"}),
                encoding="utf-8",
            )
            with patch.object(app, "data_dir", return_value=config_path.parent):
                config = app.RuntimeConfig.load()
        self.assertEqual(config.serial, app.DEFAULT_SERIAL)
        self.assertEqual(config.translation_model, "my-model")

    def make_bridge(self):
        config = app.RuntimeConfig(serial="127.0.0.1:5555")
        with patch.object(app.EmulatorBridge, "_find_adb", return_value=Path("adb")):
            bridge = app.EmulatorBridge(config, Mock())
        bridge.bluestacks = True
        return bridge

    def test_connect_checks_arm64_and_uses_bluestacks_su(self):
        bridge = self.make_bridge()
        with patch.object(bridge, "_run", side_effect=["connected", "device", "arm64-v8a", "0"]) as run:
            bridge.connect_adb()
        device = ["-s", "127.0.0.1:5555"]
        self.assertEqual(run.call_args_list, [
            call(["connect", "127.0.0.1:5555"], check=False),
            call([*device, "get-state"], timeout=30, check=False),
            call([*device, "shell", "getprop", "ro.product.cpu.abi"], timeout=30, check=True),
            call([*device, "shell", f"{app.BLUESTACKS_SU} -c 'id -u'"], timeout=8, check=False),
        ])

    def test_teardown_removes_its_forward_and_stops_its_server_once(self):
        bridge = self.make_bridge()
        with (
            patch.object(bridge, "_push_if_needed") as push,
            patch.object(bridge, "_bluestacks_root", side_effect=["321", ""]) as root,
            patch.object(bridge, "adb_device", side_effect=["48731", ""]) as adb,
            patch.object(app.frida, "get_device_manager") as manager,
        ):
            bridge._prepare_bluestacks()
            self.assertEqual(bridge._native_address, "127.0.0.1:48731")
            bridge.close()
            bridge.close()
        self.assertEqual(push.call_args.args[1:], (app.REMOTE_ARM64_SERVER, "755"))
        self.assertEqual(adb.call_args_list, [
            call(["forward", "tcp:0", f"tcp:{app.BLUESTACKS_SERVER_PORT}"], timeout=8),
            call(["forward", "--remove", "tcp:48731"], timeout=5, check=False),
        ])
        manager.return_value.remove_remote_device.assert_called_once_with("127.0.0.1:48731")
        self.assertEqual(root.call_count, 2)
        command = root.call_args.args[0]
        self.assertIn(f'if [ "$(readlink /proc/321/exe)" = {app.REMOTE_ARM64_SERVER} ]', command)
        self.assertIn("then kill 321", command)

    def test_attach_retries_when_forward_opens_before_server_is_ready(self):
        bridge = self.make_bridge()
        bridge._native_address = "127.0.0.1:48731"
        with (
            patch.object(app.frida, "get_device_manager") as manager,
            patch.object(bridge.detached_event, "wait", return_value=False),
        ):
            device = manager.return_value.add_remote_device.return_value
            session = Mock()
            device.attach.side_effect = [app.frida.TransportError("connection closed"), session]
            bridge.attach_hook(42)
            self.assertEqual(device.attach.call_args_list, [call(42), call(42)])
            session.create_script.return_value.load.assert_called_once_with()
