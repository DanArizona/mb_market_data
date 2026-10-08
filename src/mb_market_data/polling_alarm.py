"""Persistent operator alarm; independent of journal reads and API polling."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def alarm_payload(snapshot: dict, conditions: dict, incidents: list, *, window_active: bool,
                  status_path: Path, poller_windows: str) -> dict:
    urgent, notices = {}, {}
    for key, detail in conditions.items():
        historical = key.endswith(":coverage") or (not window_active and key != "journal:unavailable")
        (notices if historical else urgent)[key] = detail
    # A replacement PID is not recovery. Keep its old failure sounding until
    # a new committed slot resolves that incident, or the polling window ends.
    for item in incidents:
        channel = item["key"].split(":", 1)[0]
        info = snapshot["channels"].get(channel, {})
        if (window_active and item["resolved_at_utc"] is None
                and info.get("state") == "AWAITING_COMMIT"
                and not item["key"].endswith((":coverage", ":request_error"))):
            urgent[item["key"]] = "Restart is waiting for its first committed sample; recovery is not confirmed."
    lines = []
    for key, detail in urgent.items():
        lines.append(f"{key.split(':', 1)[0].upper()}: {detail}")
    for channel, info in snapshot["channels"].items():
        lines.append(f"{channel.upper()}: {info['state']} — {info['missing_slot_count']} missing slots")
        if (info["state"] in {"FAILED", "STOPPED_EARLY", "PROCESS_MISSING", "OVERDUE", "NOT_STARTED"}
                and any(key.startswith(channel + ":") for key in urgent)):
            launcher = snapshot.get("restart_launchers", {}).get(channel)
            if launcher:
                lines.append(f'After the poller exits, run in prod-poll-{channel}:\ncall "{launcher}"')
    if notices:
        lines.append("\nCoverage and other notices:")
        lines.extend(f"{key.split(':', 1)[0].upper()}: {detail}" for key, detail in notices.items())
    lines.append(f"\nCheck {poller_windows}. Leave healthy channels running.")
    return {"urgent": urgent, "notices": notices, "text": "\n\n".join(lines),
            "observed_at_utc": snapshot["observed_at_utc"], "status_path": str(status_path)}


class AlarmPolicy:
    """Latched notice, repeating sound, and bounded operator silence."""
    def __init__(self, *, repeat_seconds: float = 10, silence_seconds: float = 300):
        self.repeat_seconds = repeat_seconds
        self.silence_seconds = silence_seconds
        self.current = {"urgent": {}, "notices": {}, "text": "", "observed_at_utc": "", "status_path": ""}
        self.silenced_until = 0.0
        self.next_sound = 0.0
        self.visible = False
        self.sound_count = 0
        self.dismissed_notices = frozenset()
        self.unacknowledged = {}

    def update(self, payload: dict, now: float) -> None:
        old_keys, new_keys = set(self.current["urgent"]), set(payload["urgent"])
        if new_keys - old_keys:
            self.silenced_until = 0.0
            self.next_sound = now
        if new_keys or set(payload["notices"]) - self.dismissed_notices:
            self.visible = True
        for key, detail in payload["urgent"].items():
            self.unacknowledged.setdefault(key, {"detail": detail, "detected_at": payload["observed_at_utc"]})
        if not new_keys:
            self.silenced_until = 0.0
        self.current = payload

    def silence(self, now: float) -> None:
        if self.current["urgent"]:
            self.silenced_until = now + self.silence_seconds

    def dismiss(self) -> bool:
        if self.current["urgent"]:
            return False
        self.visible = False
        self.dismissed_notices = frozenset(self.current["notices"])
        self.unacknowledged.clear()
        return True

    def sound_due(self, now: float) -> bool:
        if not self.current["urgent"] or now < self.silenced_until or now < self.next_sound:
            return False
        self.next_sound = now + self.repeat_seconds
        self.sound_count += 1
        return True

    def view(self, now: float) -> dict:
        value = dict(self.current)
        value.update(visible=self.visible, urgent_active=bool(value["urgent"]),
                     silence_remaining=max(0, int(self.silenced_until - now + 0.999)),
                     sound_count=self.sound_count)
        if not value["urgent"] and self.unacknowledged:
            lines = ["\n\nEarlier alerts — retained until you acknowledge this notice:"]
            for key, record in self.unacknowledged.items():
                when = record["detected_at"]
                if when:
                    when = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
                lines.append(f"{key.split(':', 1)[0].upper()} at {when or 'an earlier check'}: {record['detail']}")
            value["text"] += "\n\n".join(lines)
        return value


def check_desktop() -> None:
    """Verify the supplied Python Tk runtime without sounding an alarm."""
    try:
        import tkinter as tk
        root = tk.Tk()
        try:
            root.withdraw()
            root.update_idletasks()
        finally:
            root.destroy()
    except Exception as error:
        raise ValueError(f"Python's Tk desktop runtime is unavailable: {error}") from error


def sound_alarm() -> None:
    import winsound
    for frequency in (1050, 1500, 1050):
        winsound.Beep(frequency, 250)


class DesktopAlarm:
    def __init__(self, *, sound=sound_alarm):
        self.policy = AlarmPolicy()
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.gui_error = None
        self.sound = sound
        self.threads = []

    def publish(self, payload: dict) -> None:
        with self.lock:
            self.policy.update(payload, time.monotonic())

    def view(self) -> dict:
        with self.lock:
            return self.policy.view(time.monotonic())

    def silence(self) -> None:
        with self.lock:
            self.policy.silence(time.monotonic())

    def dismiss(self) -> None:
        with self.lock:
            if not self.policy.dismiss():
                self.policy.silence(time.monotonic())

    def start(self) -> None:
        self.threads = [threading.Thread(target=self._desktop, daemon=True),
                        threading.Thread(target=self._sound, daemon=True)]
        for thread in self.threads:
            thread.start()
        if not self.ready.wait(5) or self.gui_error:
            self.close()
            raise ValueError("Persistent alarm window could not start: " + str(self.gui_error or "timeout"))

    def close(self) -> None:
        self.stopping.set()
        for thread in self.threads:
            thread.join(timeout=1.2)

    def _sound(self) -> None:
        while not self.stopping.is_set():
            with self.lock:
                due = self.policy.sound_due(time.monotonic())
            if due:
                try:
                    self.sound()
                except Exception as error:
                    print(f"ALARM SOUND UNAVAILABLE: {error}. Check the red alarm window.", flush=True)
            self.stopping.wait(0.1)

    def _desktop(self) -> None:
        # All Tk calls and objects live in this thread. Sound has its own
        # worker, so a slow journal/CIM read cannot suspend an active alarm.
        root = None
        refresh = None
        try:
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            root.title("Polling health alarm")
            root.geometry("860x650")
            root.minsize(620, 480)
            root.columnconfigure(0, weight=1)
            root.rowconfigure(3, weight=1)
            root.attributes("-topmost", True)
            root.protocol("WM_DELETE_WINDOW", self.dismiss)
            heading = tk.Label(root, font=("Segoe UI", 22, "bold"), fg="white", pady=16)
            heading.grid(row=0, column=0, sticky="ew")
            stamp = tk.Label(root, font=("Segoe UI", 11), pady=8)
            stamp.grid(row=1, column=0, sticky="ew")
            area = tk.Frame(root)
            # Only the scrollable text receives spare height. Keep controls
            # visible above it even with larger Windows fonts or long alerts.
            area.grid(row=3, column=0, sticky="nsew", padx=16, pady=(0, 12))
            scrollbar = tk.Scrollbar(area)
            scrollbar.pack(side="right", fill="y")
            body = tk.Text(area, font=("Segoe UI", 12), wrap="word", bg="#fff8ef",
                           relief="flat", padx=12, pady=12, height=1, width=1,
                           yscrollcommand=scrollbar.set)
            body.pack(fill="both", expand=True)
            scrollbar.config(command=body.yview)
            controls = tk.Frame(root, pady=8)
            controls.grid(row=2, column=0, sticky="ew")
            controls.columnconfigure(0, weight=1)
            mute = tk.Button(controls, text="Silence for 5 minutes", command=self.silence,
                             font=("Segoe UI", 11), padx=12, pady=8)
            mute.grid(row=0, column=0, sticky="ew", padx=16, pady=3)
            dismiss = tk.Button(controls, text="Dismiss notice", command=self.dismiss,
                                font=("Segoe UI", 11), padx=12, pady=8)
            dismiss.grid(row=1, column=0, sticky="ew", padx=16, pady=3)
            def open_status():
                path = self.view()["status_path"]
                if path and Path(path).exists():
                    os.startfile(path)
            tk.Button(controls, text="Open current status", command=open_status,
                      font=("Segoe UI", 11), padx=12, pady=8).grid(
                          row=2, column=0, sticky="ew", padx=16, pady=3)
            shown, signature, last_sound = False, None, 0
            def refresh():
                nonlocal shown, signature, last_sound
                if self.stopping.is_set():
                    root.destroy()
                    return
                view = self.view()
                if view["visible"] and not shown:
                    root.deiconify()
                    root.lift()
                    shown = True
                elif not view["visible"] and shown:
                    root.withdraw()
                    shown = False
                urgent = view["urgent_active"]
                title = "POLLING ALARM — ACTION NEEDED" if urgent else "Polling notice — check coverage"
                root.title(("!!! " if urgent and int(time.monotonic()) % 2 else "") + title)
                heading.config(text=title, bg="#b91c1c" if urgent else "#9a6700")
                when = view["observed_at_utc"]
                if when:
                    when = datetime.fromisoformat(when.replace("Z", "+00:00")).astimezone(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
                if urgent:
                    sound = (f"Sound silenced for {view['silence_remaining']} seconds" if view["silence_remaining"]
                             else "Three tones repeat every 10 seconds")
                else:
                    sound = "Repeating sound is off. Review and acknowledge this notice."
                stamp.config(text=f"Latest check: {when or 'waiting'}  |  {sound}")
                mute.config(state="normal" if urgent else "disabled")
                dismiss.config(state="disabled" if urgent else "normal")
                if view["text"] != signature:
                    body.config(state="normal")
                    body.delete("1.0", "end")
                    body.insert("1.0", view["text"])
                    body.config(state="disabled")
                    signature = view["text"]
                if view["sound_count"] != last_sound:
                    root.deiconify()
                    root.lift()
                    last_sound = view["sound_count"]
                root.after(200, refresh)
            root.after(0, refresh)
            self.ready.set()
            root.mainloop()
        except Exception as error:
            self.gui_error = f"{type(error).__name__}: {error}"
            print("PERSISTENT ALARM WINDOW UNAVAILABLE: " + self.gui_error, flush=True)
            self.ready.set()
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
            refresh = None  # break the recursive callback closure in its owning thread
