# -*- coding: utf-8 -*-
"""
중복파일 정리기 v8.1  (DoubleKiller + dupeGuru 참고, 직접 작성)

비교 방식 : 크기 → 앞부분 64KB 해시 → 전체 SHA-256  (내용이 완전히 같은 파일만)
폴더 역할 : 일반 / 기준(절대 안 지움) / 제외(검색 안 함)
정리 방식 : 휴지통 / 격리폴더로 이동 / 하드링크로 대체(용량만 확보, 경로는 그대로)
안전장치  : 그룹당 최소 1개 남김, 정리 직전 파일 변경 여부 재확인, 정리 기록 CSV 자동 저장
필요한 것 : 파이썬 3.8 이상 + (선택) Pillow: 사진·영상 미리보기
폴더 끌어다 놓기 : Windows 기본 기능을 직접 연결 (설치 불필요)
"""
import csv
import fnmatch
import hashlib
import json
import os
import queue
import shutil
import subprocess
import io
import sys
import threading
import time
import tkinter as tk
from collections import defaultdict
from tkinter import filedialog, messagebox, ttk
from tkinter import font as tkfont

BaseTk = tk.Tk
try:                                    # 사진·영상 미리보기 (없어도 실행됨)
    from PIL import Image, ImageTk
except Exception:
    Image = ImageTk = None

CHUNK = 1024 * 1024
HEAD = 64 * 1024
APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False) else __file__))
SETTINGS = os.path.join(APP_DIR, "정리기_설정.json")
LOG_DIR = os.path.join(APP_DIR, "정리기록")
DEFAULT_EXCLUDE = "Thumbs.db; desktop.ini; .DS_Store; ~$*; *.tmp; *.part; *.crdownload"
ROLES = ["일반", "기준", "제외"]


# ───────────────────────── 핵심 로직 ─────────────────────────
class Item:
    __slots__ = ("path", "size", "mtime", "ref")

    def __init__(self, path, size, mtime, ref):
        self.path, self.size, self.mtime, self.ref = path, size, mtime, ref


def _norm(p):
    return os.path.normcase(os.path.abspath(p))


def _under(path, folders):
    p = _norm(path)
    return any(p == f or p.startswith(f.rstrip("\\/") + os.sep) for f in folders)


def collect(folders, recursive=True, min_size=1, patterns=(), report=None, stop=None):
    """folders: [(경로, 역할)]  →  [Item]"""
    excl = [_norm(p) for p, r in folders if r == "제외"]
    refs = [_norm(p) for p, r in folders if r == "기준"]
    roots = [p for p, r in folders if r != "제외"]
    seen_path, seen_inode, out = set(), set(), []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if stop is not None and stop.is_set():
                return out
            dirnames[:] = [d for d in dirnames
                           if d not in ("$RECYCLE.BIN", "System Volume Information")
                           and not _under(os.path.join(dirpath, d), excl)]
            if not recursive:
                dirnames[:] = []
            for name in filenames:
                if any(fnmatch.fnmatch(name.lower(), pt.lower()) for pt in patterns):
                    continue
                p = os.path.join(dirpath, name)
                key = _norm(p)
                if key in seen_path:
                    continue
                seen_path.add(key)
                try:
                    if os.path.islink(p):
                        continue
                    st = os.stat(p)
                except OSError:
                    continue
                if st.st_size < max(min_size, 1):
                    continue
                if st.st_ino:                      # 이미 하드링크로 묶인 파일은 1개로 취급
                    ino = (st.st_dev, st.st_ino)
                    if ino in seen_inode:
                        continue
                    seen_inode.add(ino)
                out.append(Item(p, st.st_size, st.st_mtime, _under(p, refs)))
                if report and len(out) % 500 == 0:
                    report(f"파일 목록 수집 중... {len(out):,}개", None)
    return out


def hash_file(path, head_only=False):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if head_only:
            h.update(f.read(HEAD))
        else:
            for b in iter(lambda: f.read(CHUNK), b""):
                h.update(b)
    return h.hexdigest()


def find_duplicates(folders, recursive=True, min_size=1, patterns=(), progress=None, stop=None,
                    ref_only=False):
    """ref_only=True: 기준폴더 파일과 내용이 같은 것만 찾음 (나머지 폴더끼리의 중복은 무시)"""
    report = progress or (lambda *a: None)
    stop = stop or threading.Event()
    items = collect(folders, recursive, min_size, patterns, report, stop)

    by_size = defaultdict(list)
    for it in items:
        by_size[it.size].append(it)
    cands = [g for g in by_size.values() if len(g) > 1 and (not ref_only or any(it.ref for it in g))]
    total = sum(len(g) for g in cands)
    report(f"전체 {len(items):,}개 중 크기가 같은 후보 {total:,}개 비교 시작", 0)

    groups, done = [], 0
    for g in cands:
        by_head = defaultdict(list)
        for it in g:
            if stop.is_set():
                return groups
            try:
                by_head[hash_file(it.path, True)].append(it)
            except OSError:
                pass
        for hg in by_head.values():
            if len(hg) < 2:
                continue
            if hg[0].size <= HEAD:
                subs = [hg]
            else:
                by_full = defaultdict(list)
                for it in hg:
                    if stop.is_set():
                        return groups
                    try:
                        by_full[hash_file(it.path)].append(it)
                    except OSError:
                        pass
                subs = [x for x in by_full.values() if len(x) > 1]
            for s in subs:
                if ref_only and not any(it.ref for it in s):
                    continue
                if not all(it.ref for it in s):    # 전부 기준폴더면 지울 게 없음
                    s.sort(key=lambda it: (not it.ref, it.path.lower()))
                    groups.append(s)
        done += len(g)
        report(f"내용 비교 중... {done:,}/{total:,}", done / max(total, 1))
    groups.sort(key=lambda s: -s[0].size * (len(s) - 1))
    return groups


KEEP_RULES = {
    "가장 오래된 파일": lambda it: it.mtime,
    "가장 최신 파일": lambda it: -it.mtime,
    "경로가 가장 짧은 파일": lambda it: (len(it.path), it.path.lower()),
    "파일명이 가장 짧은 파일": lambda it: (len(os.path.basename(it.path)), it.path.lower()),
    "'복사본/copy/(1)' 없는 파일": lambda it: (any(k in os.path.basename(it.path).lower()
                                             for k in ("복사본", "copy", "(1)", "(2)", "(3)", " - 복사")),
                                         len(it.path)),
}


def auto_mark(groups, rule, prefer_folder=""):
    """그룹마다 남길 1개를 고르고 나머지 경로를 반환. 기준폴더 파일은 항상 남김."""
    key = KEEP_RULES[rule]
    pf = _norm(prefer_folder) if prefer_folder else None
    marked = set()
    for g in groups:
        if any(it.ref for it in g):
            marked |= {it.path for it in g if not it.ref}
            continue
        keep = min(g, key=lambda it: (0 if pf and _under(it.path, [pf]) else 1, key(it)))
        marked |= {it.path for it in g if it is not keep}
    return marked


def mark_by_reference(groups):
    """기준폴더 파일이 있는 그룹에서 기준폴더 밖의 파일을 전부 체크. 기준 없는 그룹은 건드리지 않음."""
    return {it.path for g in groups if any(x.ref for x in g) for it in g if not it.ref}


def send_to_trash(path):
    if sys.platform != "win32":
        raise OSError("휴지통 기능은 Windows 전용")
    import ctypes
    from ctypes import wintypes

    class OP(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16),
                    ("fAnyOperationsAborted", wintypes.BOOL), ("hNameMappings", ctypes.c_void_p),
                    ("lpszProgressTitle", wintypes.LPCWSTR)]
    op = OP(None, 3, os.path.abspath(path) + "\0", None, 0x40 | 0x10 | 0x4 | 0x400, False, None, None)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) != 0 or os.path.exists(path):
        raise OSError("휴지통 이동 실패")
    return "휴지통"


def move_to_folder(path, dest_root):
    drive, rest = os.path.splitdrive(os.path.abspath(path))
    target = os.path.join(dest_root, drive.replace(":", "") or "root", rest.lstrip("\\/"))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    base, ext = os.path.splitext(target)
    i = 1
    while os.path.exists(target):
        target, i = f"{base} ({i}){ext}", i + 1
    shutil.move(path, target)
    return target


def replace_with_hardlink(path, keep):
    tmp = path + ".dupetmp"
    os.link(keep, tmp)                 # 다른 드라이브면 여기서 오류 → 원본 그대로
    os.replace(tmp, path)
    return f"하드링크 → {keep}"


def remove_empty_dirs(start, stop_at):
    stops = {_norm(s) for s in stop_at}
    d = os.path.dirname(start)
    while d and _norm(d) not in stops and os.path.isdir(d) and not os.listdir(d):
        os.rmdir(d)
        d = os.path.dirname(d)


def add_column_lines(tree):
    """ttk 표에는 세로줄 기능이 없어서, 열 경계마다 1px 선을 얹어 줌 (열 너비를 바꾸면 따라 움직임)"""
    lines = []

    def redraw(e=None):
        try:
            cols = tree["displaycolumns"]
            if cols in ("#all", ("#all",)):
                cols = tree["columns"]
            cols = list(cols)
            x = tree.column("#0", "width") if "tree" in str(tree["show"]) else 0
            xs = []
            for c in cols[:-1]:
                x += tree.column(c, "width")
                xs.append(x)
            while len(lines) < len(xs):
                lines.append(tk.Frame(tree, bg=T["grid"], width=1, cursor="arrow"))
            for ln, xx in zip(lines, xs):
                ln.place(x=xx, y=0, relheight=1, width=1)
            for ln in lines[len(xs):]:
                ln.place_forget()
        except tk.TclError:
            pass
    for ev in ("<Configure>", "<ButtonRelease-1>", "<B1-Motion>"):
        tree.bind(ev, lambda e: tree.after_idle(redraw), add="+")
    tree.after(80, redraw)


def reveal_in_explorer(path):
    """탐색기로 폴더를 열고 그 파일을 선택해 둠"""
    if sys.platform != "win32":
        return
    if os.path.exists(path):
        subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
    else:
        os.startfile(os.path.dirname(path))


def human(n):
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024 or u == "TB":
            return f"{n:,.0f} {u}" if u == "B" else f"{n:,.1f} {u}"
        n /= 1024


# ───────────────────────── 테마 ─────────────────────────
THEMES = {
    "midnight": {   # A안: 다크 프로툴 (라이너·레이캐스트 계열)
        "label": "미드나잇",
        "bg": "#111318", "card": "#191C23", "line": "#2A2F3A", "border": 2, "shadow": 0, "shadow_color": "#000000",
        "head": "#111318", "head_fg": "#F3F5F8", "head_sub": "#7D869B", "head_line": "#2A2F3A",
        "text": "#E7EAF0", "muted": "#8B93A7", "entry": "#111318",
        "primary": "#C6F432", "primary_fg": "#0E1013", "primary_hover": "#B3E21C",
        "stop": "#2A2F3A", "stop_fg": "#FF6B6B", "stop_hover": "#343A47",
        "danger": "#FF5D5D", "danger_fg": "#FFFFFF", "danger_hover": "#E84848",
        "ghost": "#222631", "ghost_fg": "#D5D9E2", "ghost_hover": "#2B303D",
        "ref_bg": "#1B2A45", "ref_fg": "#82B4FF", "ex_bg": "#1E2128", "ex_fg": "#565D6B",
        "grp_bg": "#20242E", "band0": "#191C23", "band1": "#2B3140", "grid": "#343B4A", "chk_bg": "#3A1D22", "chk_fg": "#FF8F8F", "sel": "#2D3550",
        "chip": "#242833", "disabled": "#2E333E", "disabled_fg": "#5C6370",
        "font": "Wanted Sans", "title_font": "Wanted Sans ExtraBold", "pixel": False,
        "tagline": "SAME BYTES. ZERO MERCY.",
    },
    "brutal": {     # B안: 네오브루탈리즘 (굵은 외곽선 + 딱딱한 그림자)
        "label": "브루탈",
        "bg": "#FFF3DC", "card": "#FFFFFF", "line": "#111111", "border": 2, "shadow": 5, "shadow_color": "#111111",
        "head": "#FFD23F", "head_fg": "#111111", "head_sub": "#3A3A3A", "head_line": "#111111",
        "text": "#111111", "muted": "#5B5B5B", "entry": "#FFFFFF",
        "primary": "#FF6FB5", "primary_fg": "#111111", "primary_hover": "#FF55A6",
        "stop": "#111111", "stop_fg": "#FFD23F", "stop_hover": "#333333",
        "danger": "#FF4D4D", "danger_fg": "#111111", "danger_hover": "#FF3333",
        "ghost": "#FFFFFF", "ghost_fg": "#111111", "ghost_hover": "#FFF3DC",
        "ref_bg": "#B9D3FF", "ref_fg": "#111111", "ex_bg": "#ECE5D3", "ex_fg": "#8E8571",
        "grp_bg": "#FFE88A", "band0": "#FFFFFF", "band1": "#FFF1BF", "grid": "#D6CDB8", "chk_bg": "#FFC2C2", "chk_fg": "#111111", "sel": "#C8F4FF",
        "chip": "#FFFFFF", "disabled": "#D9D2C2", "disabled_fg": "#8E8571",
        "font": "Wanted Sans Medium", "title_font": "Wanted Sans ExtraBlack", "pixel": False,
        "tagline": "똑같은 파일? 가차 없이 정리!",
    },
    "pixel": {      # C안: 레트로 픽셀 아케이드 (갈무리 폰트)
        "label": "픽셀",
        "bg": "#0D0A26", "card": "#16114A", "line": "#4636C8", "border": 2, "shadow": 4, "shadow_color": "#05031A",
        "head": "#0D0A26", "head_fg": "#39FF88", "head_sub": "#9C97D9", "head_line": "#4636C8",
        "text": "#ECEAFF", "muted": "#9C97D9", "entry": "#0D0A26",
        "primary": "#39FF88", "primary_fg": "#0D0A26", "primary_hover": "#22E872",
        "stop": "#FF3EA5", "stop_fg": "#FFFFFF", "stop_hover": "#E52A8F",
        "danger": "#FF3EA5", "danger_fg": "#FFFFFF", "danger_hover": "#E52A8F",
        "ghost": "#1F1866", "ghost_fg": "#ECEAFF", "ghost_hover": "#2A2290",
        "ref_bg": "#16337A", "ref_fg": "#8FD0FF", "ex_bg": "#1A1545", "ex_fg": "#5A5498",
        "grp_bg": "#241D6B", "band0": "#16114A", "band1": "#2A2385", "grid": "#3A3199", "chk_bg": "#4A1239", "chk_fg": "#FF8CCB", "sel": "#33299A",
        "chip": "#1F1866", "disabled": "#2A2466", "disabled_fg": "#5A5498",
        "font": "Galmuri11", "title_font": "Galmuri14", "pixel": True,
        "tagline": "▶ PRESS START TO KILL DUPES",
    },
}
DEFAULT_THEME = "pixel"
T = dict(THEMES[DEFAULT_THEME])
FALLBACK_FONT = "맑은 고딕" if sys.platform == "win32" else "Noto Sans CJK KR"


def load_private_fonts(private=True):
    """exe 안 또는 프로그램 옆 fonts 폴더의 폰트를 등록 (PC에 설치하지 않음).
    private=True: 이 프로그램에서만 보이게 / False: 로그아웃 전까지 임시 등록(첫 방법이 안 먹힐 때 재시도용)"""
    if sys.platform != "win32":
        return 0
    import ctypes
    dirs = [os.path.join(getattr(sys, "_MEIPASS", APP_DIR), "fonts"), os.path.join(APP_DIR, "fonts")]
    added, seen = 0, set()
    for d in dirs:
        if not os.path.isdir(d) or os.path.normcase(d) in seen:
            continue
        seen.add(os.path.normcase(d))
        for name in os.listdir(d):
            if name.lower().endswith((".ttf", ".otf")):
                try:
                    added += ctypes.windll.gdi32.AddFontResourceExW(os.path.join(d, name), 0x10 if private else 0, 0)
                except Exception:
                    pass
    return added


def fnt(size=9, bold=False, title=False):
    """모든 테마가 같은 픽셀 높이(12/15/24/30px)를 씀 → 테마를 바꿔도 화면 배치와 크기가 그대로.
    픽셀 테마는 갈무리 원래 크기의 정수배라서 선명함."""
    px = 30 if size >= 15 else 24 if size >= 12 else 15 if size >= 9 else 12
    if T["pixel"]:
        if px == 30:
            return (T["title_font"], -30)          # 갈무리14 × 2
        if px == 24:
            return (T["font"], -24)                # 갈무리11 × 2
        if px == 15:
            return (T["title_font"], -15)          # 갈무리14
        return (T["font"], -12, "bold" if bold else "normal")
    if title:
        return (T["title_font"], -px)
    fam = T["font"]
    if bold and fam.endswith("Medium"):
        fam = fam.replace(" Medium", "")
    return (fam, -px, "bold" if bold else "normal")


def mark_position(groups, where="first"):
    """그룹마다 목록상 첫 번째(또는 마지막) 파일 1개를 체크. 기준폴더 파일은 제외."""
    out = set()
    for g in groups:
        cand = [it for it in g if not it.ref]
        if not cand or (len(cand) == len(g) and len(g) < 2):
            continue
        out.add((cand[0] if where == "first" else cand[-1]).path)
    return out


# ───────────────────────── 미리보기 ─────────────────────────
TEXT_EXT = {".txt", ".csv", ".log", ".md", ".json", ".xml", ".ini", ".cfg", ".py", ".js", ".html", ".htm",
            ".css", ".srt", ".smi", ".vtt", ".yaml", ".yml", ".bat", ".sql", ".tsv"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".ico"}
VID_EXT = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mxf", ".mts", ".m2ts", ".ts", ".mpg",
           ".mpeg", ".webm", ".flv", ".3gp", ".vob"}


def read_text_preview(path, limit=4000):
    with open(path, "rb") as f:
        raw = f.read(limit)
    for enc in ("utf-8", "cp949"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "replace")


def shell_thumbnail(path, size=320):
    """Windows 탐색기가 보여주는 썸네일을 그대로 가져옴 (영상·사진·PDF 등). 실패하면 None"""
    if sys.platform != "win32" or Image is None:
        return None
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                    ("bmWidthBytes", ctypes.c_long), ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                    ("bmBits", ctypes.c_void_p)]

    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    ole32, shell32 = ctypes.windll.ole32, ctypes.windll.shell32
    gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
    ole32.CoInitializeEx(None, 2)
    ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(GUID)]
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID),
                                                    ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    gdi32.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]

    iid = GUID()
    ole32.CLSIDFromString("{bcc18b79-ba16-442f-80c4-8a59c30c463b}", ctypes.byref(iid))  # IShellItemImageFactory
    ptr = ctypes.c_void_p()
    if shell32.SHCreateItemFromParsingName(os.path.abspath(path), None, ctypes.byref(iid), ctypes.byref(ptr)) != 0 \
            or not ptr.value:
        return None
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    get_image = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_int,
                                   ctypes.POINTER(wintypes.HBITMAP))(vtbl[3])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    hbm = wintypes.HBITMAP()
    try:
        hr = get_image(ptr, SIZE(size, size), 0x8 | 0x1, ctypes.byref(hbm))   # 썸네일만 | 큰 크기 허용
    finally:
        release(ptr)
    if hr != 0 or not hbm:
        return None
    try:
        bm = BITMAP()
        gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
        w, h = bm.bmWidth, abs(bm.bmHeight)
        if w <= 0 or h <= 0:
            return None
        bih = BMIH(ctypes.sizeof(BMIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        buf = ctypes.create_string_buffer(w * h * 4)
        hdc = user32.GetDC(None)
        gdi32.GetDIBits(hdc, hbm, 0, h, buf, ctypes.byref(bih), 0)
        user32.ReleaseDC(None, hdc)
        return Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
    finally:
        gdi32.DeleteObject(hbm)


def ffmpeg_thumbnail(path, size=320):
    """PC에 ffmpeg가 있으면 영상에서 한 장면을 뽑음 (MXF 등 방송 포맷 대응). 없으면 None"""
    exe = shutil.which("ffmpeg")
    if not exe or Image is None:
        return None
    flags = 0x08000000 if sys.platform == "win32" else 0          # 검은 창 안 뜨게
    for ss in ("3", "0"):                                          # 3초 지점, 짧은 영상이면 처음
        try:
            r = subprocess.run([exe, "-v", "error", "-ss", ss, "-i", path, "-frames:v", "1",
                                "-vf", f"scale={size}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                               capture_output=True, timeout=15, creationflags=flags)
        except Exception:
            return None
        if r.returncode == 0 and r.stdout:
            return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    return None


def make_preview(path, box=(274, 148)):
    """("text", 내용, 출처) / ("image", PIL이미지, 출처) / ("tkimage", 경로, 출처) / ("none", 안내문)"""
    ext = os.path.splitext(path)[1].lower()
    if ext in TEXT_EXT:
        return ("text", read_text_preview(path), "텍스트")
    if Image is not None:
        img, src = None, ""
        if ext in IMG_EXT:
            try:
                img = Image.open(path)
                img.draft("RGB", (box[0] * 2, box[1] * 2))
                img, src = img.convert("RGB"), "사진"
            except Exception:
                img = None
        if img is None:
            try:
                img = shell_thumbnail(path)
                src = "탐색기 썸네일" if img is not None else ""
            except Exception:
                img = None
        if img is None and ext in VID_EXT:
            img = ffmpeg_thumbnail(path)
            src = "ffmpeg 장면 추출" if img is not None else ""
        if img is not None:
            img.thumbnail(box)
            return ("image", img, src)
    elif ext in (".png", ".gif"):
        return ("tkimage", path, "사진")
    if ext in VID_EXT:
        return ("none", "영상 썸네일을 만들 수 없어요.\n(코덱이 없거나 ffmpeg가 설치되지 않음)")
    if ext in IMG_EXT and Image is None:
        return ("none", "사진 미리보기는 Pillow 설치가 필요해요.")
    return ("none", "미리보기를 지원하지 않는 형식이에요.")


class PixelBar(tk.Canvas):
    """픽셀 테마용 블록 진행바. ttk.Progressbar 처럼 bar["value"] = 0~100 으로 사용"""

    def __init__(self, master, blocks=50):
        super().__init__(master, height=14, bg=T["bg"], highlightthickness=0)
        self.blocks, self._v = blocks, 0.0
        self.bind("<Configure>", lambda e: self._draw())

    def __setitem__(self, k, v):
        if k == "value":
            self._v = max(0.0, min(100.0, float(v))); self._draw()
        else:
            super().__setitem__(k, v)

    def __getitem__(self, k):
        return self._v if k == "value" else super().__getitem__(k)

    def _draw(self):
        self.delete("all")
        w, n, gap = self.winfo_width(), self.blocks, 3
        bw = (w - gap * (n - 1)) / n
        on = round(n * self._v / 100)
        for i in range(n):
            x = i * (bw + gap)
            self.create_rectangle(x, 0, x + bw, 14, width=0, fill=T["primary"] if i < on else T["chip"])


def enable_windows_drop(root, on_paths):
    """Windows 탐색기에서 끌어다 놓은 파일·폴더 경로를 받음 (외부 라이브러리 없이 WM_DROPFILES 직접 처리).
    관리자 권한으로 실행해도 받을 수 있게 메시지 필터도 열어 둠."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes
    user32, shell32 = ctypes.windll.user32, ctypes.windll.shell32
    LRESULT = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    setlp = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    setlp.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
    setlp.restype = ctypes.c_void_p
    user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.CallWindowProcW.restype = LRESULT
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_wchar_p, wintypes.UINT]
    shell32.DragQueryFileW.restype = wintypes.UINT
    shell32.DragFinish.argtypes = [ctypes.c_void_p]
    try:
        user32.ChangeWindowMessageFilterEx.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.DWORD, ctypes.c_void_p]
    except AttributeError:
        pass
    WM_DROPFILES = 0x0233

    def make_proc(holder):
        def proc(hwnd, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                try:
                    n = shell32.DragQueryFileW(wparam, 0xFFFFFFFF, None, 0)
                    paths = []
                    for i in range(n):
                        ln = shell32.DragQueryFileW(wparam, i, None, 0) + 1
                        buf = ctypes.create_unicode_buffer(ln)
                        shell32.DragQueryFileW(wparam, i, buf, ln)
                        paths.append(buf.value)
                    shell32.DragFinish(wparam)
                    on_paths(paths)
                except Exception:
                    pass
                return 0
            return user32.CallWindowProcW(holder[0], hwnd, msg, wparam, lparam)
        return proc

    root.update_idletasks()
    inner = root.winfo_id()
    keep, done = [], set()
    for hwnd in (inner, user32.GetParent(inner)):
        if not hwnd or hwnd in done:
            continue
        done.add(hwnd)
        shell32.DragAcceptFiles(hwnd, True)
        for m in (WM_DROPFILES, 0x004A, 0x0049):          # 관리자 실행 시에도 드롭 허용
            try:
                user32.ChangeWindowMessageFilterEx(hwnd, m, 1, None)
            except Exception:
                pass
        holder = [None]
        cb = WNDPROC(make_proc(holder))
        holder[0] = setlp(hwnd, -4, ctypes.cast(cb, ctypes.c_void_p))   # GWLP_WNDPROC
        keep.append(cb)
    root._drop_callbacks = keep                                     # 콜백이 사라지지 않게 보관
    return bool(keep)


# ───────────────────────── 위젯 ─────────────────────────
def shadow_wrap(master, **pack):
    """브루탈 테마면 딱딱한 그림자 틀을 만들고 그 안의 frame을 돌려줌"""
    s = T["shadow"]
    outer = tk.Frame(master, bg=master["bg"])
    outer.pack(**pack)
    if s:
        sh = tk.Frame(outer, bg=T.get("shadow_color", T["line"]))
        sh.place(x=5, y=5, relwidth=1, relheight=1, width=-5, height=-5)
    inner = tk.Frame(outer, bg=master["bg"])
    inner.pack(fill="both", expand=True, padx=(0, 5), pady=(0, 5))   # 그림자 유무와 관계없이 같은 여백
    return outer, inner


class FlatButton(tk.Label):
    def __init__(self, master, text, command, kind="primary", size=10, padx=16, pady=7, bold=True):
        bg, fg, hv = T[kind], T[kind + "_fg"], T[kind + "_hover"]
        bd = T["border"] if (T["border"] > 1 or kind == "ghost") else 0
        super().__init__(master, text=text, bg=bg, fg=fg, cursor="hand2", padx=padx, pady=pady,
                         font=fnt(size, bold), highlightthickness=bd,
                         highlightbackground=T["line"], highlightcolor=T["line"])
        self._bg, self._fg, self._hover, self._cmd, self._on = bg, fg, hv, command, True
        self.bind("<Enter>", lambda e: self._on and self.config(bg=self._hover))
        self.bind("<Leave>", lambda e: self._on and self.config(bg=self._bg))
        self.bind("<Button-1>", lambda e: self._on and self._cmd())

    def enable(self, on=True):
        self._on = on
        self.config(bg=self._bg if on else T["disabled"], fg=self._fg if on else T["disabled_fg"],
                    cursor="hand2" if on else "arrow")


class CheckBox(tk.Frame):
    """✔ 모양 체크박스 (테마 색 적용)"""

    def __init__(self, master, text, variable):
        super().__init__(master, bg=master["bg"])
        self.var = variable
        self.box = tk.Label(self, width=2, font=fnt(9, True), cursor="hand2",
                            highlightthickness=2, highlightbackground=T["line"])
        self.box.pack(side="left")
        self.lbl = tk.Label(self, text=text, bg=master["bg"], fg=T["text"], font=fnt(9), cursor="hand2")
        self.lbl.pack(side="left", padx=(7, 0))
        for w in (self, self.box, self.lbl):
            w.bind("<Button-1>", lambda e: self.var.set(not self.var.get()))
        self._tid = variable.trace_add("write", lambda *a: self._draw())
        self.bind("<Destroy>", self._untrace)
        self._draw()

    def _draw(self):
        try:
            on = bool(self.var.get())
            self.box.config(text="✔" if on else " ", bg=T["primary"] if on else T["entry"], fg=T["primary_fg"])
        except tk.TclError:
            pass

    def _untrace(self, e):
        if e.widget is self:
            try:
                self.var.trace_remove("write", self._tid)
            except Exception:
                pass


def big_button(master, text, command, kind, size, padx, pady, side="right", px=0):
    """그림자까지 포함된 큰 버튼"""
    _, inner = shadow_wrap(master, side=side, padx=px)
    b = FlatButton(inner, text, command, kind, size=size, padx=padx, pady=pady)
    b.pack()
    return b


def ghost(master, text, command):
    return FlatButton(master, text, command, "ghost", size=9, padx=10, pady=4, bold=False)


def card(master, title, hint="", expand=False):
    if T["pixel"]:
        for a, b in (("① ", "STAGE 1 ▸ "), ("② ", "STAGE 2 ▸ "), ("③ ", "STAGE 3 ▸ ")):
            title = title.replace(a, b)
    outer, inner = shadow_wrap(master, fill="both" if expand else "x", expand=expand, padx=16, pady=(10, 0))
    box = tk.Frame(inner, bg=T["card"], highlightthickness=T["border"], highlightbackground=T["line"])
    box.pack(fill="both", expand=True)
    top = tk.Frame(box, bg=T["card"]); top.pack(fill="x", padx=14, pady=(10, 6))
    if title:
        tk.Label(top, text=title, bg=T["card"], fg=T["text"], font=fnt(12, title=True)).pack(side="left", padx=(0, 10))
    if hint:
        tk.Label(top, text=hint, bg=T["card"], fg=T["muted"], font=fnt(9)).pack(side="left")
    body = tk.Frame(box, bg=T["card"]); body.pack(fill="both", expand=True, padx=14, pady=(0, 10))
    return outer, body


def chip(master, text, bg, fg):
    bd = T["border"] if T["border"] > 1 else 0
    return tk.Label(master, text=text, bg=bg, fg=fg, font=fnt(8, True), padx=8, pady=2,
                    highlightthickness=bd, highlightbackground=T["line"])


def label(master, text, muted=True, size=9, bold=False):
    return tk.Label(master, text=text, bg=master["bg"], fg=T["muted"] if muted else T["text"], font=fnt(size, bold))


# ───────────────────────── 화면 ─────────────────────────
class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("중복파일 정리기")
        self.geometry("1240x880")
        self.minsize(1000, 700)
        if sys.platform == "win32":
            try:
                self.state("zoomed")
            except tk.TclError:
                pass
        self.groups, self.checked, self.iid2item, self.iid2band = [], set(), {}, {}
        self.q, self.stop = queue.Queue(), threading.Event()
        self.scanning = False
        self.cfg = self._load()
        self._hide_guide = self.cfg.get("hide_guide", False)
        self.freed_total = int(self.cfg.get("freed_total", 0))
        self._pv_gen, self._pv_item, self._pv_photo, self._blink_on = 0, None, None, True
        self._opt_open, self._pv_width = False, int(self.cfg.get("pv_width", 580))
        self._tab = "folders"
        self._pv_box_size, self._pv_resize_job = (550, 300), None
        self._apply_theme(self.cfg.get("theme", DEFAULT_THEME))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.after(100, self._poll)
        self.after(650, self._blink)
        self.after(300, lambda: enable_windows_drop(self, lambda paths: self.q.put(("drop", paths))))
        if not self._hide_guide:
            self.after(500, self.show_guide)

    # 테마
    def _apply_theme(self, name):
        T.clear(); T.update(THEMES.get(name, THEMES[DEFAULT_THEME])); T["name"] = name
        fams = {f.lower() for f in tkfont.families(self)}
        need = [T[k] for k in ("font", "title_font")]
        if any(f.lower() not in fams for f in need) and not getattr(self, "_font_retry", False):
            self._font_retry = True                 # 비공개 등록이 안 보이면 임시 등록으로 한 번 더
            load_private_fonts(private=False)
            fams = {f.lower() for f in tkfont.families(self)}
        self._font_warn = ""
        for k in ("font", "title_font"):
            if T[k].lower() not in fams:            # 그래도 없으면 기본 한글 폰트로
                self._font_warn = f"'{T[k]}' 폰트를 찾지 못해 기본 글꼴로 표시 중 (fonts 폴더 위치를 확인하세요)"
                T[k] = FALLBACK_FONT
                T["pixel"] = False
        for w in self.winfo_children():
            w.destroy()
        self.configure(bg=T["bg"])
        self._style()
        self._build()
        self.render()

    def switch_theme(self, name):
        if self.scanning or name == T["name"]:
            try:
                self.theme_var.set(T["label"])
            except Exception:
                pass
            return
        self.cfg = self._collect()
        self._apply_theme(name)

    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        f = fnt(9)
        s.configure(".", font=f, background=T["card"], foreground=T["text"])
        s.configure("Treeview", rowheight=30, background=T["card"],
                    fieldbackground=T["card"], foreground=T["text"], bordercolor=T["line"],
                    borderwidth=0, font=f)
        s.configure("Treeview.Heading", background=T["grp_bg"] if T["shadow"] else T["chip"],
                    foreground=T["text"] if T["shadow"] else T["muted"], relief="flat",
                    font=fnt(9, True), bordercolor=T["line"], padding=(6, 6))
        s.map("Treeview", background=[("selected", T["sel"])], foreground=[("selected", T["text"])])
        s.map("Treeview.Heading", background=[("active", T["sel"])])
        for w in ("TCheckbutton", "TRadiobutton"):
            s.configure(w, background=T["card"], foreground=T["text"], font=f,
                        indicatorbackground=T["entry"], indicatorforeground=T["primary"] if T["name"] != "brutal" else T["line"],
                        bordercolor=T["line"], focuscolor=T["card"])
            s.map(w, background=[("active", T["card"])], indicatorbackground=[("pressed", T["sel"])])
        s.configure("TEntry", fieldbackground=T["entry"], foreground=T["text"], bordercolor=T["line"],
                    lightcolor=T["line"], darkcolor=T["line"], insertcolor=T["text"], padding=5)
        s.configure("TCombobox", fieldbackground=T["entry"], foreground=T["text"], background=T["ghost"],
                    bordercolor=T["line"], arrowcolor=T["text"], lightcolor=T["line"], darkcolor=T["line"], padding=4)
        s.map("TCombobox", fieldbackground=[("readonly", T["entry"])], foreground=[("readonly", T["text"])],
              selectbackground=[("readonly", T["entry"])], selectforeground=[("readonly", T["text"])])
        self.option_add("*TCombobox*Listbox.background", T["card"])
        self.option_add("*TCombobox*Listbox.foreground", T["text"])
        s.configure("Horizontal.TProgressbar", troughcolor=T["chip"] if T["name"] != "brutal" else "#FFFFFF",
                    background=T["primary"], bordercolor=T["line"], lightcolor=T["primary"],
                    darkcolor=T["primary"], thickness=8 if T["shadow"] else 6)
        s.configure("Vertical.TScrollbar", background=T["ghost"], troughcolor=T["card"], bordercolor=T["card"],
                    arrowcolor=T["muted"], lightcolor=T["ghost"], darkcolor=T["ghost"])

    # 설정
    def _load(self):
        try:
            with open(SETTINGS, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _collect(self):
        return {"folders": [list(self.ftree.item(i, "values")) for i in self.ftree.get_children()],
                "recursive": self.recursive.get(), "min_kb": self.min_kb.get(),
                "exclude": self.exclude.get(), "mode": self.mode.get(), "rule": self.rule.get(),
                "ref_only": self.ref_only.get(), "theme": T["name"], "pv_width": self._current_pv_width(),
                "hide_guide": self._hide_guide, "freed_total": self.freed_total, "prefer": self.prefer.get()}

    def _close(self):
        try:
            with open(SETTINGS, "w", encoding="utf-8") as f:
                json.dump(self._collect(), f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        self.stop.set()
        self.destroy()

    def _build(self):
        c = self.cfg

        # ── 헤더 (한 줄로 얇게)
        head = tk.Frame(self, bg=T["head"], height=56); head.pack(fill="x")
        head.pack_propagate(False)                       # 테마가 달라도 헤더 높이 고정
        tk.Frame(self, bg=T["head_line"], height=2).pack(fill="x")
        left = tk.Frame(head, bg=T["head"]); left.pack(side="left", padx=20)
        title = "DUPE KILLER" if T["pixel"] else "중복파일 정리기"
        tk.Label(left, text=title, bg=T["head"], fg=T["head_fg"], font=fnt(12, title=True)).pack(side="left")
        self.tag_lbl = tk.Label(left, text=T["tagline"], bg=T["head"], fg=T["head_sub"], font=fnt(8, True))
        self.tag_lbl.pack(side="left", padx=(14, 0), pady=(8, 0))
        score = tk.Frame(head, bg=T["head"]); score.pack(side="left", padx=(28, 0))
        tk.Label(score, text="TOTAL FREED" if T["pixel"] else "누적 확보", bg=T["head"], fg=T["head_sub"],
                 font=fnt(8, True)).pack(side="left", pady=(4, 0))
        self.lbl_freed = tk.Label(score, text=human(self.freed_total), bg=T["head"],
                                  fg=T["primary"] if T["name"] != "brutal" else T["head_fg"], font=fnt(10, True))
        self.lbl_freed.pack(side="left", padx=(8, 0))

        right = tk.Frame(head, bg=T["head"]); right.pack(side="right", padx=20, pady=(4, 0))
        self.btn_stop = big_button(right, "■  중지", self.stop_scan, "stop", 10, 18, 6, px=(8, 0))
        self.btn_scan = big_button(right, "▶  검색 시작", self.scan, "primary", 10, 28, 6)
        self.btn_stop.enable(self.scanning); self.btn_scan.enable(not self.scanning)
        hb = tk.Frame(head, bg=T["head"]); hb.pack(side="right", padx=(0, 16))
        FlatButton(hb, "❔ HOW TO" if T["pixel"] else "❔ 사용법", self.show_guide, "ghost", size=9,
                   padx=10, pady=3).pack()
        th = tk.Frame(head, bg=T["head"]); th.pack(side="right", padx=(0, 14))
        tk.Label(th, text="THEME", bg=T["head"], fg=T["head_sub"], font=fnt(8, True)).pack(side="left", padx=(0, 6))
        self.theme_var = tk.StringVar(value=T["label"])
        tcb = ttk.Combobox(th, textvariable=self.theme_var, values=[t["label"] for t in THEMES.values()],
                           state="readonly", width=9, font=fnt(9))
        tcb.pack(side="left")
        tcb.bind("<<ComboboxSelected>>", lambda e: self.after(
            30, lambda: self.switch_theme(next(k for k, t in THEMES.items() if t["label"] == self.theme_var.get()))))

        # ── 탭 (엑셀 시트처럼)
        tabbar = tk.Frame(self, bg=T["bg"], height=42); tabbar.pack(fill="x", padx=16, pady=(10, 0))
        tabbar.pack_propagate(False)                     # 탭 높이 고정
        self.tab_btns = {}
        for key in ("folders", "results"):
            b = tk.Label(tabbar, text="", font=fnt(10, True), padx=24, cursor="hand2",
                         highlightthickness=2)
            b.pack(side="left", fill="y", padx=(0, 4))
            b.bind("<Button-1>", lambda e, k=key: self.show_tab(k))
            self.tab_btns[key] = b
        tk.Frame(self, bg=T["primary"] if T["name"] != "brutal" else T["line"], height=2).pack(fill="x", padx=16)

        # ── 하단 상태줄 (모든 탭 공통)
        st = tk.Frame(self, bg=T["bg"]); st.pack(side="bottom", fill="x", padx=16, pady=(0, 10))
        self.pb = PixelBar(st) if T["pixel"] else ttk.Progressbar(st, mode="determinate", style="Horizontal.TProgressbar")
        self.pb.pack(fill="x")
        self.status = tk.Label(st, text="폴더를 추가하고 [검색 시작]을 누르세요.", bg=T["bg"], fg=T["muted"],
                               font=fnt(9), anchor="w")
        self.status.pack(fill="x", pady=(5, 0))

        area = tk.Frame(self, bg=T["bg"]); area.pack(fill="both", expand=True)
        self.tab_frames = {"folders": tk.Frame(area, bg=T["bg"]), "results": tk.Frame(area, bg=T["bg"])}

        # ══ 탭 1: 검색할 폴더
        hint = "폴더를 창으로 끌어다 놓아도 돼요 · 역할 칸 더블클릭 → 일반 / 🔒기준 / 제외"
        _, body = card(self.tab_frames["folders"], "", hint, expand=True)
        row = tk.Frame(body, bg=T["card"]); row.pack(fill="both", expand=True)
        tw = tk.Frame(row, bg=T["line"], padx=1, pady=1)
        tw.pack(side="left", fill="both", expand=True)
        self.ftree = ttk.Treeview(tw, columns=("path", "role"), show="headings")
        self.ftree.heading("path", text="폴더 경로"); self.ftree.heading("role", text="역할")
        self.ftree.column("path", width=900); self.ftree.column("role", width=120, anchor="center")
        self.ftree.tag_configure("기준", background=T["ref_bg"], foreground=T["ref_fg"], font=fnt(9, True))
        self.ftree.tag_configure("제외", background=T["ex_bg"], foreground=T["ex_fg"])
        self.ftree.tag_configure("일반", background=T["card"], foreground=T["text"])
        self.ftree.pack(fill="both", expand=True)
        self.ftree.bind("<Double-1>", self._cycle_role)
        add_column_lines(self.ftree)
        for p, r in c.get("folders", []):
            self._add_folder_row(p, r)
        fb = tk.Frame(row, bg=T["card"]); fb.pack(side="left", padx=(12, 0), anchor="n")
        FlatButton(fb, "＋ 폴더 추가", self.add_folder, "primary", size=9, padx=16, pady=7).pack(fill="x")
        ghost(fb, "역할 바꾸기", self._cycle_role).pack(fill="x", pady=6)
        ghost(fb, "목록에서 빼기", self._remove_folders).pack(fill="x")

        optbar = tk.Frame(body, bg=T["card"]); optbar.pack(fill="x", pady=(10, 0))
        self.btn_opt = ghost(optbar, "", self.toggle_options)
        self.btn_opt.pack(side="left")
        self.opt_frame = tk.Frame(body, bg=T["card"])
        o = tk.Frame(self.opt_frame, bg=T["card"]); o.pack(fill="x", pady=(10, 0))
        self.recursive = tk.BooleanVar(value=c.get("recursive", True))
        CheckBox(o, "하위 폴더 포함", self.recursive).pack(side="left")
        self.ref_only = tk.BooleanVar(value=c.get("ref_only", False))
        CheckBox(o, "기준폴더와 겹치는 것만 찾기", self.ref_only).pack(side="left", padx=18)
        label(o, "최소 크기(KB)").pack(side="left", padx=(6, 5))
        self.min_kb = tk.StringVar(value=c.get("min_kb", "1"))
        ttk.Entry(o, textvariable=self.min_kb, width=7, font=fnt(9)).pack(side="left")
        o2 = tk.Frame(self.opt_frame, bg=T["card"]); o2.pack(fill="x", pady=(8, 0))
        label(o2, "제외할 파일 이름 (; 로 구분)").pack(side="left", padx=(0, 6))
        self.exclude = tk.StringVar(value=c.get("exclude", DEFAULT_EXCLUDE))
        ttk.Entry(o2, textvariable=self.exclude, font=fnt(9)).pack(side="left", fill="x", expand=True)
        for v in (self.recursive, self.ref_only, self.min_kb, self.exclude):
            v.trace_add("write", lambda *a: self._update_opt_btn())
        if self._opt_open:
            self.opt_frame.pack(fill="x")
        self._update_opt_btn()

        # ══ 탭 2: 검색 결과 + 미리보기
        tr = self.tab_frames["results"]
        bo, bi = shadow_wrap(tr, side="bottom", fill="x", padx=16, pady=(10, 6))
        bar = tk.Frame(bi, bg=T["card"], highlightthickness=T["border"], highlightbackground=T["line"])
        bar.pack(fill="x")
        inner = tk.Frame(bar, bg=T["card"]); inner.pack(fill="x", padx=14, pady=8)
        label(inner, "정리 방식", muted=False, bold=True).pack(side="left", padx=(0, 10))
        self.mode = tk.StringVar(value=c.get("mode", "trash" if sys.platform == "win32" else "folder"))
        for t, v in [("휴지통 (추천)", "trash"), ("격리폴더로 이동", "folder"), ("하드링크로 대체", "link")]:
            ttk.Radiobutton(inner, text=t, variable=self.mode, value=v).pack(side="left", padx=5)
        self.btn_del = big_button(inner, "🗑  체크한 파일 정리", self.process, "danger", 10, 22, 7)

        _, body = card(tr, "", "☐ 칸을 눌러 체크 · 줄을 누르면 미리보기 · 더블클릭은 폴더 열기 · 오른쪽 클릭은 메뉴",
                       expand=True)
        tb = tk.Frame(body, bg=T["card"]); tb.pack(fill="x", pady=(0, 8))
        label(tb, "검색", size=9).pack(side="left")
        self.filter = tk.StringVar()
        self.filter.trace_add("write", lambda *a: self.render())
        ttk.Entry(tb, textvariable=self.filter, width=28, font=fnt(9)).pack(side="left", padx=(6, 14))
        chip(tb, "☑ 정리 대상", T["chk_bg"], T["chk_fg"]).pack(side="left")
        chip(tb, "🔒 기준폴더", T["ref_bg"], T["ref_fg"]).pack(side="left", padx=6)
        self.lbl_count = label(tb, "", size=9)
        self.lbl_count.pack(side="right")

        mid = tk.Frame(body, bg=T["line"], padx=1, pady=1)
        mid.pack(fill="both", expand=True)
        ck2 = tk.Frame(body, bg=T["card"]); ck2.pack(side="bottom", fill="x", pady=(8, 0), before=mid)
        ck = tk.Frame(body, bg=T["card"]); ck.pack(side="bottom", fill="x", pady=(10, 0), before=mid)

        self.pane = tk.PanedWindow(mid, orient="horizontal", sashwidth=8, bg=T["line"], bd=0, cursor="arrow",
                                   sashrelief="flat", opaqueresize=True, sashcursor="sb_h_double_arrow")
        self.pane.pack(fill="both", expand=True)
        left = tk.Frame(self.pane, bg=T["card"])
        pv = self._build_preview(self.pane)
        self.pane.add(left, minsize=420, stretch="always")
        self.pane.add(pv, minsize=320, width=self._pv_width, stretch="never")
        cols = ("grp", "check", "name", "folder", "size", "mtime")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for col, t, w in [("grp", "그룹", 90), ("check", "", 40), ("name", "파일명", 230), ("folder", "폴더", 260),
                          ("size", "크기", 100), ("mtime", "수정일", 160)]:
            self.tree.heading(col, text=t)
            self.tree.column(col, width=w, anchor="w" if col in ("name", "folder") else "center")
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<Button-1>", self.on_click)
        self.tree.bind("<space>", lambda e: [self.toggle(i) for i in self.tree.selection()])
        self.tree.bind("<Double-1>", self.open_folder)
        self.tree.bind("<<TreeviewSelect>>", self.on_select)
        self.tree.bind("<Button-3>", self.on_right_click)
        self.tree.configure(cursor="arrow")
        left.configure(cursor="arrow")
        add_column_lines(self.tree)
        self.tree.tag_configure("b0", background=T["band0"], foreground=T["text"])
        self.tree.tag_configure("b1", background=T["band1"], foreground=T["text"])
        self.tree.tag_configure("on", background=T["chk_bg"], foreground=T["chk_fg"])
        self.tree.tag_configure("ref", background=T["ref_bg"], foreground=T["ref_fg"])

        label(ck, "빠른 체크", muted=False, bold=True).pack(side="left", padx=(0, 8))
        ghost(ck, "⤒ 첫 번째 체크", lambda: self.mark_pos("first")).pack(side="left", padx=3)
        ghost(ck, "⤓ 마지막 체크", lambda: self.mark_pos("last")).pack(side="left", padx=3)
        ghost(ck, "🔒 기준폴더 기준 체크", self.auto_ref).pack(side="left", padx=3)
        ghost(ck, "체크 해제", self.uncheck_all).pack(side="left", padx=3)
        ghost(ck, "CSV 저장", self.export).pack(side="right")

        label(ck2, "자동 체크", muted=False, bold=True).pack(side="left", padx=(0, 8))
        label(ck2, "그룹마다 남길 파일").pack(side="left")
        self.rule = tk.StringVar(value=c.get("rule", "가장 오래된 파일"))
        ttk.Combobox(ck2, textvariable=self.rule, values=list(KEEP_RULES), state="readonly",
                     width=22, font=fnt(9)).pack(side="left", padx=6)
        label(ck2, "우선 남길 폴더(선택)").pack(side="left", padx=(8, 4))
        self.prefer = tk.StringVar(value=c.get("prefer", ""))
        ttk.Entry(ck2, textvariable=self.prefer, width=30, font=fnt(9)).pack(side="left")
        ghost(ck2, "…", lambda: self.prefer.set(filedialog.askdirectory() or self.prefer.get())).pack(side="left", padx=4)
        FlatButton(ck2, "✔ 자동 체크", self.auto, "primary", size=9, padx=14, pady=4).pack(side="left", padx=4)

        self.show_tab(self._tab)
        if getattr(self, "_font_warn", ""):
            self.status.config(text="⚠ " + self._font_warn)

    # 탭
    def show_tab(self, key):
        self._tab = key
        for k, f in self.tab_frames.items():
            f.pack_forget()
        self.tab_frames[key].pack(fill="both", expand=True)
        self._update_tabs()

    def _update_tabs(self):
        try:
            nf = len(self.ftree.get_children())
            ng = len(self.groups)
        except (tk.TclError, AttributeError):
            return
        names = {"folders": f"검색할 폴더  {nf}", "results": f"검색 결과  {ng}"}
        if T["pixel"]:
            names = {"folders": f"STAGE 1 · 검색할 폴더  {nf}", "results": f"STAGE 2 · 검색 결과  {ng}"}
        else:
            names = {"folders": f"① 검색할 폴더  {nf}", "results": f"② 검색 결과  {ng}"}
        for k, b in self.tab_btns.items():
            on = k == self._tab
            active_bg = T["head"] if T["name"] == "brutal" else T["card"]
            b.config(text=names[k], bg=active_bg if on else T["bg"], fg=T["text"] if on else T["muted"],
                     highlightbackground=T["primary"] if on and T["name"] != "brutal" else T["line"])

    def _remove_folders(self):
        for i in self.ftree.selection():
            self.ftree.delete(i)
        self._update_tabs()


    # 깜빡임
    def _blink(self):
        try:
            if T["pixel"] and self.tag_lbl.winfo_exists():
                self._blink_on = not self._blink_on
                self.tag_lbl.config(fg=T["head_sub"] if self._blink_on else T["head"])
        except (tk.TclError, AttributeError):
            pass
        self.after(650, self._blink)

    # 사용법 팝업
    def show_guide(self):
        w = tk.Toplevel(self)
        w.title("사용법")
        w.configure(bg=T["bg"])
        w.transient(self)
        w.resizable(False, False)
        _, inner = shadow_wrap(w, padx=22, pady=22)
        box = tk.Frame(inner, bg=T["card"], highlightthickness=T["border"], highlightbackground=T["line"])
        box.pack()
        pad = tk.Frame(box, bg=T["card"]); pad.pack(padx=24, pady=20)
        tk.Label(pad, text="HOW TO PLAY" if T["pixel"] else "처음이세요? 이렇게 쓰면 돼요", bg=T["card"],
                 fg=T["primary"] if T["name"] != "brutal" else T["text"], font=fnt(16, title=True)).pack(anchor="w")
        tk.Label(pad, text="내용이 100% 같은 파일만 찾아서, 원하는 것만 안전하게 정리하는 프로그램이에요.",
                 bg=T["card"], fg=T["muted"], font=fnt(9)).pack(anchor="w", pady=(4, 14))
        steps = [
            ("폴더 넣기", "[＋ 폴더 추가]를 누르거나, 탐색기에서 폴더를 프로그램 창으로 끌어다 놓으세요. 여러 개 넣어도 돼요."),
            ("역할 정하기", "원본이 있는 폴더는 역할 칸을 더블클릭해서 🔒기준으로 바꾸세요. 기준 폴더 파일은 절대 지워지지 않아요. "
                       "검색에서 뺄 하위 폴더는 '제외'로 두면 돼요. 세부 옵션은 [⚙ 검색 옵션]을 누르면 나와요."),
            ("검색 시작", "오른쪽 위 [▶ 검색 시작]을 누르세요. 크기 → 앞부분 → 전체 내용 순서로 비교해서 똑같은 파일만 묶어 줘요."),
            ("체크하기", "지울 파일의 ☐ 칸을 눌러서 ☑로 만드세요. [빠른 체크]·[자동 체크]로 한 번에 체크할 수 있고, "
                     "파일을 선택하면 오른쪽 미리보기에서 확인할 수 있어요. 오른쪽 클릭하면 폴더 열기·경로 복사 같은 메뉴가 나와요."),
            ("정리하기", "아래에서 정리 방식(휴지통 추천)을 고르고 [🗑 체크한 파일 정리]를 누르세요. "
                     "무엇을 정리했는지는 '정리기록' 폴더에 CSV로 남아요."),
        ]
        for n, (t, d) in enumerate(steps, 1):
            row = tk.Frame(pad, bg=T["card"]); row.pack(fill="x", pady=5)
            tk.Label(row, text=str(n), bg=T["primary"], fg=T["primary_fg"], font=fnt(12, True), width=2,
                     highlightthickness=T["border"] if T["border"] > 1 else 0,
                     highlightbackground=T["line"]).pack(side="left", anchor="n")
            col = tk.Frame(row, bg=T["card"]); col.pack(side="left", padx=12, fill="x")
            tk.Label(col, text=t, bg=T["card"], fg=T["text"], font=fnt(10, True)).pack(anchor="w")
            tk.Label(col, text=d, bg=T["card"], fg=T["muted"], font=fnt(9), wraplength=600,
                     justify="left").pack(anchor="w")
        tip = tk.Label(pad, text="🛡 안전장치: 그룹마다 최소 1개는 꼭 남도록 막혀 있어요. 실수해도 휴지통에서 되살릴 수 있어요.",
                       bg=T["chip"], fg=T["text"], font=fnt(9), wraplength=660, justify="left", padx=10, pady=8)
        tip.pack(fill="x", pady=(12, 14))
        bottom = tk.Frame(pad, bg=T["card"]); bottom.pack(fill="x")
        var = tk.BooleanVar(value=self._hide_guide)
        CheckBox(bottom, "다음부터 이 창 보지 않기", var).pack(side="left")

        def close():
            self._hide_guide = var.get()
            w.grab_release()
            w.destroy()
        FlatButton(bottom, "START ▶" if T["pixel"] else "시작하기", close, "primary", size=11,
                   padx=22, pady=7).pack(side="right")
        w.protocol("WM_DELETE_WINDOW", close)
        w.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - w.winfo_width()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - w.winfo_height()) // 3)
        w.geometry(f"+{x}+{y}")
        try:
            w.grab_set()
        except tk.TclError:
            pass

    # 미리보기
    def _build_preview(self, parent):
        """미리보기 패널. 결과 표와의 경계선을 끌어서 폭을 바꿀 수 있고, 이미지도 칸 크기에 맞춰 다시 그림"""
        self._pv_item = None
        pv = tk.Frame(parent, bg=T["card"])
        top = tk.Frame(pv, bg=T["card"]); top.pack(fill="x", padx=12, pady=(8, 4))
        tk.Label(top, text="PREVIEW" if T["pixel"] else "미리보기", bg=T["card"], fg=T["primary"] if T["name"] != "brutal"
                 else T["text"], font=fnt(10, True)).pack(side="left")
        self.pv_src = tk.Label(top, text="", bg=T["card"], fg=T["muted"], font=fnt(8))
        self.pv_src.pack(side="right")
        info = tk.Frame(pv, bg=T["card"]); info.pack(side="bottom", fill="x", padx=12, pady=(6, 8))
        self.pv_name = tk.Label(info, text="", bg=T["card"], fg=T["text"], font=fnt(10, True), justify="left", anchor="w")
        self.pv_name.pack(fill="x")
        row = tk.Frame(info, bg=T["card"]); row.pack(fill="x", pady=(2, 0))
        self.pv_meta = tk.Label(row, text="", bg=T["card"], fg=T["muted"], font=fnt(9), justify="left")
        self.pv_meta.pack(side="left")
        ghost(row, "📂 폴더 열기", self._open_pv_folder).pack(side="right")
        self.pv_box = tk.Frame(pv, bg=T["entry"], highlightthickness=1, highlightbackground=T["line"])
        self.pv_box.pack(fill="both", expand=True, padx=12)
        self.pv_box.pack_propagate(False)
        self.pv_img = tk.Label(self.pv_box, bg=T["entry"], fg=T["muted"], font=fnt(9),
                               text="파일을 클릭하면\n여기에 미리보기가 나와요")
        self.pv_img.pack(fill="both", expand=True)
        self.pv_text = tk.Text(self.pv_box, bg=T["entry"], fg=T["text"], font=fnt(9), relief="flat",
                               wrap="word", padx=8, pady=8, highlightthickness=0)
        pv.bind("<Configure>", lambda e: self.pv_name.config(wraplength=max(100, e.width - 24)))
        self.pv_box.bind("<Configure>", self._pv_resized)
        return pv

    def _pv_size(self):
        try:
            w, h = self.pv_box.winfo_width() - 6, self.pv_box.winfo_height() - 6
        except tk.TclError:
            return self._pv_box_size
        return (max(w, 120), max(h, 90)) if w > 20 and h > 20 else self._pv_box_size

    def _pv_resized(self, e=None):
        if self._pv_resize_job:
            self.after_cancel(self._pv_resize_job)
        self._pv_resize_job = self.after(250, self._pv_reload)

    def _pv_reload(self):
        self._pv_resize_job = None
        size = self._pv_size()
        if self._pv_item is not None and abs(size[0] - self._pv_box_size[0]) + abs(size[1] - self._pv_box_size[1]) > 30:
            it, self._pv_item = self._pv_item, None
            self._request_preview(it)

    def _current_pv_width(self):
        try:
            x, _ = self.pane.sash_coord(0)
            return max(320, self.pane.winfo_width() - x - 8)
        except Exception:
            return self._pv_width

    def _pv_message(self, msg):
        self.pv_text.pack_forget()
        self.pv_img.config(image="", text=msg)
        self.pv_img.pack(fill="both", expand=True)
        self._pv_photo = None

    def on_select(self, e=None):
        sel = self.tree.selection()
        if not sel:
            return
        it = self.iid2item.get(sel[0])
        if it is None:
            kids = self.tree.get_children(sel[0])
            it = self.iid2item.get(kids[0]) if kids else None
        if it is None or it is self._pv_item:
            return
        self._request_preview(it)

    def _request_preview(self, it):
        self._pv_item = it
        self._pv_gen += 1
        gen = self._pv_gen
        self.pv_name.config(text=os.path.basename(it.path))
        self.pv_meta.config(text=f"{human(it.size)}  ·  {time.strftime('%Y-%m-%d %H:%M', time.localtime(it.mtime))}"
                                 + ("  ·  🔒기준" if it.ref else ""))
        self.pv_src.config(text="")
        self._pv_message("불러오는 중...")
        box = self._pv_box_size = self._pv_size()

        def work():
            try:
                res = make_preview(it.path, box)
            except Exception as ex:
                res = ("none", f"미리보기 오류: {ex}")
            self.q.put(("pv", gen, res))
        threading.Thread(target=work, daemon=True).start()

    def _show_preview(self, gen, res):
        if gen != self._pv_gen:
            return
        try:
            kind = res[0]
            if kind == "text":
                self.pv_img.pack_forget()
                self.pv_text.config(state="normal")
                self.pv_text.delete("1.0", "end")
                self.pv_text.insert("1.0", res[1])
                self.pv_text.config(state="disabled")
                self.pv_text.pack(fill="both", expand=True)
                self.pv_src.config(text=res[2])
            elif kind == "image":
                self._pv_photo = ImageTk.PhotoImage(res[1])
                self.pv_text.pack_forget()
                self.pv_img.config(image=self._pv_photo, text="")
                self.pv_img.pack(fill="both", expand=True)
                self.pv_src.config(text=res[2])
            elif kind == "tkimage":
                ph = tk.PhotoImage(file=res[1])
                bw, bh = self._pv_box_size
                k = max(1, -(-ph.width() // bw), -(-ph.height() // bh))
                self._pv_photo = ph.subsample(k, k)
                self.pv_text.pack_forget()
                self.pv_img.config(image=self._pv_photo, text="")
                self.pv_img.pack(fill="both", expand=True)
                self.pv_src.config(text=res[2])
            else:
                self._pv_message(res[1])
        except (tk.TclError, AttributeError):
            pass

    def _open_pv_folder(self):
        if self._pv_item:
            reveal_in_explorer(self._pv_item.path)

    # 폴더
    def folders(self):
        return [tuple(self.ftree.item(i, "values")) for i in self.ftree.get_children()]

    def _add_folder_row(self, p, r):
        self.ftree.insert("", "end", values=(p, r), tags=(r,))
        if hasattr(self, "tab_btns"):
            self._update_tabs()

    def _add_path(self, d):
        d = os.path.normpath(d)
        if d in [p for p, _ in self.folders()]:
            return 0
        self._add_folder_row(d, "일반")
        return 1

    def add_folder(self):
        d = filedialog.askdirectory()
        if d:
            self._add_path(d)

    def toggle_options(self):
        self._opt_open = not self._opt_open
        if self._opt_open:
            self.opt_frame.pack(fill="x")
        else:
            self.opt_frame.pack_forget()
        self._update_opt_btn()

    def _update_opt_btn(self):
        try:
            changed = (not self.recursive.get() or self.ref_only.get()
                       or self.min_kb.get().strip() not in ("1", "1.0")
                       or self.exclude.get().strip() != DEFAULT_EXCLUDE)
            arrow = "▴" if self._opt_open else "▾"
            self.btn_opt.config(text=f"⚙ 검색 옵션 {arrow}" + ("   · 변경됨" if changed else ""),
                                fg=T["primary"] if changed and T["name"] != "brutal" else T["ghost_fg"])
        except (tk.TclError, AttributeError):
            pass

    def _handle_drop(self, paths):
        added = skipped = 0
        for p in paths:
            if os.path.isdir(p):
                added += self._add_path(p)
            else:
                skipped += 1
        msg = f"폴더 {added}개를 추가했어요."
        if skipped:
            msg += f" (파일 {skipped}개는 폴더가 아니라서 건너뜀)"
        self.status.config(text=msg)

    def _cycle_role(self, e=None):
        for i in self.ftree.selection():
            p, r = self.ftree.item(i, "values")
            r = ROLES[(ROLES.index(r) + 1) % 3]
            self.ftree.item(i, values=(p, r), tags=(r,))

    # 검색
    def scan(self):
        folders = self.folders()
        if not any(r != "제외" for _, r in folders):
            return messagebox.showinfo("안내", "검색할 폴더(일반 또는 기준)를 1개 이상 추가하세요.")
        ref_only = self.ref_only.get()
        if ref_only and not any(r == "기준" for _, r in folders):
            return messagebox.showinfo("안내", "'기준폴더와 겹치는 것만'을 쓰려면 폴더 1개 이상을 '기준'으로 지정하세요.\n(역할 칸 더블클릭)")
        try:
            min_size = max(1, int(float(self.min_kb.get() or 0) * 1024))
        except ValueError:
            return messagebox.showwarning("안내", "최소 크기는 숫자로 입력하세요.")
        patterns = [p.strip() for p in self.exclude.get().split(";") if p.strip()]
        self.stop.clear(); self.scanning = True
        self.btn_scan.enable(False); self.btn_stop.enable(True)
        self.groups, self.checked = [], set(); self.render()
        self.pb["value"] = 0
        t0 = time.time()
        recursive = self.recursive.get()

        def work():
            try:
                g = find_duplicates(folders, recursive, min_size, patterns,
                                    progress=lambda m, f: self.q.put(("p", m, f)), stop=self.stop,
                                    ref_only=ref_only)
            except Exception as ex:
                g = []; self.q.put(("p", f"오류: {ex}", None))
            self.q.put(("done", g, time.time() - t0))
        threading.Thread(target=work, daemon=True).start()

    def stop_scan(self):
        self.stop.set()
        self.status.config(text="중지하는 중... 지금까지 찾은 결과를 보여드려요.")

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                if m[0] == "pv":
                    self._show_preview(m[1], m[2])
                    continue
                if m[0] == "drop":
                    self._handle_drop(m[1])
                    continue
                if m[0] == "p":
                    self.status.config(text=m[1])
                    if m[2] is not None:
                        self.pb["value"] = m[2] * 100
                else:
                    self.groups = m[1]; self.scanning = False
                    self.btn_scan.enable(True); self.btn_stop.enable(False)
                    self.pb["value"] = 100
                    self.render()
                    waste = sum(g[0].size * (len(g) - 1) for g in self.groups)
                    head = "중지됨" if self.stop.is_set() else "완료"
                    self.show_tab("results")
                    self.status.config(text=f"{head} ({m[2]:.1f}초) — 중복 그룹 {len(self.groups):,}개, "
                                            f"정리하면 최대 {human(waste)} 확보 가능")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # 결과 표시
    def render(self):
        self.tree.delete(*self.tree.get_children()); self.iid2item.clear(); self.iid2band = {}
        f = self.filter.get().strip().lower()
        shown = 0
        for gi, g in enumerate(self.groups, 1):
            if f and not any(f in it.path.lower() for it in g):
                continue
            shown += 1
            if shown > 3000:
                self.tree.insert("", "end", values=("…", "", "결과가 많아 3,000그룹까지만 표시 — 검색칸을 사용하세요", "", "", ""))
                break
            band = "b0" if shown % 2 else "b1"          # 그룹마다 배경색을 번갈아
            for k, it in enumerate(g):
                label_txt = f"#{gi} ({len(g)}개)" if k == 0 else ""
                iid = self.tree.insert("", "end", values=(label_txt, self._mark(it), os.path.basename(it.path),
                                       os.path.dirname(it.path), human(it.size),
                                       time.strftime("%Y-%m-%d %H:%M", time.localtime(it.mtime))),
                                       tags=self._tags(it, band))
                self.iid2item[iid] = it
                self.iid2band[iid] = band
        self._summary()

    def _mark(self, it):
        return "🔒" if it.ref else ("☑" if it.path in self.checked else "☐")

    def _tags(self, it, band="b0"):
        return ("ref",) if it.ref else (("on",) if it.path in self.checked else (band,))

    def _refresh_row(self, iid):
        it = self.iid2item[iid]
        self.tree.set(iid, "check", self._mark(it))
        self.tree.item(iid, tags=self._tags(it, self.iid2band.get(iid, "b0")))

    def _group_of(self, it):
        for g in self.groups:
            if it in g:
                return g
        return []

    # 체크
    def on_click(self, e):
        iid = self.tree.identify_row(e.y)
        if iid in self.iid2item and self.tree.identify_column(e.x) == "#2":   # 체크 칸만
            self.toggle(iid)

    def toggle(self, iid):
        it = self.iid2item.get(iid)
        if not it or it.ref:
            return
        if it.path in self.checked:
            self.checked.discard(it.path)
        else:
            g = self._group_of(it)
            if all(x.path in self.checked for x in g if x is not it and not x.ref) and not any(x.ref for x in g):
                return messagebox.showwarning("안내", "그룹의 모든 파일을 체크할 수는 없어요. 최소 1개는 남겨야 합니다.")
            self.checked.add(it.path)
        self._refresh_row(iid); self._summary()

    def mark_pos(self, where):
        if not self.groups:
            return
        self.checked = mark_position(self.groups, where)
        self.render()
        self.status.config(text=f"그룹마다 {'첫 번째' if where == 'first' else '마지막'} 파일을 체크했어요. (기준폴더 파일은 제외)")

    def auto(self):
        if not self.groups:
            return
        self.checked = auto_mark(self.groups, self.rule.get(), self.prefer.get().strip())
        self.render()

    def auto_ref(self):
        if not self.groups:
            return
        if not any(it.ref for g in self.groups for it in g):
            return messagebox.showinfo("안내", "결과에 기준폴더 파일이 없어요.\n폴더 역할을 '기준'으로 바꾸고 다시 검색하세요.")
        self.checked = mark_by_reference(self.groups)
        self.render()
        n_ref = sum(1 for g in self.groups if any(x.ref for x in g))
        self.status.config(text=f"기준폴더와 겹치는 {n_ref:,}개 그룹에서 기준폴더 밖 파일 {len(self.checked):,}개를 체크했어요.")

    def uncheck_all(self):
        self.checked.clear(); self.render()

    def _summary(self):
        size = {it.path: it.size for g in self.groups for it in g}
        s = sum(size.get(p, 0) for p in self.checked)
        self.btn_del.config(text=f"🗑  체크한 파일 정리  ({len(self.checked):,}개 · {human(s)})")
        self.lbl_count.config(text=f"중복 그룹 {len(self.groups):,}개")
        self._update_tabs()

    def open_folder(self, e):
        it = self.iid2item.get(self.tree.identify_row(e.y))
        if it:
            reveal_in_explorer(it.path)

    # 오른쪽 클릭 메뉴
    def on_right_click(self, e):
        iid = self.tree.identify_row(e.y)
        it = self.iid2item.get(iid)
        if not it:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        targets = [self.iid2item[i] for i in self.tree.selection() if i in self.iid2item] or [it]
        m = tk.Menu(self, tearoff=0, bg=T["card"], fg=T["text"], activebackground=T["sel"],
                    activeforeground=T["text"], font=fnt(9), bd=1, relief="solid")
        m.add_command(label="📂  폴더 열기 (이 파일 선택된 채로)", command=lambda: reveal_in_explorer(it.path))
        m.add_command(label="▶  파일 열기", command=lambda: self._open_file(it.path))
        m.add_command(label="📋  경로 복사", command=lambda: self._copy_paths(targets))
        m.add_separator()
        can = [x for x in targets if not x.ref]
        m.add_command(label=f"☑  체크  ({len(can)}개)", command=lambda: self._set_checked(can, True),
                      state="normal" if can else "disabled")
        m.add_command(label=f"☐  체크 해제  ({len(can)}개)", command=lambda: self._set_checked(can, False),
                      state="normal" if can else "disabled")
        m.add_command(label="★  이 그룹에서 이 파일만 남기기", command=lambda: self._keep_only(it))
        m.add_separator()
        m.add_command(label="🔒  이 폴더를 기준폴더로 지정", command=lambda: self._make_ref_folder(it.path))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _open_file(self, path):
        try:
            if sys.platform == "win32":
                os.startfile(path)
        except OSError as ex:
            messagebox.showwarning("안내", f"파일을 열 수 없어요.\n{ex}")

    def _copy_paths(self, items):
        self.clipboard_clear()
        self.clipboard_append("\n".join(x.path for x in items))
        self.status.config(text=f"경로 {len(items)}개를 복사했어요.")

    def _set_checked(self, items, on):
        for x in items:
            if on:
                g = self._group_of(x)
                others = [y for y in g if y is not x and not y.ref]
                if not any(y.ref for y in g) and all(y.path in self.checked for y in others):
                    continue                            # 그룹에 최소 1개는 남김
                self.checked.add(x.path)
            else:
                self.checked.discard(x.path)
        for iid, x in self.iid2item.items():
            if x in items:
                self._refresh_row(iid)
        self._summary()

    def _keep_only(self, it):
        g = self._group_of(it)
        for y in g:
            if y.ref:
                continue
            if y is it:
                self.checked.discard(y.path)
            else:
                self.checked.add(y.path)
        for iid, x in self.iid2item.items():
            if x in g:
                self._refresh_row(iid)
        self._summary()
        self.status.config(text=f"'{os.path.basename(it.path)}'만 남기고 같은 그룹의 나머지를 체크했어요.")

    def _make_ref_folder(self, path):
        d = os.path.normpath(os.path.dirname(path))
        for i in self.ftree.get_children():
            p, r = self.ftree.item(i, "values")
            if os.path.normcase(p) == os.path.normcase(d):
                self.ftree.item(i, values=(p, "기준"), tags=("기준",))
                break
        else:
            self._add_folder_row(d, "기준")
        self.status.config(text=f"'{d}'를 기준폴더로 지정했어요. 다시 검색하면 반영돼요.")

    # 정리
    def process(self):
        if not self.checked:
            return messagebox.showinfo("안내", "체크된 파일이 없어요.")
        mode, dest = self.mode.get(), None
        if mode == "folder":
            dest = filedialog.askdirectory(title="중복 파일을 옮길 격리 폴더 선택")
            if not dest:
                return
        what = {"trash": "휴지통으로 이동", "folder": f"'{dest}'(으)로 이동",
                "link": "하드링크로 대체 (파일은 그대로 보이지만 용량은 1개분만 차지)"}[mode]
        if not messagebox.askyesno("확인", f"체크한 {len(self.checked):,}개 파일을 {what}할까요?"):
            return
        os.makedirs(LOG_DIR, exist_ok=True)
        log_path = os.path.join(LOG_DIR, time.strftime("정리기록_%Y%m%d_%H%M%S.csv"))
        roots = [p for p, r in self.folders() if r != "제외"]
        ok, fail, rows = 0, [], []
        todo = [(g, it) for g in self.groups for it in g if it.path in self.checked]
        for n, (g, it) in enumerate(todo, 1):
            keep = next((x for x in g if x.path not in self.checked), None)
            try:
                st = os.stat(it.path)
                if st.st_size != it.size or abs(st.st_mtime - it.mtime) > 2:
                    raise OSError("검색 이후 파일이 수정됨 — 건너뜀")
                if keep is None or not os.path.exists(keep.path) or os.path.getsize(keep.path) != it.size:
                    raise OSError("남길 파일이 없어지거나 바뀜 — 건너뜀")
                if mode == "trash":
                    res = send_to_trash(it.path)
                elif mode == "folder":
                    res = move_to_folder(it.path, dest)
                else:
                    res = replace_with_hardlink(it.path, keep.path)
                rows.append(["완료", it.path, res, keep.path]); ok += 1
            except Exception as ex:
                rows.append(["실패", it.path, str(ex), keep.path if keep else ""]); fail.append(f"{it.path} ({ex})")
            if n % 20 == 0:
                self.status.config(text=f"정리 중... {n:,}/{len(todo):,}")
                self.pb["value"] = n / len(todo) * 100; self.update()
        with open(log_path, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.writer(fp); w.writerow(["결과", "정리한 파일", "처리 내용/이동 위치", "남긴 파일"]); w.writerows(rows)

        done = {r[1] for r in rows if r[0] == "완료"}
        if mode == "link":
            self.groups = [g for g in self.groups if not all(x.path in done or x is g[0] for x in g)]
        else:
            self.groups = [[x for x in g if x.path not in done] for g in self.groups]
        self.groups = [g for g in self.groups if len(g) > 1 and not all(x.ref for x in g)]
        self.checked -= done
        freed = sum(it.size for g in [x for x in todo] for it in [g[1]] if it.path in done)
        self.freed_total += freed
        try:
            self.lbl_freed.config(text=human(self.freed_total))
        except tk.TclError:
            pass
        self._pv_item = None
        self.render()
        msg = f"{ok:,}개 정리 완료 — {human(freed)} 확보!\n기록: {log_path}"
        if fail:
            msg += f"\n\n실패/건너뜀 {len(fail)}개:\n" + "\n".join(fail[:8])
        messagebox.showinfo("결과", msg)
        self.status.config(text=f"정리 완료 {ok:,}개 / 실패 {len(fail)}개")

    def export(self):
        if not self.groups:
            return
        f = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if not f:
            return
        with open(f, "w", newline="", encoding="utf-8-sig") as fp:
            w = csv.writer(fp); w.writerow(["그룹", "경로", "크기(바이트)", "수정일", "기준폴더", "체크"])
            for gi, g in enumerate(self.groups, 1):
                for it in g:
                    w.writerow([gi, it.path, it.size, time.strftime("%Y-%m-%d %H:%M", time.localtime(it.mtime)),
                                "Y" if it.ref else "", "정리" if it.path in self.checked else ""])
        messagebox.showinfo("저장", "CSV로 저장했어요.")


if __name__ == "__main__":
    load_private_fonts()
    App().mainloop()
