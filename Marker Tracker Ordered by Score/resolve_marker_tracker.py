r"""
Resolve Marker Tracker
-----------------------
Reads all markers on the CURRENT TIMELINE in an already-running DaVinci Resolve
session, extracts a "Score" from each marker's name (e.g. "Enemy Chaining
Abilities (Score 9)"), lists them ranked highest-to-lowest score, and jumps
the playhead to a marker when you click it in the list.

HOW TO RUN
==========
This is an EXTERNAL script (it opens its own Tkinter window), not a
Fusion-console "Utility" script. Run it from a normal terminal while
Resolve is open with your project/timeline loaded.

1) Make sure DaVinci Resolve is running and the timeline with your markers
   is the *current* timeline.

2) Set the environment variables Resolve's scripting API needs.

   macOS (Terminal):
     export RESOLVE_SCRIPT_API="/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"
     export RESOLVE_SCRIPT_LIB="/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"
     export PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules/"

   Windows (PowerShell):
     $env:RESOLVE_SCRIPT_API="C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting"
     $env:RESOLVE_SCRIPT_LIB="C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
     $env:PYTHONPATH="$env:PYTHONPATH;$env:RESOLVE_SCRIPT_API\Modules\"

   Linux:
     export RESOLVE_SCRIPT_API="/opt/resolve/Developer/Scripting"
     export RESOLVE_SCRIPT_LIB="/opt/resolve/libs/Fusion/fusionscript.so"
     export PYTHONPATH="$PYTHONPATH:$RESOLVE_SCRIPT_API/Modules/"

3) Run it with the SAME Python version Resolve ships bindings for
   (usually system Python 3 on Windows/Linux, Resolve's bundled Python
   3.6-3.10 on macOS — check Resolve's docs if you get an import error):

     python3 resolve_marker_tracker.py

No third-party packages are required — the GUI uses Tkinter, which ships
with standard Python, so it won't collide with the specific PySide/Qt
version bundled inside Resolve.

WHAT IT ASSUMES ABOUT YOUR MARKERS
===================================
Your EDL's marker names end with "(Score N)", e.g.:
    "Enemy Chaining Abilities (Score 9)"
That's what gets imported into Resolve as the marker's Name/Note text when
you import the EDL with markers. This script parses that "(Score N)" tag
with a regex; if a marker doesn't match, it's treated as score 0 and still
listed (so nothing silently disappears).
"""

import os
import sys
import re
import tkinter as tk
from tkinter import ttk, messagebox


# ----------------------------------------------------------------------
# Connect to Resolve
# ----------------------------------------------------------------------
def connect_to_resolve():
    """Import DaVinciResolveScript and return the Resolve app object."""
    try:
        import DaVinciResolveScript as dvr_script  # noqa: F401
    except ImportError:
        # Fall back to manually locating the module if PYTHONPATH wasn't set.
        if sys.platform.startswith("darwin"):
            default_path = ("/Library/Application Support/Blackmagic Design/"
                             "DaVinci Resolve/Developer/Scripting/Modules/")
        elif sys.platform.startswith("win"):
            default_path = os.path.join(
                os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                "Blackmagic Design", "DaVinci Resolve", "Support",
                "Developer", "Scripting", "Modules")
        else:
            default_path = "/opt/resolve/Developer/Scripting/Modules/"

        if default_path not in sys.path:
            sys.path.append(default_path)
        import DaVinciResolveScript as dvr_script

    resolve = dvr_script.scriptapp("Resolve")
    if resolve is None:
        raise RuntimeError(
            "Could not connect to DaVinci Resolve. Make sure Resolve is "
            "running and the RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB / "
            "PYTHONPATH environment variables are set (see the docstring "
            "at the top of this file)."
        )
    return resolve


SCORE_RE = re.compile(r"\(Score\s*(-?\d+)\)", re.IGNORECASE)

# Rough color mapping so the list visually matches Resolve's marker colors.
RESOLVE_COLOR_HEX = {
    "Blue": "#4aa3e0",
    "Cyan": "#5fd0d0",
    "Green": "#6fbf5f",
    "Yellow": "#e0d34a",
    "Red": "#e05a5a",
    "Pink": "#e07fbf",
    "Purple": "#a37fe0",
    "Fuchsia": "#c04ac0",
    "Rose": "#e0a3a3",
    "Lavender": "#c9c3f0",
    "Sky": "#8fd0f0",
    "Mint": "#a3e0c3",
    "Lemon": "#f0f0a3",
    "Sand": "#e0cba3",
    "Cocoa": "#a3826f",
    "Cream": "#f0e6c9",
}


def frames_to_timecode(frame_count, fps):
    """Convert an absolute frame number into an HH:MM:SS:FF timecode
    string. Non-drop-frame only (matches the source EDL's FCM)."""
    fps_int = int(round(fps))
    frame_count = int(frame_count)
    hh = frame_count // (3600 * fps_int)
    mm = (frame_count % (3600 * fps_int)) // (60 * fps_int)
    ss = (frame_count % (60 * fps_int)) // fps_int
    ff = frame_count % fps_int
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


class MarkerTrackerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Resolve Marker Tracker — ranked by score")
        self.root.geometry("820x520")
        self.root.minsize(600, 360)

        self.resolve = None
        self.project = None
        self.timeline = None
        self.fps = 30.0
        self.marker_rows = []  # list of dicts: score, name, color, tc, frame_id

        self._build_ui()
        self._connect_and_load()

    # ------------------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        self.status_var = tk.StringVar(value="Connecting to DaVinci Resolve...")
        ttk.Label(top, textvariable=self.status_var).pack(side="left")

        ttk.Button(top, text="Refresh Markers", command=self._connect_and_load
                   ).pack(side="right")

        columns = ("score", "name", "color", "timecode")
        self.tree = ttk.Treeview(self.root, columns=columns, show="headings",
                                  selectmode="browse")
        self.tree.heading("score", text="Score")
        self.tree.heading("name", text="Marker Name")
        self.tree.heading("color", text="Color")
        self.tree.heading("timecode", text="Timecode")

        self.tree.column("score", width=70, anchor="center")
        self.tree.column("name", width=430, anchor="w")
        self.tree.column("color", width=90, anchor="center")
        self.tree.column("timecode", width=140, anchor="center")

        vsb = ttk.Scrollbar(self.root, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)

        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        vsb.pack(side="left", fill="y", pady=8)

        # Double-click AND single-click both jump the playhead.
        self.tree.bind("<<TreeviewSelect>>", self._on_row_selected)

        bottom = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        bottom.pack(fill="x", side="bottom")
        self.detail_var = tk.StringVar(value="Click a marker to move the playhead there.")
        ttk.Label(bottom, textvariable=self.detail_var).pack(side="left")

    # ------------------------------------------------------------------
    def _connect_and_load(self):
        try:
            if self.resolve is None:
                self.resolve = connect_to_resolve()

            project_manager = self.resolve.GetProjectManager()
            self.project = project_manager.GetCurrentProject()
            if not self.project:
                raise RuntimeError("No project is currently open in Resolve.")

            self.timeline = self.project.GetCurrentTimeline()
            if not self.timeline:
                raise RuntimeError("No timeline is currently open in Resolve.")

            fps_setting = self.timeline.GetSetting("timelineFrameRate")
            self.fps = float(fps_setting) if fps_setting else 30.0

            self._load_markers()
            self.status_var.set(
                f"Project: {self.project.GetName()}  |  "
                f"Timeline: {self.timeline.GetName()}  |  "
                f"{len(self.marker_rows)} markers"
            )
        except Exception as exc:
            self.status_var.set("Not connected")
            messagebox.showerror("Resolve Marker Tracker", str(exc))

    # ------------------------------------------------------------------
    def _load_markers(self):
        markers = self.timeline.GetMarkers() or {}
        start_frame = self.timeline.GetStartFrame()

        rows = []
        for frame_id, info in markers.items():
            name = info.get("name", "") or ""
            match = SCORE_RE.search(name)
            score = int(match.group(1)) if match else 0
            abs_frame = start_frame + int(frame_id)
            tc = frames_to_timecode(abs_frame, self.fps)
            rows.append({
                "score": score,
                "name": name,
                "color": info.get("color", ""),
                "note": info.get("note", ""),
                "timecode": tc,
                "frame_id": int(frame_id),
            })

        # Highest score first; ties broken by earliest timecode.
        rows.sort(key=lambda r: (-r["score"], r["timecode"]))
        self.marker_rows = rows

        self.tree.delete(*self.tree.get_children())
        for row in rows:
            tag = row["color"] or "default"
            self.tree.insert(
                "", "end", iid=str(row["frame_id"]),
                values=(row["score"], row["name"], row["color"], row["timecode"]),
                tags=(tag,),
            )
            hex_color = RESOLVE_COLOR_HEX.get(row["color"])
            if hex_color:
                self.tree.tag_configure(tag, background=hex_color)

    # ------------------------------------------------------------------
    def _on_row_selected(self, _event):
        selection = self.tree.selection()
        if not selection:
            return
        frame_id = int(selection[0])
        row = next((r for r in self.marker_rows if r["frame_id"] == frame_id), None)
        if not row:
            return

        try:
            ok = self.timeline.SetCurrentTimecode(row["timecode"])
            if ok:
                self.detail_var.set(
                    f"Jumped to \"{row['name']}\" @ {row['timecode']} "
                    f"(Score {row['score']})"
                )
            else:
                self.detail_var.set(
                    f"Resolve rejected the jump to {row['timecode']} "
                    f"— is the timeline still current?"
                )
        except Exception as exc:
            messagebox.showerror("Resolve Marker Tracker", str(exc))


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except Exception:
        pass
    MarkerTrackerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
