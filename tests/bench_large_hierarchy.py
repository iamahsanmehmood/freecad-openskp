"""Benchmark large nested SketchUp models in both FreeCAD import layouts (#31).

Run headlessly with:
    freecadcmd tests/bench_large_hierarchy.py

Or measure the real GUI (ViewProviders + 3D scene graph + Tree view) under xvfb:
    BENCH_LAUNCHER="xvfb-run -a squashfs-root/AppRun" freecadcmd tests/bench_large_hierarchy.py

Builds synthetic nested .skp models with openskp's writer at 100, 1,000 and
5,000 component definitions (80% 6-face box leaf parts, 10% sub-assemblies
placing 4 leaves + 1 base face, 10% top-level bays placing 2 sub-assemblies +
1 leaf across 4 tags) and measures import wall time, RSS memory delta / peak
RSS, and FreeCAD document object counts in fresh subprocesses for both
``merge=True`` (merged per-tag compounds) and ``merge=False`` (component
hierarchy).
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _harness  # noqa: E402
import importSKP  # noqa: E402


def _proc_status_mb():
    """Returns ``(vm_rss_mb, vm_hwm_mb)`` on Linux from ``/proc/self/status``."""
    rss_kb = 0
    hwm_kb = 0
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss_kb = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    hwm_kb = int(line.split()[1])
    except OSError:
        pass
    return rss_kb / 1024.0, hwm_kb / 1024.0


def build_nested_skp(path: str, total_defs: int) -> int:
    """Writes a nested synthetic .skp with ``total_defs`` definitions and
    returns its byte size."""
    import openskp

    b = openskp.create()
    tags = [b.add_layer(f"Tag_{i}") for i in range(4)]
    n_leaves = int(total_defs * 0.8)
    n_mid = (total_defs - n_leaves) // 2
    n_top = total_defs - n_leaves - n_mid

    leaves = []
    for i in range(n_leaves):
        w = 2.0 + (i % 7) * 0.5
        d = 2.0 + (i % 5) * 0.5
        h = 12.0 + (i % 11)
        with b.add_component_definition(f"Part_{i}") as comp:
            comp.add_face([(0, 0, 0), (0, d, 0), (w, d, 0), (w, 0, 0)])
            comp.add_face([(0, 0, h), (w, 0, h), (w, d, h), (0, d, h)])
            comp.add_face([(0, 0, 0), (w, 0, 0), (w, 0, h), (0, 0, h)])
            comp.add_face([(0, d, 0), (0, d, h), (w, d, h), (w, d, 0)])
            comp.add_face([(0, 0, 0), (0, 0, h), (0, d, h), (0, d, 0)])
            comp.add_face([(w, 0, 0), (w, d, 0), (w, d, h), (w, 0, h)])
        leaves.append(comp)

    mids = []
    used_leaves = set()
    for j in range(n_mid):
        with b.add_component_definition(f"SubAssembly_{j}") as sub:
            sub.add_face([(0, 0, 0), (20, 0, 0), (20, 10, 0), (0, 10, 0)])
            for k in range(4):
                leaf_idx = (j * 4 + k) % n_leaves
                used_leaves.add(leaf_idx)
                sub.add_instance(
                    leaves[leaf_idx],
                    name=f"SubPart_{k}",
                    translation=(k * 5.0, 0.0, 0.0),
                )
        mids.append(sub)

    tops = []
    for t in range(n_top):
        leaf_idx = (t * 3) % n_leaves
        used_leaves.add(leaf_idx)
        with b.add_component_definition(f"Bay_{t}") as bay:
            bay.add_instance(mids[t % n_mid], name="LeftSub", translation=(0.0, 0.0, 0.0))
            bay.add_instance(mids[(t + 1) % n_mid], name="RightSub", translation=(25.0, 0.0, 0.0))
            bay.add_instance(leaves[leaf_idx], name="Tie", translation=(12.0, 5.0, 0.0))
        tops.append(bay)

    for idx, bay in enumerate(tops):
        b.add_instance(
            bay,
            name=f"BayInst_{idx}",
            translation=((idx % 25) * 60.0, (idx // 25) * 30.0, 0.0),
            layer=tags[idx % 4],
        )
    for idx in range(n_leaves):
        if idx not in used_leaves:
            b.add_instance(
                leaves[idx],
                name=f"LoosePart_{idx}",
                translation=((idx % 50) * 10.0, -40.0 - (idx // 50) * 10.0, 0.0),
                layer=tags[idx % 4],
            )

    raw = b.to_bytes()
    with open(path, "wb") as f:
        f.write(raw)
    return len(raw)


def _run_single_case(skp_path: str, merge: bool) -> dict:
    import FreeCAD as App

    if App.GuiUp:
        import FreeCADGui as Gui

        Gui.updateGui()

    doc = App.newDocument("Bench")
    rss_before, _ = _proc_status_mb()
    t0 = time.perf_counter()
    importSKP.import_skp(skp_path, doc, merge=merge)
    if App.GuiUp:
        import FreeCADGui as Gui

        Gui.updateGui()
    elapsed = time.perf_counter() - t0
    rss_after, hwm_after = _proc_status_mb()
    stats = importSKP.last_import_stats
    by_type = {}
    for obj in doc.Objects:
        by_type[obj.TypeId] = by_type.get(obj.TypeId, 0) + 1
    result = {
        "merge": merge,
        "gui": bool(App.GuiUp),
        "elapsed_s": round(elapsed, 2),
        "rss_before_mb": round(rss_before, 1),
        "rss_after_mb": round(rss_after, 1),
        "rss_delta_mb": round(rss_after - rss_before, 1),
        "peak_rss_mb": round(hwm_after, 1),
        "doc_objects": len(doc.Objects),
        "by_type": by_type,
        "faces_built": stats.get("faces_built", 0),
        "placements": stats.get("placements", 0),
        "components": stats.get("components", 0),
        "links": stats.get("links", 0),
    }
    App.closeDocument(doc.Name)
    return result


def _main():
    import FreeCAD as App

    worker_cfg = os.environ.get("BENCH_WORKER_CFG")
    if worker_cfg:
        cfg = json.loads(worker_cfg)
        try:
            res = _run_single_case(cfg["path"], cfg["merge"])
            with open(cfg["out_file"], "w", encoding="utf-8") as f:
                json.dump({"ok": True, "result": res}, f)
        except BaseException as exc:
            import traceback

            with open(cfg["out_file"], "w", encoding="utf-8") as f:
                json.dump({"ok": False, "error": f"{exc}\n{traceback.format_exc()}"}, f)
            if App.GuiUp:
                os._exit(1)
            raise
        if App.GuiUp:
            os._exit(0)
        return

    sizes = [int(x) for x in os.environ.get("BENCH_SIZES", "100,1000,5000").split(",")]
    launcher = shlex.split(os.environ.get("BENCH_LAUNCHER", sys.executable))
    timeout_s = int(os.environ.get("BENCH_TIMEOUT", "240"))
    script = os.path.abspath(__file__)
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for n in sizes:
            skp_path = os.path.join(tmp, f"nested_{n}.skp")
            t_w = time.perf_counter()
            file_bytes = build_nested_skp(skp_path, n)
            write_s = round(time.perf_counter() - t_w, 2)
            for merge in (True, False):
                out_file = os.path.join(tmp, f"res_{n}_{int(merge)}.json")
                env = dict(os.environ)
                env["BENCH_WORKER_CFG"] = json.dumps(
                    {"path": skp_path, "merge": merge, "out_file": out_file}
                )
                try:
                    proc = subprocess.run(
                        [*launcher, script],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=timeout_s,
                    )
                except subprocess.TimeoutExpired:
                    data = {
                        "defs": n,
                        "skp_kb": round(file_bytes / 1024.0, 1),
                        "write_s": write_s,
                        "merge": merge,
                        "gui": "AppRun" in " ".join(launcher),
                        "elapsed_s": f">{timeout_s}",
                        "rss_delta_mb": None,
                        "peak_rss_mb": None,
                        "doc_objects": None,
                        "by_type": {"timed_out": True},
                    }
                    rows.append(data)
                    print("BENCH_ROW:" + json.dumps(data), flush=True)
                    continue
                if not os.path.exists(out_file):
                    raise RuntimeError(
                        f"worker wrote no result (rc={proc.returncode}, n={n}, merge={merge}):\n"
                        f"{proc.stdout}\n{proc.stderr}"
                    )
                with open(out_file, encoding="utf-8") as f:
                    payload = json.load(f)
                if not payload.get("ok"):
                    raise RuntimeError(f"worker error (n={n}, merge={merge}):\n{payload.get('error')}")
                data = payload["result"]
                data["defs"] = n
                data["skp_kb"] = round(file_bytes / 1024.0, 1)
                data["write_s"] = write_s
                rows.append(data)
                print("BENCH_ROW:" + json.dumps(data), flush=True)

    print("\n=== SUMMARY TABLE ===", flush=True)
    for r in rows:
        layout = "merged" if r["merge"] else "hierarchy"
        mode = "GUI" if r["gui"] else "headless"
        print(
            f"[{mode}] defs={r['defs']:5d} | {layout:9s} | time={r['elapsed_s']!s:>7s}s | "
            f"+RSS={r['rss_delta_mb']!s:>6s} MB (peak {r['peak_rss_mb']!s:>6s} MB) | "
            f"objects={r['doc_objects']!s:>6s} | types={r['by_type']}",
            flush=True,
        )


_harness.run(_main)
