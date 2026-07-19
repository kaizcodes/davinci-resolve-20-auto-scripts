#!/usr/bin/env python3
"""
DaVinci Resolve 20 - OBS VOD auto-importer
===========================================

Pick a folder like:
    F:\\OBS VOD\\2026-07-04 12-30-17

...that contains:
    2026-07-04 12-30-17.mp4                (video)
    2026-07-04 12-30-17_mic_fixed.srt      (subtitles)
    top50_markers.edl                      (timeline markers, exported from Resolve as
                                             a CMX3600 EDL: each marker is an edit event
                                             line followed by a "|C:.. |M:.. |D:.." line)

The script will:
    1. Open a folder-picker dialog
    2. Create a Media Pool bin named after the folder (the timestamp)
    3. Import the .mp4 and .srt into that bin
    4. Create a new timeline named after the folder, from the video
    5. Drop the .srt onto the timeline (subtitle track)
    6. Parse the .edl and re-create every marker on the timeline via AddMarker()
       (there is no scriptable equivalent of the manual "Timelines > Import >
       Timeline Markers from EDL" menu action, so this reconstructs it directly)

Requirements
------------
- DaVinci Resolve must already be running, with a project open.
- Run this with the SAME Python that Resolve's API is registered for
  (see the "External scripting" setup below if double-clicking doesn't work).
"""

import os
import re
import sys
import time
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox

# ---------------------------------------------------------------------------
# 1. Hook up to the DaVinci Resolve scripting API
# ---------------------------------------------------------------------------
try:
    import DaVinciResolveScript as dvr_script
except ImportError:
    # Running outside of Resolve's own script menu -> point Python at the
    # DaVinci Resolve API modules manually (Windows default paths shown).
    programdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    api_path = os.path.join(
        programdata,
        r"Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting\Modules",
    )
    sys.path.append(api_path)
    os.environ.setdefault(
        "RESOLVE_SCRIPT_API",
        r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
    )
    os.environ.setdefault(
        "RESOLVE_SCRIPT_LIB",
        r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll",
    )
    import DaVinciResolveScript as dvr_script


# ---------------------------------------------------------------------------
# 2. EDL marker parsing
# ---------------------------------------------------------------------------
VALID_MARKER_COLORS = {
    "BLUE", "CYAN", "GREEN", "YELLOW", "RED", "PINK", "PURPLE", "FUCHSIA",
    "ROSE", "LAVENDER", "SKY", "MINT", "LEMON", "SAND", "COCOA", "CREAM",
}

# Resolve's exported marker EDL uses TWO lines per marker, e.g.:
#   001  AX       V     C        00:22:04:00 00:22:04:01 00:22:04:00 00:22:04:01
#    |C:ResolveColorGreen |M:Game just crashed mid-match (Score 10) |D:1
# Line 1 = standard CMX3600 edit event (src_in src_out rec_in rec_out)
# Line 2 = |C:<color> |M:<marker name> |D:<duration in frames>
EVENT_RE = re.compile(
    r"^\s*\d+\s+\S+\s+\S+\s+\S+\s+"
    r"\d{2}:\d{2}:\d{2}[:;]\d{2}\s+"
    r"\d{2}:\d{2}:\d{2}[:;]\d{2}\s+"
    r"(\d{2}):(\d{2}):(\d{2})[:;](\d{2})\s+"
    r"\d{2}:\d{2}:\d{2}[:;]\d{2}"
)
MARKER_COMMENT_RE = re.compile(r"\|C:(\S+)\s*\|M:(.*?)\s*\|D:(\d+)")


def tc_to_frames(h, m, s, f, fps):
    return int(round(((h * 3600 + m * 60 + s) * fps) + f))


def parse_edl_markers(edl_path, fps):
    """Return a list of {frame, color, name, duration} dicts."""
    markers = []
    pending_tc = None
    with open(edl_path, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            ev = EVENT_RE.match(line)
            if ev:
                pending_tc = tuple(int(x) for x in ev.groups())
                continue
            mk = MARKER_COMMENT_RE.search(line)
            if mk and pending_tc is not None:
                color_raw, name, duration = mk.groups()
                color = color_raw.replace("ResolveColor", "").strip()
                if color.upper() not in VALID_MARKER_COLORS:
                    color = "Blue"
                h, m_, s, f = pending_tc
                frame = tc_to_frames(h, m_, s, f, fps)
                markers.append(
                    {
                        "frame": frame,
                        "color": color,
                        "name": name.strip() or "Marker",
                        "duration": max(1, int(duration)),
                    }
                )
                pending_tc = None
    return markers


# ---------------------------------------------------------------------------
# 3. Folder picking + file discovery
# ---------------------------------------------------------------------------
def pick_folder():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    folder = filedialog.askdirectory(title="Select OBS VOD folder")
    root.destroy()
    return folder


def find_required_files(folder):
    base_name = os.path.basename(os.path.normpath(folder))
    video_path = os.path.join(folder, base_name + ".mp4")
    srt_path = os.path.join(folder, base_name + "_mic_fixed.srt")
    edl_path = os.path.join(folder, "top50_markers.edl")

    missing = [p for p in (video_path, srt_path, edl_path) if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError(
            "Missing expected file(s) in that folder:\n" + "\n".join(missing)
        )
    return base_name, video_path, srt_path, edl_path


# ---------------------------------------------------------------------------
# 4. Main
# ---------------------------------------------------------------------------
def main():
    folder = pick_folder()
    if not folder:
        print("No folder selected, aborting.")
        return

    try:
        base_name, video_path, srt_path, edl_path = find_required_files(folder)
    except FileNotFoundError as e:
        messagebox.showerror("Missing files", str(e))
        return

    resolve = dvr_script.scriptapp("Resolve")
    if resolve is None:
        messagebox.showerror(
            "Resolve not found",
            "Could not connect to DaVinci Resolve.\n"
            "Make sure Resolve is running and external scripting is allowed "
            "(Preferences > General > External scripting using).",
        )
        return

    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        messagebox.showerror("No project", "No project is currently open in DaVinci Resolve.")
        return

    media_pool = project.GetMediaPool()
    root_folder = media_pool.GetRootFolder()

    # --- Bin (create or reuse) --------------------------------------------
    existing = [f for f in root_folder.GetSubFolderList() if f.GetName() == base_name]
    bin_folder = existing[0] if existing else media_pool.AddSubFolder(root_folder, base_name)
    media_pool.SetCurrentFolder(bin_folder)

    # --- Import video + srt -------------------------------------------------
    imported = media_pool.ImportMedia([video_path, srt_path])
    if not imported:
        messagebox.showerror("Import failed", "Could not import the video/srt into the media pool.")
        return

    video_clip, srt_clip = None, None
    for clip in imported:
        props = clip.GetClipProperty() or {}
        fp = props.get("File Path", "")
        if os.path.normpath(fp) == os.path.normpath(video_path):
            video_clip = clip
        elif os.path.normpath(fp) == os.path.normpath(srt_path):
            srt_clip = clip

    if video_clip is None:
        messagebox.showerror("Import failed", "Video clip wasn't found after import.")
        return

    # --- Timeline -------------------------------------------------------------
    timeline = media_pool.CreateTimelineFromClips(base_name, [video_clip])
    if timeline is None:
        messagebox.showerror("Timeline failed", "Could not create a timeline from the video.")
        return
    project.SetCurrentTimeline(timeline)

    # --- Frame rate (needed for both subtitle and marker placement) -------------
    fps_str = timeline.GetSetting("timelineFrameRate") or project.GetSetting("timelineFrameRate")
    try:
        fps = float(fps_str)
    except (TypeError, ValueError):
        fps = 30.0
        print(f"Couldn't read timeline frame rate, defaulting to {fps} fps.")

    # --- Subtitles ----------------------------------------------------------
    # IMPORTANT: there is no safe, scriptable way to place subtitle captions
    # onto a timeline at their correct timecodes. AppendToTimeline just dumps
    # the whole file as one block at track start. An earlier version of this
    # script tried slicing the same subtitle mediaPoolItem into per-caption
    # startFrame/endFrame chunks and calling AppendToTimeline once per caption
    # -- that pushed the API somewhere it isn't designed to go and could
    # crash/corrupt Resolve. So: the srt is imported into the bin (done above)
    # and left there. The only correct way to drop it onto the timeline with
    # working timecodes is the one-click manual step below.
    if srt_clip is not None:
        print(
            f"'{os.path.basename(srt_path)}' is imported into the bin '{base_name}'.\n"
            "To place it on the timeline with correct timecodes:\n"
            "  1. In the Media Pool, right-click the srt clip\n"
            "  2. Choose 'Insert Selected Subtitles to Timeline Using Timecode'\n"
            "(This is a one-click manual step -- there's no safe scripted "
            "equivalent for it.)"
        )
    else:
        print("Warning: SRT clip not found after import, subtitles skipped.")

    # --- Markers from EDL --------------------------------------------------------

    start_tc = timeline.GetStartTimecode() or "00:00:00:00"
    start_h, start_m, start_s, start_f = (int(x) for x in re.split("[:;]", start_tc))
    start_frame = tc_to_frames(start_h, start_m, start_s, start_f, fps)

    with open(edl_path, "r", encoding="utf-8", errors="ignore") as fh:
        raw_lines = fh.readlines()
    comment_like_lines = [l for l in raw_lines if "|C:" in l or "|M:" in l]
    print(f"EDL has {len(raw_lines)} lines total, {len(comment_like_lines)} look like marker comment lines.")
    if comment_like_lines and not any(MARKER_COMMENT_RE.search(l) for l in comment_like_lines):
        print("None of the marker comment lines matched the expected pattern. Example line(s):")
        for l in comment_like_lines[:3]:
            print(f"    {l.rstrip()}")
        print("-> Send these lines back so the parser regex can be adjusted.")

    markers = parse_edl_markers(edl_path, fps)

    # Two markers can't share the exact same frame -- Resolve silently
    # rejects the second one. Rather than lose markers to that, nudge any
    # collision forward by a frame (or more, if several stack up) until it
    # lands on a free spot. This keeps every marker, just off by a frame or
    # two when the source EDL had several highlights land in the same second.
    used_frames = set()
    nudged = 0
    for mk in markers:
        rel = mk["frame"] - start_frame
        rel = rel if rel >= 0 else mk["frame"]
        original = rel
        while rel in used_frames:
            rel += 1
        if rel != original:
            nudged += 1
        used_frames.add(rel)
        mk["rel_frame"] = rel
    if nudged:
        print(f"{nudged} marker(s) shared a timecode with another marker; "
              f"nudged forward by 1+ frame(s) so all of them survive.")

    added = 0
    for i, mk in enumerate(markers, start=1):
        rel_frame = mk["rel_frame"]
        try:
            if timeline.AddMarker(rel_frame, mk["color"], mk["name"], "", mk["duration"]):
                added += 1
            else:
                print(f"AddMarker rejected: frame={rel_frame} color={mk['color']} name={mk['name']!r}")
        except Exception as e:
            print(f"AddMarker raised {e!r} for {mk}")
            traceback.print_exc()

        if i % 10 == 0:
            print(f"  ...{i}/{len(markers)} markers processed")
        time.sleep(0.08)  # pace the calls so we don't overrun Resolve's scripting bridge

    messagebox.showinfo(
        "Done",
        f"Bin '{base_name}' ready.\n"
        f"Timeline '{base_name}' created with video and markers.\n"
        f"{added}/{len(markers)} markers added from {os.path.basename(edl_path)}.\n\n"
        + (
            "Srt is imported into the bin -- right-click it and choose "
            "'Insert Selected Subtitles to Timeline Using Timecode' to add captions."
            if srt_clip is not None
            else "No srt was imported."
        ),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log_path = os.path.join(os.path.expanduser("~"), "obs_vod_import_error.log")
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write(traceback.format_exc())
        print(f"Script crashed. Full traceback written to: {log_path}")
        traceback.print_exc()