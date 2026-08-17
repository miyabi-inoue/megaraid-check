#!/usr/bin/env python3

#=======================================================================================#
# MegaRAID / SMART ステータスチェック スクリプト										#
#=======================================================================================#

"""MegaRAID / SMART daily health checker."""

import argparse
import configparser
import json
import logging
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_CONFIG = "/etc/megaraid-check/megaraid_check.conf"
VERSION = "1.0.0"
LOG = logging.getLogger("megaraid_check")

SMART_NAMES = {
	"Reallocated_Sector_Ct": "reallocated_sector_ct",
	"Reallocated_Event_Count": "reallocated_event_count",
	"Current_Pending_Sector": "current_pending_sector",
	"Offline_Uncorrectable": "offline_uncorrectable",
	"UDMA_CRC_Error_Count": "udma_crc_error_count",
	"Power_On_Hours": "power_on_hours",
	"Temperature_Celsius": "temperature_celsius",
}

#=======================================================================================#
# チェックエラー クラス																	#
#=======================================================================================#
class CheckError(RuntimeError):

	pass

#=======================================================================================#
# コマンド実行結果 クラス																#
#=======================================================================================#
class CommandResult(object):

	def __init__(self, argv, returncode, output):
		self.argv = argv
		self.returncode = returncode
		self.output = output

#=======================================================================================#
# 物理ディスク情報 クラス																#
#=======================================================================================#
class PhysicalDisk(object):

	def __init__(self, adapter=0, enclosure="", slot="", device_id="", wwn="",
				 media_error_count=0, other_error_count=0,
				 predictive_failure_count=0, firmware_state="",
				 smart_alert="", inquiry_data=""):
		self.adapter = adapter
		self.enclosure = enclosure
		self.slot = slot
		self.device_id = device_id
		self.wwn = wwn
		self.media_error_count = media_error_count
		self.other_error_count = other_error_count
		self.predictive_failure_count = predictive_failure_count
		self.firmware_state = firmware_state
		self.smart_alert = smart_alert
		self.inquiry_data = inquiry_data

	@property
	def key(self):

		return self.wwn or "A{}E{}S{}".format(self.adapter, self.enclosure, self.slot)

	@property
	def label(self):

		return (
			"Adapter {}, Enclosure {}, Slot {}, Device {}".format(
				self.adapter, self.enclosure, self.slot, self.device_id
			)
		)

	def to_dict(self):

		return {
			"adapter": self.adapter,
			"enclosure": self.enclosure,
			"slot": self.slot,
			"device_id": self.device_id,
			"wwn": self.wwn,
			"media_error_count": self.media_error_count,
			"other_error_count": self.other_error_count,
			"predictive_failure_count": self.predictive_failure_count,
			"firmware_state": self.firmware_state,
			"smart_alert": self.smart_alert,
			"inquiry_data": self.inquiry_data,
		}

#=======================================================================================#
# パラメータを解析する																	#
#=======================================================================================#
def parse_args():

	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="設定ファイル")
	parser.add_argument("--no-mail", action="store_true", help="警告メールを送信しない")
	parser.add_argument("--dump", action="store_true", help="取得した状態をJSONで標準出力へ表示する")
	parser.add_argument("--debug", action="store_true", help="デバッグログを有効にする")
	parser.add_argument("--initialize", action="store_true", help="現在の状態を基準値として保存し、警告メールを送信しない")
	parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)

	return parser.parse_args()

#=======================================================================================#
# 設定ファイルを読み込む																#
#=======================================================================================#
def load_config(path):

	cfg = configparser.ConfigParser()
	if not cfg.read(path):
		raise CheckError(f"設定ファイルを読み込めません: {path}")

	return cfg

#=======================================================================================#
# コマンドを実行する																	#
#=======================================================================================#
def run(argv, timeout=300):

	LOG.debug("run: %s", " ".join(argv))
	try:
		proc = subprocess.run(
			argv,
			stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT,
			universal_newlines=True,
			timeout=timeout,
			check=False,
		)
	except FileNotFoundError as exc:
		raise CheckError(f"コマンドが見つかりません: {argv[0]}") from exc
	except subprocess.TimeoutExpired as exc:
		raise CheckError(f"コマンドがタイムアウトしました: {' '.join(argv)}") from exc
	return CommandResult(argv, proc.returncode, proc.stdout.replace("\r", ""))

#=======================================================================================#
# 実行結果を取得する																	#
#=======================================================================================#
def require_success(result):

	if result.returncode != 0:
		raise CheckError(
			f"コマンド失敗 ({result.returncode}): {' '.join(result.argv)}\n{result.output}"
		)

	return result.output

#=======================================================================================#
# S.M.A.R.Tの実行結果を出力する															#
#=======================================================================================#
def smartctl_output(result):

	# smartctl is a bitmask. Bits 0-1 mean command/open failure.
	if result.returncode & 0b11:
		raise CheckError(
			f"smartctl実行失敗 ({result.returncode}): {' '.join(result.argv)}\n"
			f"{result.output}"
		)

	return result.output

#=======================================================================================#
# CSVデータを分解する																	#
#=======================================================================================#
def split_csv(value):

	return [v.strip() for v in value.split(",") if v.strip()]

#=======================================================================================#
# RAIDデバイスを検出する																#
#=======================================================================================#
def detect_raid_devices(cfg):

	configured = split_csv(cfg.get("raid", "devices", fallback=""))
	if configured:
		return configured

	lsblk = cfg.get("commands", "lsblk", fallback="/usr/bin/lsblk")
	pattern = cfg.get(
		"raid", "model_pattern", fallback=r"RAID.*SAS|MegaRAID|MR\d+"
	)
	try:
		model_re = re.compile(pattern, re.IGNORECASE)
	except re.error as exc:
		raise CheckError(f"model_patternの正規表現が不正です: {exc}") from exc

	text = require_success(run([lsblk, "--json", "--output", "NAME,TYPE,MODEL"]))
	try:
		payload = json.loads(text)
	except json.JSONDecodeError as exc:
		raise CheckError(f"lsblk JSONの解析に失敗しました: {exc}") from exc

	devices = []
	for item in payload.get("blockdevices", []):
		if item.get("type") != "disk":
			continue
		name = str(item.get("name") or "").strip()
		model = str(item.get("model") or "").strip()
		if name and model_re.search(model):
			devices.append(f"/dev/{name}")

	if not devices:
		raise CheckError(
			"MegaRAID論理デバイスを自動検出できません。"
			"設定ファイルの [raid] devices に明示してください。"
		)
	return devices

#=======================================================================================#
# 数値に変換する																		#
#=======================================================================================#
def parse_int(value):

	m = re.search(r"-?\d+", value)
	return int(m.group()) if m else 0

#=======================================================================================#
# アダプタの警告を解析する																#
#=======================================================================================#
def parse_adapter_alerts(text):

	alerts = []
	for line in text.splitlines():
		s = line.strip()
		if s.startswith(("Degraded", "Failed Disks")) and parse_int(s.split(":")[-1]) > 0:
			alerts.append(f"Adapter error: {s}")
	return alerts

#=======================================================================================#
# 論理ディスクの警告を解析する															#
#=======================================================================================#
def parse_logical_alerts(text):

	alerts = []
	vd = "Unknown virtual drive"
	for line in text.splitlines():
		s = line.strip()
		if s.startswith("Virtual Drive:"):
			vd = s
		elif s.startswith("State") and ":" in s:
			state = s.split(":", 1)[1].strip()
			if state != "Optimal":
				alerts.append(f"Logical drive error: {vd} / State: {state}")
		elif s.startswith("Bad Blocks Exist") and ":" in s:
			value = s.split(":", 1)[1].strip()
			if value != "No":
				alerts.append(f"Logical drive bad blocks: {vd} / {s}")
	return alerts

#=======================================================================================#
# 物理ディスクの警告を解析する															#
#=======================================================================================#
def parse_physical_disks(text):

	disks = []
	current = None
	adapter = 0

	for line in text.splitlines():
		s = line.strip()
		m_adapter = re.match(r"Adapter\s+#?(\d+)", s)
		if m_adapter:
			adapter = int(m_adapter.group(1))
			continue
		if s.startswith("Enclosure Device ID:"):
			if current and current.slot:
				disks.append(current)
			current = PhysicalDisk(adapter=adapter, enclosure=s.split(":", 1)[1].strip())
			continue
		if current is None:
			continue
		mapping = {
			"Slot Number:": "slot",
			"Device Id:": "device_id",
			"WWN:": "wwn",
			"Firmware state:": "firmware_state",
			"Drive has flagged a S.M.A.R.T alert:": "smart_alert",
			"Inquiry Data:": "inquiry_data",
		}
		matched = False
		for prefix, attr in mapping.items():
			if s.startswith(prefix):
				setattr(current, attr, s.split(":", 1)[1].strip())
				matched = True
				break
		if matched:
			continue
		if s.startswith("Media Error Count:"):
			current.media_error_count = parse_int(s.split(":", 1)[1])
		elif s.startswith("Other Error Count:"):
			current.other_error_count = parse_int(s.split(":", 1)[1])
		elif s.startswith("Predictive Failure Count:"):
			current.predictive_failure_count = parse_int(s.split(":", 1)[1])
	if current and current.slot:
		disks.append(current)
	return disks

#=======================================================================================#
# S.M.A.R.T情報を解析する																#
#=======================================================================================#
def parse_smart(text):

	data = {
		"health": "",
		"device_model": "",
		"serial_number": "",
		"lu_wwn": "",
		"firmware_version": "",
	}
	data.update({v: 0 for v in SMART_NAMES.values()})
	for line in text.splitlines():
		s = line.strip()
		if "SMART overall-health self-assessment test result:" in s:
			data["health"] = s.split(":", 1)[1].strip()
		elif s.startswith("SMART Health Status:"):
			data["health"] = s.split(":", 1)[1].strip()
		elif s.startswith("Device Model:"):
			data["device_model"] = s.split(":", 1)[1].strip()
		elif s.startswith("Serial Number:"):
			data["serial_number"] = s.split(":", 1)[1].strip()
		elif s.startswith("LU WWN Device Id:"):
			data["lu_wwn"] = re.sub(r"[^0-9a-fA-F]", "", s.split(":", 1)[1]).lower()
		elif s.startswith("Firmware Version:"):
			data["firmware_version"] = s.split(":", 1)[1].strip()
		parts = s.split()
		if len(parts) >= 10 and parts[0].isdigit() and parts[1] in SMART_NAMES:
			data[SMART_NAMES[parts[1]]] = parse_int(parts[9])
	return data

#=======================================================================================#
# ディスクからS.M.A.R.T情報を取得する													#
#=======================================================================================#
def get_smart_for_disk(
	smartctl, disk, raid_devices
):

	failures = []
	expected_wwn = re.sub(r"[^0-9a-fA-F]", "", disk.wwn or "").lower()
	for dev in raid_devices:
		result = run([smartctl, "-a", "-d", f"megaraid,{disk.device_id}", dev])
		try:
			text = smartctl_output(result)
			smart = parse_smart(text)
			actual_wwn = smart.get("lu_wwn", "")

			# 複数MegaRAIDアダプタ環境ではDevice IDが重複することがあるため、
			# smartctlが返すWWNとMegaCliのWWNが取得できる場合は照合する。
			if expected_wwn and actual_wwn and expected_wwn != actual_wwn:
				failures.append(
					"{}: WWN不一致 (expected={}, actual={})".format(
						dev, expected_wwn, actual_wwn
					)
				)
				continue

			return smart, text, dev
		except CheckError as exc:
			failures.append(str(exc))
	raise CheckError(f"{disk.label}: SMARTを取得できません\n" + "\n".join(failures))

#=======================================================================================#
# 状態を読み込む																		#
#=======================================================================================#
def load_state(path):

	if not path.exists():
		return {"disks": {}, "event_seq": {}}
	try:
		with path.open(encoding="utf-8") as fp:
			data = json.load(fp)
	except (OSError, json.JSONDecodeError) as exc:
		raise CheckError(f"状態ファイルを読み込めません: {path}: {exc}") from exc
	data.setdefault("disks", {})
	data.setdefault("event_seq", {})
	return data

#=======================================================================================#
# 状態を保存する																		#
#=======================================================================================#
def save_state(path, state):

	path.parent.mkdir(parents=True, exist_ok=True)
	fd, temp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent), text=True)
	try:
		with os.fdopen(fd, "w", encoding="utf-8") as fp:
			json.dump(state, fp, ensure_ascii=False, indent=2, sort_keys=True)
			fp.write("\n")
			fp.flush()
			os.fsync(fp.fileno())
		os.replace(temp, path)
	except Exception:
		try:
			os.unlink(temp)
		except OSError:
			pass
		raise

#=======================================================================================#
# 前回の値を取得する																	#
#=======================================================================================#
def old_value(old, key, current):

	previous = int(old.get(key, 0) or 0)
	return 0 if current < previous else previous

#=======================================================================================#
# 前回の値と比較する																	#
#=======================================================================================#
def compare_increase(
	alerts, label, name, old, key, current
):

	previous = old_value(old, key, current)
	if current > previous:
		alerts.append(f"{label}: {name} increased {previous} -> {current}")

#=======================================================================================#
# 新しいイベントの警告を解析する														#
#=======================================================================================#
def parse_new_event_alerts(
	text, adapter, previous_seq, first_run_notify
):

	blocks = re.split(r"\n(?=seqNum:)", text.strip()) if text.strip() else []
	alerts = []
	max_seq = previous_seq
	keywords = re.compile(
		r"medium error|uncorrectable|predictive|failed|missing|degraded|"
		r"removed|offline|punctur|bad block",
		re.IGNORECASE,
	)
	for block in blocks:
		seq_match = re.search(r"seqNum:\s*0x([0-9a-fA-F]+)", block)
		desc_match = re.search(r"Event Description:\s*(.+)", block)
		if not seq_match:
			continue
		seq = int(seq_match.group(1), 16)
		max_seq = max(max_seq, seq)
		is_new = seq > previous_seq
		if previous_seq == 0 and not first_run_notify:
			is_new = False
		description = desc_match.group(1).strip() if desc_match else "Unknown event"
		if is_new and keywords.search(description):
			alerts.append(f"Adapter {adapter} event 0x{seq:08x}: {description}")
	return alerts, max_seq

#=======================================================================================#
# イベントログを取得する																#
#=======================================================================================#
def get_event_log(megacli, adapter, history):

	with tempfile.NamedTemporaryFile(prefix="megaraid-event-", delete=False) as fp:
		path = fp.name
	try:
		result = run(
			[megacli, "-AdpEventLog", "-GetLatest", str(history), "-f", path, f"-a{adapter}"],
			timeout=600,
		)
		require_success(result)
		return Path(path).read_text(encoding="utf-8", errors="replace").replace("\r", "")
	finally:
		try:
			os.unlink(path)
		except OSError:
			pass

#=======================================================================================#
# アダプタを検出する																	#
#=======================================================================================#

def detect_adapters(physical_disks):

	return sorted({disk.adapter for disk in physical_disks}) or [0]

#=======================================================================================#
# メールを送信する																		#
#=======================================================================================#
def send_mail(command, recipient, subject, body):

	result = subprocess.run(
		[command, "-s", subject, recipient],
		input=body,
		universal_newlines=True,
		stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT,
		check=False,
	)
	if result.returncode != 0:
		raise CheckError(f"メール送信に失敗しました: {result.stdout}")

#=======================================================================================#
# メイン																				#
#=======================================================================================#
def main():

	# 管理者権限で実行されていない場合は何もしない
	if os.geteuid() != 0:
		print("管理者権限で実行する必要があります。", file=sys.stderr)
		return 1

	# 引数を解析する
	args = parse_args()

	# ロガーを設定する
	logging.basicConfig(
		level=logging.DEBUG if args.debug else logging.WARNING,
		format="%(asctime)s %(levelname)s %(message)s",
	)

	try:
		cfg = load_config(args.config)
		megacli = cfg.get("commands", "megacli", fallback="/usr/bin/MegaCli")
		smartctl = cfg.get("commands", "smartctl", fallback="/usr/sbin/smartctl")
		mail_cmd = cfg.get("commands", "mail", fallback="/usr/bin/mail")
		raid_devices = detect_raid_devices(cfg)
		LOG.info("RAID devices: %s", ", ".join(raid_devices))

		state_path = Path(
			cfg.get("paths", "state_file", fallback="/var/lib/megaraid-check/state.json")
		)
		allowed_states = {
			s.strip()
			for s in cfg.get(
				"raid", "allowed_firmware_states", fallback="Online, Spun Up|Hotspare, Spun Up"
			).split("|")
			if s.strip()
		}
		event_history = cfg.getint("eventlog", "history", fallback=500)
		first_run_notify = cfg.getboolean("eventlog", "notify_on_first_run", fallback=False)
		recipient = cfg.get("mail", "address", fallback="root")
		hostname = socket.gethostname().split(".", 1)[0]

		adapter_text = require_success(run([megacli, "-AdpAllInfo", "-aALL"]))
		logical_text = require_success(run([megacli, "-LDInfo", "-Lall", "-aALL"]))
		physical_text = require_success(run([megacli, "-PDList", "-aALL"]))
		disks = parse_physical_disks(physical_text)
		if not disks:
			raise CheckError("MegaCliの物理ディスク情報を解析できませんでした")

		old_state = load_state(state_path)
		new_state = {
			"hostname": hostname,
			"raid_devices": raid_devices,
			"disks": {},
			"event_seq": dict(old_state.get("event_seq", {})),
		}
		alerts = parse_adapter_alerts(adapter_text) + parse_logical_alerts(logical_text)
		smart_reports = []

		for disk in disks:
			old = old_state.get("disks", {}).get(disk.key, {})
			if disk.firmware_state not in allowed_states:
				alerts.append(f"{disk.label}: Firmware state = {disk.firmware_state}")
			if disk.smart_alert == "Yes":
				alerts.append(f"{disk.label}: MegaRAID S.M.A.R.T alert = Yes")

			for key, title, current in (
				("media_error_count", "Media Error Count", disk.media_error_count),
				("other_error_count", "Other Error Count", disk.other_error_count),
				("predictive_failure_count", "Predictive Failure Count", disk.predictive_failure_count),
			):
				compare_increase(alerts, disk.label, title, old, key, current)

			smart, smart_text, used_dev = get_smart_for_disk(smartctl, disk, raid_devices)
			identity = "Model={0}, Serial={1}, WWN={2}".format(
				smart.get("device_model", "unknown"),
				smart.get("serial_number", "unknown"),
				disk.wwn or "unknown",
			)
			alert_label = "{0} [{1}]".format(disk.label, identity)

			health = str(smart.get("health", "")).strip()
			if health and health not in {"PASSED", "OK"}:
				alerts.append(f"{alert_label}: SMART health = {health}")

			for key, title in (
				("reallocated_sector_ct", "Reallocated_Sector_Ct"),
				("reallocated_event_count", "Reallocated_Event_Count"),
				("current_pending_sector", "Current_Pending_Sector"),
				("offline_uncorrectable", "Offline_Uncorrectable"),
				("udma_crc_error_count", "UDMA_CRC_Error_Count"),
			):
				compare_increase(alerts, alert_label, title, old, key, int(smart[key]))

			if int(smart["current_pending_sector"]) > 0:
				alerts.append(
					f"{alert_label}: Current_Pending_Sector = {smart['current_pending_sector']}"
				)
			if int(smart["offline_uncorrectable"]) > 0:
				alerts.append(
					f"{alert_label}: Offline_Uncorrectable = {smart['offline_uncorrectable']}"
				)

			disk_state = disk.to_dict()
			disk_state.update(smart)
			disk_state["smart_device"] = used_dev
			new_state["disks"][disk.key] = disk_state
			smart_reports.append(
				f"===== {disk.label} via {used_dev} =====\nWWN: {disk.wwn}\n"
				f"Inquiry: {disk.inquiry_data}\n{smart_text.rstrip()}"
			)

		for adapter in detect_adapters(disks):
			event_text = get_event_log(megacli, adapter, event_history)
			previous_seq = int(old_state.get("event_seq", {}).get(str(adapter), 0) or 0)
			event_alerts, max_seq = parse_new_event_alerts(
				event_text, adapter, previous_seq, first_run_notify
			)
			alerts.extend(event_alerts)
			new_state["event_seq"][str(adapter)] = max_seq

		if args.dump:
			print(json.dumps(new_state, ensure_ascii=False, indent=2, sort_keys=True))

		# 初回導入時や意図的に基準値を取り直す場合に使用する。
		# 現在値をstateへ保存するだけで、検出済みの警告は通知しない。
		if args.initialize:
			save_state(state_path, new_state)
			print("状態を初期化しました: {}".format(state_path))
			return 0

		if alerts:
			body = "\n".join(
				[
					f"MegaRAID warning on {hostname}",
					"",
					"===== Detected alerts =====",
					*[f"- {a}" for a in alerts],
					"",
					f"===== Auto-detected RAID devices =====\n{chr(10).join(raid_devices)}",
					"",
					"===== Logical drives =====",
					logical_text.rstrip(),
					"",
					"===== Physical drives =====",
					physical_text.rstrip(),
					"",
					"===== SMART reports =====",
					"\n\n".join(smart_reports),
					"",
				]
			)
			if not args.no_mail:
				# メール送信に失敗した場合はstateを更新しない。
				# これにより次回実行時に同じ一過性警告を再通知できる。
				send_mail(mail_cmd, recipient, f"MegaRAID warning on {hostname}", body)

			save_state(state_path, new_state)
			return 1

		save_state(state_path, new_state)
		return 0
	except CheckError as exc:
		LOG.error("%s", exc)
		return 2
	except Exception:
		LOG.exception("想定外のエラー")
		return 3


if __name__ == "__main__":
	raise SystemExit(main())
