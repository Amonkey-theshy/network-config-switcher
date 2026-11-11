import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import network_switcher as app


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.pending = {}
        self.time = 0.0
        self.serial = 0

        def schedule(delay, callback):
            self.serial += 1
            self.pending[self.serial] = callback
            return self.serial

        owner = SimpleNamespace(after=schedule, after_cancel=lambda timer: self.pending.pop(timer))
        self.motion = app.Transitions(owner, clock=lambda: self.time)

    def frame(self, elapsed):
        self.time += elapsed
        callbacks = list(self.pending.values())
        self.pending.clear()
        for callback in callbacks:
            callback()

    def test_delayed_frame_finishes_by_elapsed_time(self):
        values, completed = [], []
        self.motion.animate("panel", 0.24, values.append, lambda: completed.append(True))
        self.frame(0.12)
        self.assertAlmostEqual(values[-1], 0.875)
        self.frame(0.5)
        self.assertEqual(values[-1], 1)
        self.assertEqual(completed, [True])
        self.assertFalse(self.pending)

    def test_rapid_reversal_cancels_previous_completion(self):
        values, completed = [], []
        self.motion.animate("panel", 0.24, values.append, lambda: completed.append("old"))
        self.frame(0.06)
        self.motion.animate("panel", 0.24, values.append, lambda: completed.append("new"))
        self.assertEqual(len(self.pending), 1)
        self.frame(0.3)
        self.assertEqual(completed, ["new"])

    def test_close_cancels_pending_callbacks(self):
        self.motion.animate("panel", 0.24, lambda fraction: None)
        self.motion.close()
        self.assertFalse(self.pending)
        self.assertFalse(self.motion.jobs)


class NetworkRegressionTests(unittest.TestCase):
    def setUp(self):
        self.window = object.__new__(app.NetworkSwitcher)
        self.window.adapter_display_map = {"Ethernet": "Ethernet", "Ethernet 2": "Ethernet 2"}
        self.adapter = app.Adapter("Ethernet", index=12)
        self.config = dict(interface="Ethernet", mode="static", ip="192.168.1.100",
                           mask="255.255.255.0", gateway="192.168.1.1",
                           dns1="1.1.1.1", dns2="8.8.8.8")

    def test_shell_characters_remain_literal_arguments(self):
        payload = 'nic" & echo INJECTED & rem " %PATH% $(echo bad) 中文'
        _, output, _ = app.run_cmd([
            sys.executable, "-c", "import sys,json;print(json.dumps(sys.argv[1:]))", payload,
        ], check=True)
        self.assertEqual(json.loads(output), [payload])
        with self.assertRaises(TypeError):
            app.run_cmd("echo unsafe")

    def test_timeout_and_nonzero_exit_propagate(self):
        with patch.object(app.subprocess, "run", side_effect=subprocess.TimeoutExpired("mock", 1)):
            with self.assertRaises(RuntimeError):
                app.run_cmd(["mock"])
        with patch.object(app.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=b"failure")):
            with self.assertRaisesRegex(RuntimeError, "failure"):
                app.run_cmd(["mock"], check=True)

    def test_utf8_chinese_is_preserved(self):
        result = SimpleNamespace(returncode=0, stdout="无线网卡".encode("utf-8"), stderr=b"")
        with patch.object(app.subprocess, "run", return_value=result):
            self.assertEqual(app.run_cmd(["mock"])[1], "无线网卡")

    def test_contiguous_masks(self):
        for mask in ("255.255.255.0", "255.255.255.255", "0.0.0.0", "255.255.254.0"):
            self.assertTrue(app.is_valid_mask(mask), mask)
        for mask in ("255.0.255.0", "0.0.0.255", "24", "255.255.255.256"):
            self.assertFalse(app.is_valid_mask(mask), mask)
        self.assertFalse(app.is_valid_ip("².1.1.1"))

    def test_configuration_validation(self):
        self.window.validate_config(self.config)
        for changes in (
            dict(mask="255.0.255.0"), dict(mode="unknown"), dict(interface="missing"),
            dict(dns1="", dns2="8.8.8.8"), dict(ip="192.168.1.0"),
            dict(ip="192.168.1.255"), dict(gateway="192.168.2.1"),
            dict(gateway="192.168.1.100"), dict(dns2="1.1.1.1"),
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.window.validate_config({**self.config, **changes})

    def test_dhcp_and_ipv4_gateway_reading(self):
        details = dict(Name="Ethernet", Dhcp="Enabled",
                       Addresses=[dict(IPAddress="192.168.1.100", PrefixLength=24)],
                       Gateways=["192.168.1.1"], Dns=["1.1.1.1", "8.8.8.8"])
        with patch.object(app, "powershell_json", return_value=details) as read:
            config, _ = self.window.read_config(self.adapter)
            self.assertEqual(config["mode"], "dhcp")
            self.assertEqual(config["mask"], "255.255.255.0")
            self.assertEqual(config["gateway"], "192.168.1.1")
            self.assertIn("-AddressFamily IPv4", read.call_args.args[0])
        with patch.object(app, "powershell_json", return_value={**details, "Name": "Ethernet 2"}):
            with self.assertRaisesRegex(RuntimeError, "网卡已发生变化"):
                self.window.read_config(self.adapter)

    def test_disconnected_adapter_without_addresses(self):
        details = dict(Name="Ethernet", Dhcp="Enabled", Addresses=[], Gateways=[], Dns=[])
        with patch.object(app, "powershell_json", return_value=details):
            config, _ = self.window.read_config(self.adapter)
        self.assertEqual(config["mode"], "dhcp")
        self.assertEqual(config["ip"], "")

    def test_commands_use_index_and_replace_dns_directly(self):
        commands = self.window.build_commands(self.config, 12)
        self.assertTrue(all(isinstance(command, list) for command in commands))
        self.assertTrue(all("name=12" in command for command in commands))
        self.assertEqual(commands[1][3:5], ["set", "dnsservers"])
        self.assertFalse(any("delete" in command for command in commands))
        self.assertIn("index=2", commands[2])
        empty = self.window.build_commands({**self.config, "gateway": "", "dns1": "", "dns2": ""}, 12)
        self.assertIn("gateway=none", empty[0])
        self.assertIn("address=none", empty[1])
        dhcp = self.window.build_commands({**self.config, "mode": "dhcp"}, 12)
        self.assertTrue(all("source=dhcp" in command for command in dhcp))

    def test_failure_stops_and_preserves_backup(self):
        commands = self.window.build_commands(self.config, 12)
        for failure_at in (0, 1):
            with self.subTest(failure_at=failure_at), tempfile.TemporaryDirectory() as temporary:
                effects = [(0, "", "")] * failure_at + [RuntimeError("simulated failure")]
                with patch.object(app, "CONFIG_DIR", Path(temporary) / "configs"), \
                     patch.object(self.window, "read_config", return_value=(self.config, {"Name": "Ethernet"})), \
                     patch.object(app, "run_cmd", side_effect=effects) as run:
                    with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                        self.window.execute_config(self.adapter, commands)
                    self.assertEqual(run.call_count, failure_at + 1)
                backup = next((Path(temporary) / "backups").glob("*.json"))
                self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))["ip"], self.config["ip"])

    def test_backup_failure_prevents_all_network_changes(self):
        with patch.object(self.window, "read_config", return_value=(self.config, {})), \
             patch.object(app.Path, "mkdir", side_effect=PermissionError("denied")), \
             patch.object(app, "run_cmd") as run:
            with self.assertRaises(PermissionError):
                self.window.execute_config(self.adapter, self.window.build_commands(self.config, 12))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
