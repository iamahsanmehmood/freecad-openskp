"""Benchmark textured material imports and saved .FCStd sizes (#30).

Run headlessly with:
    freecadcmd tests/bench_textures.py

Or measure FreeCAD's real GUI (ViewProviders + Coin3D SoTexture2 scene graph + Tree)
under xvfb:
    BENCH_LAUNCHER="xvfb-run -a squashfs-root/AppRun" freecadcmd tests/bench_textures.py

Measures two synthetic models built with openskp's writer:
1. ``200_materials``: 200 distinct textured materials (200 unique ~1.0 MB 512x512 PNG
   images) across 20 component definitions (10 faces each = 200 textured faces).
2. ``2000_placements``: 2,000 placements of 1 textured 6-face component (128x128 PNG
   image) in the default component hierarchy layout.

Each case runs in a fresh subprocess with ``textures=False`` (off) and ``textures=True``
(on), recording import wall time, RSS delta / peak RSS, and saved ``.FCStd`` file size.
"""
from __future__ import annotations

import json
import os
import shlex
import struct
import subprocess
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _harness  # noqa: E402
import importSKP  # noqa: E402


def _proc_status_mb():
    """Returns ``(vm_rss_mb, vm_hwm_mb)`` on Linux from ``/proc/self/status``."""
    rss_kb = 0
    hwm_kb = 0
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    hwm_kb = int(line.split()[1])
    except OSError:
        pass
    return rss_kb / 1024.0, hwm_kb / 1024.0


def _make_textured_png(width: int, height: int, seed: int) -> bytes:
    """Deterministic procedural brick/grain PNG (~2-4 KB) unique per ``seed``."""
    rows = []
    s = (seed * 2654435761 + 1013904223) & 0xFFFFFFFF
    for y in range(height):
        row = bytearray([0])
        mortar_y = (y % 16) == 0
        shift = 16 if ((y // 16) % 2) else 0
        for x in range(width):
            s = (s * 1664525 + 1013904223) & 0xFFFFFFFF
            noise = (s >> 24) & 0x1F
            mortar = mortar_y or (((x + shift) % 32) == 0)
            if mortar:
                r, g, b = 185 + (noise // 3), 182 + (noise // 3), 176 + (noise // 3)
            else:
                base_r = 90 + ((seed * 37) % 130)
                base_g = 60 + ((seed * 73) % 130)
                base_b = 50 + ((seed * 19) % 130)
                r = min(255, base_r + noise)
                g = min(255, base_g + (noise // 2))
                b = min(255, base_b + (noise // 3))
            row.extend((r, g, b, 255))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def ck(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", ihdr) + ck(b"IDAT", zlib.compress(raw, 6)) + ck(b"IEND", b"")


def _make_1mb_textured_png(seed: int) -> bytes:
    """Deterministic ~1.0 MB (512x512 RGBA8888, uncompressed deflate) PNG unique per ``seed``."""
    import numpy as np

    w, h = 512, 512
    yy = np.arange(h, dtype=np.uint32)[:, None]
    xx = np.arange(w, dtype=np.uint32)[None, :]
    s_u32 = np.uint32((seed * 2654435761) & 0xFFFFFFFF)
    hv = (xx * np.uint32(1664525)) ^ (yy * np.uint32(1013904223)) ^ s_u32
    buf = np.empty((h, 1 + w * 4), dtype=np.uint8)
    buf[:, 0] = 0
    px = buf[:, 1:].reshape((h, w, 4))
    mortar = ((yy % 32) == 0) | (((xx + ((yy // 32) % 2) * 32) % 64) == 0)
    noise = ((hv >> 24) & np.uint32(0x3F)).astype(np.int16)
    base_r = 60 + ((seed * 37) % 130)
    base_g = 45 + ((seed * 73) % 130)
    base_b = 40 + ((seed * 19) % 130)
    px[..., 0] = np.where(mortar, np.clip(180 + noise // 3, 0, 255), np.clip(base_r + noise, 0, 255)).astype(np.uint8)
    px[..., 1] = np.where(mortar, np.clip(177 + noise // 3, 0, 255), np.clip(base_g + noise // 2, 0, 255)).astype(np.uint8)
    px[..., 2] = np.where(mortar, np.clip(172 + noise // 3, 0, 255), np.clip(base_b + noise // 3, 0, 255)).astype(np.uint8)
    px[..., 3] = 255
    raw = buf.tobytes()

    def ck(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", ihdr) + ck(b"IDAT", zlib.compress(raw, 0)) + ck(b"IEND", b"")


def build_200_materials_skp(skp_path: str, work_dir: str) -> int:
    import openskp

    prev = os.getcwd()
    try:
        os.chdir(work_dir)
        b = openskp.create()
        mats = []
        for i in range(200):
            fn = f"mat_{i:03d}.png"
            png_bytes = _make_1mb_textured_png(i + 1)
            with open(fn, "wb") as fh:
                fh.write(png_bytes)
            mats.append(b.add_texture_material(f"TexMat_{i:03d}", fn, applied_width=12.0, applied_height=12.0))
            os.remove(fn)
        comps = []
        for c_idx in range(20):
            with b.add_component_definition(f"PanelSet_{c_idx:02d}") as comp:
                for f_idx in range(10):
                    m_handle = mats[c_idx * 10 + f_idx]
                    x0 = f_idx * 14.0
                    comp.add_face(
                        [(x0, 0.0, 0.0), (x0 + 12.0, 0.0, 0.0), (x0 + 12.0, 12.0, 0.0), (x0, 12.0, 0.0)],
                        material=m_handle,
                    )
            comps.append(comp)
        for c_idx, comp in enumerate(comps):
            b.add_instance(comp, name=f"Row_{c_idx:02d}", translation=(0.0, c_idx * 16.0, 0.0))
        raw = b.to_bytes()
    finally:
        os.chdir(prev)
    with open(skp_path, "wb") as fh:
        fh.write(raw)
    return len(raw)


def build_2000_placements_skp(skp_path: str, work_dir: str) -> int:
    import openskp

    prev = os.getcwd()
    try:
        os.chdir(work_dir)
        with open("column_tex.png", "wb") as fh:
            fh.write(_make_textured_png(128, 128, 777))
        b = openskp.create()
        tmat = b.add_texture_material("ColumnBrick", "column_tex.png", applied_width=8.0, applied_height=8.0)
        with b.add_component_definition("TexturedColumn") as col:
            w, d, h = 8.0, 8.0, 24.0
            col.add_face([(0, 0, 0), (0, d, 0), (w, d, 0), (w, 0, 0)], material=tmat)
            col.add_face([(0, 0, h), (w, 0, h), (w, d, h), (0, d, h)], material=tmat)
            col.add_face([(0, 0, 0), (w, 0, 0), (w, 0, h), (0, 0, h)], material=tmat)
            col.add_face([(0, d, 0), (0, d, h), (w, d, h), (w, d, 0)], material=tmat)
            col.add_face([(0, 0, 0), (0, 0, h), (0, d, h), (0, d, 0)], material=tmat)
            col.add_face([(w, 0, 0), (w, d, 0), (w, d, h), (w, 0, h)], material=tmat)
        for idx in range(2000):
            b.add_instance(
                col,
                name=f"Col_{idx:04d}",
                translation=((idx % 50) * 14.0, (idx // 50) * 14.0, 0.0),
            )
        raw = b.to_bytes()
    finally:
        os.chdir(prev)
    with open(skp_path, "wb") as fh:
        fh.write(raw)
    return len(raw)


def _run_single_case(skp_path: str, textures_on: bool, fcstd_path: str) -> dict:
    import FreeCAD as App

    if App.GuiUp:
        import FreeCADGui as Gui

        Gui.updateGui()

    doc = App.newDocument("BenchTextures")
    rss_before, _ = _proc_status_mb()
    t0 = time.perf_counter()
    importSKP.import_skp(skp_path, doc, merge=False, textures=textures_on)
    if App.GuiUp:
        import FreeCADGui as Gui

        gui_doc = Gui.getDocument(doc.Name)
        if gui_doc is not None and gui_doc.activeView() is not None:
            gui_doc.activeView().viewIsometric()
            gui_doc.activeView().fitAll()
        Gui.updateGui()
    elapsed = time.perf_counter() - t0
    rss_after, hwm_after = _proc_status_mb()

    t_save = time.perf_counter()
    doc.saveAs(fcstd_path)
    save_s = time.perf_counter() - t_save
    fcstd_bytes = os.path.getsize(fcstd_path)
    try:
        os.remove(fcstd_path)
    except OSError:
        pass

    stats = importSKP.last_import_stats
    by_type = {}
    for obj in doc.Objects:
        by_type[obj.TypeId] = by_type.get(obj.TypeId, 0) + 1
    res = {
        "textures": textures_on,
        "gui": bool(App.GuiUp),
        "elapsed_s": round(elapsed, 3),
        "save_s": round(save_s, 3),
        "rss_before_mb": round(rss_before, 1),
        "rss_after_mb": round(rss_after, 1),
        "rss_delta_mb": round(rss_after - rss_before, 1),
        "peak_rss_mb": round(hwm_after, 1),
        "fcstd_kb": round(fcstd_bytes / 1024.0, 1),
        "doc_objects": len(doc.Objects),
        "textured_objects": stats.get("textured_objects", 0),
        "textures_loaded": stats.get("textures_loaded", 0),
        "by_type": by_type,
    }
    App.closeDocument(doc.Name)
    return res


def _main():
    import FreeCAD as App

    worker_cfg = os.environ.get("BENCH_TEX_WORKER_CFG")
    if worker_cfg:
        cfg = json.loads(worker_cfg)
        try:
            res = _run_single_case(cfg["skp_path"], cfg["textures"], cfg["fcstd_path"])
            with open(cfg["out_file"], "w", encoding="utf-8") as fh:
                json.dump({"ok": True, "result": res}, fh)
        except BaseException as exc:
            import traceback

            with open(cfg["out_file"], "w", encoding="utf-8") as fh:
                json.dump({"ok": False, "error": f"{exc}\n{traceback.format_exc()}"}, fh)
            if App.GuiUp:
                os._exit(1)
            raise
        if App.GuiUp:
            os._exit(0)
        return

    launcher = shlex.split(os.environ.get("BENCH_LAUNCHER", sys.executable))
    timeout_s = int(os.environ.get("BENCH_TIMEOUT", "360"))
    script = os.path.abspath(__file__)
    rows = []

    with tempfile.TemporaryDirectory() as tmp:
        img_dir = os.path.join(tmp, "imgs")
        os.makedirs(img_dir, exist_ok=True)
        scenarios = [
            ("200_materials", build_200_materials_skp),
            ("2000_placements", build_2000_placements_skp),
        ]
        for sc_name, builder_fn in scenarios:
            skp_path = os.path.join(tmp, f"{sc_name}.skp")
            skp_bytes = builder_fn(skp_path, img_dir)
            for tex_on in (False, True):
                out_file = os.path.join(tmp, f"res_{sc_name}_{int(tex_on)}.json")
                fcstd_path = os.path.join(tmp, f"saved_{sc_name}_{int(tex_on)}.FCStd")
                env = dict(os.environ)
                env["BENCH_TEX_WORKER_CFG"] = json.dumps(
                    {
                        "skp_path": skp_path,
                        "textures": tex_on,
                        "fcstd_path": fcstd_path,
                        "out_file": out_file,
                    }
                )
                proc = subprocess.run(
                    [*launcher, script],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
                if not os.path.isfile(out_file):
                    raise RuntimeError(
                        f"worker wrote no result (rc={proc.returncode}, {sc_name}, textures={tex_on}):\n"
                        f"{proc.stdout}\n{proc.stderr}"
                    )
                payload = json.loads(open(out_file, encoding="utf-8").read())
                if not payload.get("ok"):
                    raise RuntimeError(f"worker error ({sc_name}, textures={tex_on}):\n{payload.get('error')}")
                data = payload["result"]
                data["scenario"] = sc_name
                data["skp_kb"] = round(skp_bytes / 1024.0, 1)
                rows.append(data)
                print("BENCH_TEX_ROW:" + json.dumps(data), flush=True)

    print("\n=== TEXTURE BENCHMARK SUMMARY ===", flush=True)
    for r in rows:
        mode = "GUI" if r["gui"] else "headless"
        ton = "on " if r["textures"] else "off"
        print(
            f"[{mode:8s}] {r['scenario']:16s} (.skp {r['skp_kb']:6.1f} KB) | textures={ton} | "
            f"time={r['elapsed_s']:6.3f}s | +RSS={r['rss_delta_mb']:5.1f} MB (peak {r['peak_rss_mb']:6.1f} MB) | "
            f".FCStd={r['fcstd_kb']:6.1f} KB | objs={r['doc_objects']} (tex_objs={r['textured_objects']}, imgs={r['textures_loaded']})",
            flush=True,
        )


_harness.run(_main)
