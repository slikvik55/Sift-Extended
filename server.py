"""
Sift server — Flask backend for sift.html
Run with:  python server.py
"""
import os
import sys
import time
import base64
import shutil
import tempfile
import subprocess
import threading
import webbrowser
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory, send_file
from werkzeug.exceptions import HTTPException

app = Flask(__name__)


def _move_with_retry(src, dst, attempts=12, delay=0.15):
    """shutil.move that retries on transient Windows file locks.

    A media file that is still open by the browser (e.g. a <video> that is
    buffering/playing) can't be moved on Windows and raises PermissionError
    (WinError 32) / OSError. We retry briefly to ride out the lock instead of
    letting the exception bubble up as an HTML 500 page.
    """
    last = None
    for _ in range(attempts):
        try:
            shutil.move(src, dst)
            return
        except OSError as e:
            # 32 = file in use by another process, 5 = access denied (transient)
            if isinstance(e, PermissionError) or getattr(e, "winerror", None) in (32, 5):
                last = e
                time.sleep(delay)
            else:
                raise
    raise last


@app.errorhandler(Exception)
def _json_errors_for_api(e):
    """Ensure /api/* routes always return JSON, never Flask's HTML error page.

    Without this, an unhandled exception makes the frontend's response.json()
    fail with "Unexpected token '<'" because it received an HTML error page.
    """
    if not request.path.startswith("/api/"):
        if isinstance(e, HTTPException):
            return e
        raise e
    code = e.code if isinstance(e, HTTPException) else 500
    msg = (e.description if isinstance(e, HTTPException) else str(e)) or "Server error"
    return jsonify({"ok": False, "error": msg}), code

src_folder: str = ""          # absolute path selected by user

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".tiff", ".tif", ".avif", ".heic", ".heif", ".ico", ".svg",
}
VIDEO_EXTS = {
    ".mp4", ".webm", ".mov", ".avi", ".mkv", ".ogv", ".m4v",
    ".3gp", ".wmv", ".flv", ".mpeg", ".mpg",
}
AUDIO_EXTS = {
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac", ".opus", ".wma", ".aif", ".aiff",
}

MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

PORT = 7432
MAX_RECURSIVE_FILES = 2000   # safety cap for recursive scans


def _find_ffmpeg() -> "str | None":
    """Return path to ffmpeg binary, or None if not found anywhere."""
    try:
        r = subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return "ffmpeg"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    if sys.platform == "win32":
        import glob as _glob
        # winget places shims here after install; not yet in the process's PATH
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"),
        ]
        pkg_base = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
        if os.path.isdir(pkg_base):
            candidates += _glob.glob(
                os.path.join(pkg_base, "Gyan.FFmpeg*", "**", "bin", "ffmpeg.exe"),
                recursive=True,
            )
        for c in candidates:
            if os.path.isfile(c):
                return c
    return None


_ffmpeg_exe: "str | None" = _find_ffmpeg()
_keyframe_cache: dict = {}  # path -> (mtime, [seconds...])


def _find_ffprobe() -> "str | None":
    """Return path to ffprobe binary, preferring the sibling of the ffmpeg we found."""
    if _ffmpeg_exe:
        if _ffmpeg_exe == "ffmpeg":
            try:
                r = subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
                if r.returncode == 0:
                    return "ffprobe"
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass
        else:
            sibling = str(Path(_ffmpeg_exe).with_name("ffprobe.exe" if sys.platform == "win32" else "ffprobe"))
            if os.path.isfile(sibling):
                return sibling
    try:
        r = subprocess.run(["ffprobe", "-version"], capture_output=True, timeout=5)
        if r.returncode == 0:
            return "ffprobe"
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    if sys.platform == "win32":
        import glob as _glob
        candidates = [
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffprobe.exe"),
        ]
        pkg_base = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
        if os.path.isdir(pkg_base):
            candidates += _glob.glob(
                os.path.join(pkg_base, "Gyan.FFmpeg*", "**", "bin", "ffprobe.exe"),
                recursive=True,
            )
        for c in candidates:
            if os.path.isfile(c):
                return c
    return None


_ffprobe_exe: "str | None" = _find_ffprobe()


def _resolve_src_path(filename: str, src_subfolder: str = "") -> "str | None":
    """Resolve a media path under src_folder; return None if missing/invalid."""
    if not src_folder or not filename:
        return None
    if ".." in filename or ".." in (src_subfolder or ""):
        return None
    path = (
        os.path.join(src_folder, src_subfolder, filename)
        if src_subfolder
        else os.path.join(src_folder, filename)
    )
    return path if os.path.isfile(path) else None


def _unique_dest_path(dest_dir: str, filename: str) -> str:
    dest_path = os.path.join(dest_dir, filename)
    if not os.path.exists(dest_path):
        return dest_path
    base, ext = os.path.splitext(filename)
    n = 1
    while os.path.exists(dest_path):
        dest_path = os.path.join(dest_dir, f"{base}_{n}{ext}")
        n += 1
    return dest_path


def _list_keyframes(path: str) -> "list[float]":
    """Return I-frame timestamps (seconds) for the first video stream."""
    global _ffprobe_exe
    if not _ffprobe_exe:
        _ffprobe_exe = _find_ffprobe()
    if not _ffprobe_exe:
        raise RuntimeError("ffprobe is not available. Install ffmpeg to enable video editing.")

    mtime = os.path.getmtime(path)
    cached = _keyframe_cache.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    # Packet flags containing 'K' mark keyframes — much faster than decoding all frames.
    result = subprocess.run(
        [
            _ffprobe_exe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "packet=pts_time,flags",
            "-of", "csv=p=0",
            path,
        ],
        capture_output=True, text=True, timeout=180,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "ffprobe failed")

    keyframes: list[float] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        pts_s, flags = parts[0], parts[1]
        if "K" not in flags:
            continue
        try:
            t = float(pts_s)
        except ValueError:
            continue
        if t < 0:
            continue
        if not keyframes or abs(keyframes[-1] - t) > 0.001:
            keyframes.append(t)

    if not keyframes:
        keyframes = [0.0]

    _keyframe_cache[path] = (mtime, keyframes)
    return keyframes


def _stream_copy_segment(src: str, dest: str, start: float, end: float) -> None:
    """Extract [start, end) from src into dest with stream copy (no re-encode)."""
    global _ffmpeg_exe
    if not _ffmpeg_exe:
        _ffmpeg_exe = _find_ffmpeg()
    if not _ffmpeg_exe:
        raise RuntimeError("ffmpeg is not available")
    if end <= start:
        raise ValueError("Segment end must be after start")
    duration = end - start
    result = subprocess.run(
        [
            _ffmpeg_exe, "-y",
            "-ss", f"{start:.6f}",
            "-i", src,
            "-t", f"{duration:.6f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            dest,
        ],
        capture_output=True, text=True, timeout=600,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        raise RuntimeError(detail[-1] if detail else "ffmpeg segment export failed")


def _stream_copy_segments(src: str, dest: str, segments: "list[tuple[float, float]]") -> None:
    """Export one or more kept segments into dest via stream copy (+ concat if needed)."""
    if not segments:
        raise ValueError("No segments to export")
    if len(segments) == 1:
        _stream_copy_segment(src, dest, segments[0][0], segments[0][1])
        return

    global _ffmpeg_exe
    if not _ffmpeg_exe:
        _ffmpeg_exe = _find_ffmpeg()
    if not _ffmpeg_exe:
        raise RuntimeError("ffmpeg is not available")

    tmpdir = tempfile.mkdtemp(prefix="sift_edit_")
    try:
        part_paths = []
        for i, (start, end) in enumerate(segments):
            part = os.path.join(tmpdir, f"part_{i:03d}{Path(src).suffix}")
            _stream_copy_segment(src, part, start, end)
            part_paths.append(part)

        list_path = os.path.join(tmpdir, "concat.txt")
        with open(list_path, "w", encoding="utf-8") as fh:
            for p in part_paths:
                # concat demuxer needs forward-slash escaped paths
                safe = p.replace("\\", "/").replace("'", "'\\''")
                fh.write(f"file '{safe}'\n")

        result = subprocess.run(
            [
                _ffmpeg_exe, "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_path,
                "-c", "copy",
                dest,
            ],
            capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip().splitlines()
            raise RuntimeError(detail[-1] if detail else "ffmpeg concat failed")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _resource(rel_path: str) -> Path:
    """Resolve a bundled resource path — works both frozen (PyInstaller) and from source."""
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent
    return base / rel_path


# ─── Serve the UI ────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(_resource("sift.html"))

@app.route("/logo.png")
def serve_logo():
    p = _resource("logo.png")
    if not p.exists():
        return "", 404
    return send_file(p)

@app.route("/nimblecloud.png")
def serve_nimblecloud():
    p = _resource("nimblecloud.png")
    if not p.exists():
        return "", 404
    return send_file(p)


# ─── In-page folder browser ──────────────────────────────────────────────────

def _windows_drives() -> "list[str]":
    """Return available drive roots like ['C:\\\\', 'D:\\\\'] on Windows."""
    import string
    import ctypes
    drives = []
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return ["C:\\"]
    for i, letter in enumerate(string.ascii_uppercase):
        if bitmask & (1 << i):
            drives.append(f"{letter}:\\")
    return drives


@app.route("/api/list_dir")
def list_dir():
    """List the sub-folders of a directory so the UI can browse the filesystem.
    An empty path returns the drive list (Windows) or filesystem root (POSIX)."""
    raw = (request.args.get("path", "") or "").strip()

    # No path → top level: Windows drive list, or "/" on POSIX.
    if not raw:
        if sys.platform == "win32":
            drives = _windows_drives()
            return jsonify({
                "path": "", "parent": None, "isRoot": True,
                "dirs": [{"name": d, "path": d} for d in drives],
            })
        raw = "/"

    path = os.path.abspath(raw)
    if not os.path.isdir(path):
        return jsonify({"error": f"Not a folder: {path}"}), 400

    entries = []
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith('.'):
                    continue
                try:
                    if e.is_dir():
                        entries.append({"name": e.name, "path": os.path.join(path, e.name)})
                except OSError:
                    continue
    except PermissionError as ex:
        return jsonify({"error": f"Permission denied: {ex}"}), 403
    entries.sort(key=lambda d: d["name"].lower())

    # Determine the parent. At a drive root on Windows, go back to the drive list ("").
    parent = os.path.dirname(path)
    if sys.platform == "win32" and os.path.splitdrive(path)[1] in ("\\", "/", ""):
        parent = ""
    elif parent == path:
        parent = "" if sys.platform == "win32" else None

    return jsonify({"path": path, "parent": parent, "isRoot": False, "dirs": entries})


# ─── Open / scan folder ───────────────────────────────────────────────────────

@app.route("/api/open", methods=["POST"])
def api_open():
    global src_folder
    data = request.get_json(force=True)
    path = (data or {}).get("path", "").strip()
    if not path or not os.path.isdir(path):
        return jsonify({"error": f"Folder not found: {path}"}), 400
    src_folder = os.path.abspath(path)
    recursive = bool((data or {}).get("recursive", False))
    offset    = int((data or {}).get("offset", 0))
    return jsonify(_scan_recursive(offset) if recursive else _scan())


@app.route("/api/files")
def api_files():
    return jsonify(_scan())


@app.route("/api/peek")
def api_peek():
    """Return the first image file at or after the given total-media offset (recursive scan).
    Used to preview the thumbnail for the 'Next 2,000' pagination button."""
    path   = request.args.get("path",   "").strip()
    offset = int(request.args.get("offset", 0) or 0)
    if not path or not os.path.isdir(path):
        return jsonify({"found": False})
    path = os.path.abspath(path)
    skipped = 0
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = sorted(
                [d for d in dirnames if not d.startswith('.')],
                key=lambda n: n.lower(),
            )
            for name in sorted(filenames, key=lambda n: n.lower()):
                ext = os.path.splitext(name)[1].lower()
                if ext not in MEDIA_EXTS:
                    continue
                if skipped < offset:
                    skipped += 1
                    continue
                # Past the offset — return the first image we find
                if ext in IMAGE_EXTS:
                    rel_dir = os.path.relpath(dirpath, path)
                    rel_dir = '' if rel_dir == '.' else rel_dir.replace(os.sep, '/')
                    rel_path = (rel_dir + '/' + name) if rel_dir else name
                    return jsonify({"found": True, "path": rel_path})
                # Non-image media: keep counting but skip for thumbnail purposes
    except PermissionError:
        pass
    return jsonify({"found": False})


def _scan_recursive(offset: int = 0) -> dict:
    """Walk src_folder recursively, returning up to MAX_RECURSIVE_FILES media files
    starting from *offset* (number of media files to skip)."""
    if not src_folder or not os.path.isdir(src_folder):
        return {"files": [], "folder": ""}

    entries = []
    skipped = 0
    collected = 0

    try:
        for dirpath, dirnames, filenames in os.walk(src_folder):
            # Sort dirs so traversal order is predictable; skip hidden dirs
            dirnames[:] = sorted(
                [d for d in dirnames if not d.startswith('.')],
                key=lambda n: n.lower(),
            )

            rel_dir = os.path.relpath(dirpath, src_folder)
            rel_dir = '' if rel_dir == '.' else rel_dir.replace(os.sep, '/')

            for name in sorted(filenames, key=lambda n: n.lower()):
                ext = os.path.splitext(name)[1].lower()
                if ext not in MEDIA_EXTS:
                    continue

                # Skip files before the requested offset
                if skipped < offset:
                    skipped += 1
                    continue

                collected += 1
                # One past the cap — there are more files; stop here
                if collected > MAX_RECURSIVE_FILES:
                    return {
                        "files": entries,
                        "folder": src_folder,
                        "truncated": True,
                        "offset": offset,
                    }

                full = os.path.join(dirpath, name)
                if ext in IMAGE_EXTS:
                    media_type = "image"
                elif ext in VIDEO_EXTS:
                    media_type = "video"
                else:
                    media_type = "audio"

                entries.append({
                    "name":      name,
                    "subfolder": rel_dir,
                    "type":      media_type,
                    "size":      os.path.getsize(full),
                })
    except PermissionError as e:
        return {
            "files": entries, "folder": src_folder,
            "error": str(e), "truncated": False, "offset": offset,
        }

    return {"files": entries, "folder": src_folder, "truncated": False, "offset": offset}


def _scan() -> dict:
    if not src_folder or not os.path.isdir(src_folder):
        return {"files": [], "folder": ""}

    entries = []
    try:
        names = sorted(os.listdir(src_folder), key=lambda n: n.lower())
    except PermissionError as e:
        return {"files": [], "folder": src_folder, "error": str(e)}

    for name in names:
        full = os.path.join(src_folder, name)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in MEDIA_EXTS:
            continue
        if ext in IMAGE_EXTS:
            media_type = "image"
        elif ext in VIDEO_EXTS:
            media_type = "video"
        else:
            media_type = "audio"
        entries.append({
            "name": name,
            "type": media_type,
            "size": os.path.getsize(full),
        })
    return {"files": entries, "folder": src_folder}


# ─── Sort (move) a file ───────────────────────────────────────────────────────

@app.route("/api/sort", methods=["POST"])
def api_sort():
    global src_folder
    if not src_folder:
        return jsonify({"error": "No folder open"}), 400

    data = request.get_json(force=True) or {}
    filename      = data.get("filename",      "").strip()
    dest_name     = data.get("folder",        "").strip()
    src_subfolder = data.get("src_subfolder", "").strip()

    if not filename or not dest_name:
        return jsonify({"error": "Missing filename or folder"}), 400

    # Prevent path traversal
    if ".." in filename or ".." in dest_name or ".." in src_subfolder:
        return jsonify({"error": "Invalid path"}), 400

    src_path = os.path.join(src_folder, src_subfolder, filename) if src_subfolder else os.path.join(src_folder, filename)
    if not os.path.isfile(src_path):
        return jsonify({"error": f"File not found: {filename}"}), 404

    dest_dir = os.path.join(src_folder, dest_name)
    os.makedirs(dest_dir, exist_ok=True)

    dest_path = os.path.join(dest_dir, filename)

    # Resolve name collision
    if os.path.exists(dest_path):
        base, ext = os.path.splitext(filename)
        n = 1
        while os.path.exists(dest_path):
            dest_path = os.path.join(dest_dir, f"{base}_{n}{ext}")
            n += 1

    try:
        _move_with_retry(src_path, dest_path)
    except OSError as e:
        return jsonify({"error": f"Couldn't move file (it may still be in use): {e}"}), 409
    return jsonify({"ok": True, "dest_name": os.path.basename(dest_path)})


# ─── Video edit: keyframes + stream-copy export ──────────────────────────────

@app.route("/api/keyframes")
def api_keyframes():
    global src_folder
    if not src_folder:
        return jsonify({"ok": False, "error": "No folder open"}), 400

    filename = (request.args.get("filename") or "").strip()
    src_subfolder = (request.args.get("src_subfolder") or "").strip()
    if not filename:
        return jsonify({"ok": False, "error": "Missing filename"}), 400

    src_path = _resolve_src_path(filename, src_subfolder)
    if not src_path:
        return jsonify({"ok": False, "error": f"File not found: {filename}"}), 404

    try:
        keyframes = _list_keyframes(src_path)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "keyframes": keyframes})


@app.route("/api/export_edit", methods=["POST"])
def api_export_edit():
    """Stream-copy one or more kept segments into folder/basename_edit.ext.

    Does not move or modify the source file.
    """
    global src_folder, _ffmpeg_exe, _ffprobe_exe
    if not src_folder:
        return jsonify({"ok": False, "error": "No folder open"}), 400

    if not _ffmpeg_exe:
        _ffmpeg_exe = _find_ffmpeg()
    if not _ffmpeg_exe:
        return jsonify({
            "ok": False,
            "error": "ffmpeg is required for video editing. Install it from the Strip Metadata prompt, or from ffmpeg.org.",
            "ffmpeg_needed": True,
        }), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    dest_folder = (data.get("folder") or "").strip()
    src_subfolder = (data.get("src_subfolder") or "").strip()
    raw_segments = data.get("segments") or []

    if not filename or not dest_folder:
        return jsonify({"ok": False, "error": "Missing filename or folder"}), 400
    if ".." in filename or ".." in dest_folder or ".." in src_subfolder:
        return jsonify({"ok": False, "error": "Invalid path"}), 400
    if not isinstance(raw_segments, list) or not raw_segments:
        return jsonify({"ok": False, "error": "No segments to export"}), 400

    src_path = _resolve_src_path(filename, src_subfolder)
    if not src_path:
        return jsonify({"ok": False, "error": f"File not found: {filename}"}), 404

    segments: list[tuple[float, float]] = []
    for seg in raw_segments:
        try:
            start = float(seg.get("start"))
            end = float(seg.get("end"))
        except (TypeError, ValueError, AttributeError):
            return jsonify({"ok": False, "error": "Invalid segment"}), 400
        if end <= start:
            return jsonify({"ok": False, "error": "Each segment must have end > start"}), 400
        segments.append((start, end))

    base, ext = os.path.splitext(filename)
    edit_name = f"{base}_edit{ext}"
    dest_dir = os.path.join(src_folder, dest_folder)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = _unique_dest_path(dest_dir, edit_name)

    try:
        _stream_copy_segments(src_path, dest_path, segments)
    except Exception as e:
        if os.path.isfile(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "dest_name": os.path.basename(dest_path)})


@app.route("/api/snapshot", methods=["POST"])
def api_snapshot():
    """Save a JPEG snapshot next to the source video as basename_snap.jpg."""
    global src_folder
    if not src_folder:
        return jsonify({"ok": False, "error": "No folder open"}), 400

    data = request.get_json(force=True) or {}
    filename = (data.get("filename") or "").strip()
    src_subfolder = (data.get("src_subfolder") or "").strip()
    image_data = data.get("image") or ""

    if not filename or not image_data:
        return jsonify({"ok": False, "error": "Missing filename or image"}), 400
    if ".." in filename or ".." in src_subfolder:
        return jsonify({"ok": False, "error": "Invalid path"}), 400

    src_path = _resolve_src_path(filename, src_subfolder)
    if not src_path:
        return jsonify({"ok": False, "error": f"File not found: {filename}"}), 404

    if "," in image_data:
        image_data = image_data.split(",", 1)[1]
    try:
        raw = base64.b64decode(image_data, validate=False)
    except Exception:
        return jsonify({"ok": False, "error": "Invalid image data"}), 400
    if len(raw) < 100 or raw[:2] != b"\xff\xd8":
        return jsonify({"ok": False, "error": "Expected a JPEG image"}), 400

    base, _ext = os.path.splitext(filename)
    snap_name = f"{base}_snap.jpg"
    dest_dir = os.path.dirname(src_path)
    dest_path = _unique_dest_path(dest_dir, snap_name)

    try:
        with open(dest_path, "wb") as fh:
            fh.write(raw)
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    return jsonify({"ok": True, "dest_name": os.path.basename(dest_path), "path": dest_path})


# ─── Undo: move a file from a subfolder back to source ───────────────────────

@app.route("/api/undo", methods=["POST"])
def api_undo():
    global src_folder
    if not src_folder:
        return jsonify({"error": "No folder open"}), 400

    data = request.get_json(force=True) or {}
    folder_name  = data.get("folder",    "").strip()   # subfolder it lives in
    dest_name    = data.get("dest_name", "").strip()   # actual filename in subfolder
    orig_name    = data.get("orig_name", "").strip()   # original name to restore as

    if not folder_name or not dest_name or not orig_name:
        return jsonify({"error": "Missing parameters"}), 400

    for part in (folder_name, dest_name, orig_name):
        if ".." in part:
            return jsonify({"error": "Invalid path"}), 400

    src_path = os.path.join(src_folder, folder_name, dest_name)
    if not os.path.isfile(src_path):
        return jsonify({"error": f"File not found in {folder_name}/"}), 404

    restore_path = os.path.join(src_folder, orig_name)
    if os.path.exists(restore_path):
        base, ext = os.path.splitext(orig_name)
        n = 1
        while os.path.exists(restore_path):
            restore_path = os.path.join(src_folder, f"{base}_{n}{ext}")
            n += 1

    try:
        _move_with_retry(src_path, restore_path)
    except OSError as e:
        return jsonify({"error": f"Couldn't move file (it may still be in use): {e}"}), 409
    return jsonify({"ok": True, "restored_name": os.path.basename(restore_path)})


# ─── Strip metadata from media files in a folder ─────────────────────────────

@app.route("/api/strip_metadata", methods=["POST"])
def strip_metadata():
    global _ffmpeg_exe
    data = request.get_json(force=True) or {}
    folder_path = data.get("path", "").strip()
    if not folder_path or not os.path.isdir(folder_path):
        return jsonify({"error": "Folder not found"}), 400

    folder_path = os.path.abspath(folder_path)
    parent      = os.path.dirname(folder_path)
    folder_name = os.path.basename(folder_path)
    dest_name   = folder_name + "_no_metadata"
    dest_path   = os.path.join(parent, dest_name)

    if ".." in folder_name:
        return jsonify({"error": "Invalid path"}), 400

    os.makedirs(dest_path, exist_ok=True)

    try:
        from PIL import Image as PILImage
        pil_ok = True
    except ImportError:
        pil_ok = False

    try:
        import mutagen as _mutagen
        mutagen_ok = True
    except ImportError:
        mutagen_ok = False

    # Which extensions each method handles
    IMG_STRIP    = {".jpg", ".jpeg", ".png", ".webp"}
    MUTAGEN_EXTS = AUDIO_EXTS | {".mp4", ".m4v"}        # pure-Python, no external tool
    FFMPEG_EXTS  = VIDEO_EXTS - {".mp4", ".m4v"}         # needs ffmpeg binary

    stripped       = 0
    ffmpeg_skipped = 0
    errors         = []

    for name in sorted(os.listdir(folder_path)):
        src = os.path.join(folder_path, name)
        if not os.path.isfile(src):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in MEDIA_EXTS:
            continue
        dst = os.path.join(dest_path, name)

        if pil_ok and ext in IMG_STRIP:
            # Re-save via Pillow — strips all embedded metadata silently
            try:
                with PILImage.open(src) as img:
                    fmt = (img.format or ext.lstrip(".").upper()).upper()
                    if fmt in ("JPEG", "JPG"):
                        img.convert("RGB").save(dst, format="JPEG", quality=95)
                    elif fmt == "PNG":
                        img.copy().save(dst, format="PNG")
                    elif fmt == "WEBP":
                        img.copy().save(dst, format="WEBP", quality=90)
                    else:
                        img.copy().save(dst)
                stripped += 1
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        elif mutagen_ok and ext in MUTAGEN_EXTS:
            # Copy then wipe tags in-place with mutagen (no external tool needed)
            try:
                import mutagen as _mutagen
                shutil.copy2(src, dst)
                f = _mutagen.File(dst, easy=False)
                if f is not None:
                    f.delete()
                stripped += 1
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        elif _ffmpeg_exe and ext in FFMPEG_EXTS:
            # Strip container metadata with ffmpeg, no re-encode
            try:
                result = subprocess.run(
                    [_ffmpeg_exe, "-y", "-i", src,
                     "-map_metadata", "-1", "-c", "copy", dst],
                    capture_output=True, timeout=300,
                )
                if result.returncode == 0:
                    stripped += 1
                else:
                    errors.append(f"{name}: ffmpeg exit {result.returncode}")
            except Exception as exc:
                errors.append(f"{name}: {exc}")

        elif ext in FFMPEG_EXTS:
            # ffmpeg not available — skip rather than copy with metadata intact
            ffmpeg_skipped += 1

        # Remaining IMAGE_EXTS (gif, bmp, tiff, svg, etc.) skipped per same policy

    return jsonify({
        "ok":             True,
        "dest":           dest_path,
        "dest_name":      dest_name,
        "stripped":       stripped,
        "ffmpeg_skipped": ffmpeg_skipped,
        "ffmpeg_ok":      bool(_ffmpeg_exe),
        "errors":         errors[:10],
    })


# ─── Install ffmpeg via winget ───────────────────────────────────────────────

@app.route("/api/install_ffmpeg", methods=["POST"])
def install_ffmpeg():
    global _ffmpeg_exe, _ffprobe_exe
    try:
        subprocess.run(
            ["winget", "install", "--id", "Gyan.FFmpeg", "-e",
             "--accept-package-agreements", "--accept-source-agreements",
             "--scope", "user"],
            capture_output=True, text=True, timeout=300,
        )
    except FileNotFoundError:
        return jsonify({
            "ok": False,
            "error": "Windows Package Manager (winget) was not found on this system. "
                     "Please download ffmpeg manually from ffmpeg.org.",
        }), 500
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Installation timed out. Please try again or install manually."}), 500
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500

    _ffmpeg_exe = _find_ffmpeg()
    _ffprobe_exe = _find_ffprobe()
    if not _ffmpeg_exe:
        return jsonify({
            "ok": False,
            "error": "ffmpeg was installed but could not be located automatically. "
                     "Please restart Sift and try again.",
        }), 500

    return jsonify({"ok": True})


# ─── Serve media files (supports HTTP Range for video seeking) ───────────────

@app.route("/media/<path:filename>")
def serve_media(filename):
    if not src_folder:
        return "No folder open", 404
    # Security: resolve and confirm the file is inside src_folder
    try:
        target = Path(src_folder) / filename
        target.resolve().relative_to(Path(src_folder).resolve())
    except ValueError:
        return "Forbidden", 403
    return send_from_directory(src_folder, filename, conditional=True)


# ─── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    url = f"http://localhost:{PORT}"

    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    # Flask in a background daemon thread
    flask_thread = threading.Thread(
        target=lambda: app.run(
            host="127.0.0.1", port=PORT,
            debug=False, threaded=True, use_reloader=False,
        ),
        daemon=True,
    )
    flask_thread.start()

    # Open browser after a short delay
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        import pystray
        import tkinter as tk
        from PIL import Image as PILImage, ImageTk

        icon_path = _resource("logo.png")
        pil_img = (
            PILImage.open(icon_path)
            if icon_path.exists()
            else PILImage.new("RGB", (64, 64), (255, 153, 0))
        )

        # ── Shared quit ───────────────────────────────────────────
        def quit_all():
            try: tray.stop()
            except: pass
            os._exit(0)

        def open_browser(icon=None, item=None):
            webbrowser.open(url)

        # ── System tray icon (runs in its own thread) ─────────────
        tray = pystray.Icon(
            "Sift", pil_img.resize((64, 64)), "Sift",
            menu=pystray.Menu(
                pystray.MenuItem("Open Sift", open_browser, default=True),
                pystray.MenuItem("Quit",      lambda i, m: quit_all()),
            ),
        )
        tray.run_detached()

        # ── Tkinter window (taskbar presence) ─────────────────────
        # Tell Windows this is its own app (not pythonw) so taskbar icon is ours
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("sift.mediasort.app")
        except Exception:
            pass

        root = tk.Tk()
        root.title("Sift")
        root.geometry("300x90")
        root.resizable(False, False)
        root.configure(bg="#1a1a1a")

        # iconbitmap() is what Windows actually uses for the taskbar icon
        import tempfile
        _ico = Path(tempfile.gettempdir()) / "sift_icon.ico"
        pil_img.resize((32, 32)).save(str(_ico), format="ICO")
        root.iconbitmap(str(_ico))

        photo = ImageTk.PhotoImage(pil_img.resize((32, 32)))
        root.iconphoto(True, photo)

        ABOUT_TEXT = (
            "Sift  —  Free Media Sorter for Windows\n"
            "─────────────────────────────────────────\n\n"
            "A free media sorter for Windows, for the moment when you realize you have "
            "gigabytes of images, audio, or videos that you created sitting unsorted on "
            "your hard drive, and you just need to go through it fast and move the keepers "
            "somewhere.\n\n"
            "It happened to me, so I built Sift.\n\n"
            "I'm sharing it freely because I wouldn't have gotten this far without so much "
            "free support and assistance from others. No bloat, no subscription, no learning "
            "curve.\n\n"
            "If you like my work, and only if you can afford to, my family and I would truly "
            "appreciate your support!\nhttps://buymeacoffee.com/nimblecloud13\n\n"
            "Keep creating, and have a great day!\n\n"
            "- Nimble\n\n"
            "─────────────────────────────────────────\n"
            "Copyright © 2026 NimbleCloud13. All rights reserved.\n"
            "Free for personal, non-commercial use only.\n"
            "See LICENSE file for full terms."
        )

        def show_about():
            win = tk.Toplevel(root)
            win.title("About Sift")
            win.geometry("480x380")
            win.resizable(False, False)
            win.configure(bg="#1a1a1a")
            win.grab_set()
            try: win.iconbitmap(str(_ico))
            except: pass

            txt = tk.Text(win, bg="#1a1a1a", fg="#d4d4d4", font=("Segoe UI", 9),
                          wrap="word", relief="flat", padx=18, pady=14,
                          bd=0, highlightthickness=0, cursor="arrow")
            txt.insert("1.0", ABOUT_TEXT)
            txt.config(state="disabled")
            txt.pack(fill="both", expand=True)

            tk.Button(win, text="Close", command=win.destroy,
                      bg="#2c2c2c", fg="#d4d4d4", font=("Segoe UI", 10),
                      relief="flat", padx=16, pady=4, cursor="hand2").pack(pady=(0, 12))

        tk.Label(root, text="Sift is running",
                 bg="#1a1a1a", fg="#d4d4d4",
                 font=("Segoe UI", 11)).pack(pady=(14, 8))

        btn_frame = tk.Frame(root, bg="#1a1a1a")
        btn_frame.pack()
        tk.Button(btn_frame, text="Open", command=open_browser,
                  bg="#ff9900", fg="#000", font=("Segoe UI", 10, "bold"),
                  relief="flat", padx=16, pady=4, cursor="hand2").pack(side="left", padx=5)
        tk.Button(btn_frame, text="About", command=show_about,
                  bg="#2c2c2c", fg="#d4d4d4", font=("Segoe UI", 10),
                  relief="flat", padx=12, pady=4, cursor="hand2").pack(side="left", padx=5)
        tk.Button(btn_frame, text="Quit", command=quit_all,
                  bg="#2c2c2c", fg="#d4d4d4", font=("Segoe UI", 10),
                  relief="flat", padx=16, pady=4, cursor="hand2").pack(side="left", padx=5)

        root.protocol("WM_DELETE_WINDOW", quit_all)
        root.iconify()   # start minimized — shows as taskbar button
        root.mainloop()

    except ImportError:
        # Fallback: plain terminal mode (pystray/pillow not installed)
        print(f"\n  Sift running → {url}\n  Press Ctrl+C to stop.\n")
        try:
            flask_thread.join()
        except KeyboardInterrupt:
            pass
