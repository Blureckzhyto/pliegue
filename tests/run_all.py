"""Corre todas las suites (núcleo e interfaz) una tras otra con Python 3.11.

    python tests\\run_all.py [filtro]
    python tests\\run_all.py --exe dist\\Pliegue\\Pliegue.exe [filtro]

Con --exe, cada suite corre DENTRO de la app compilada (Pliegue.exe --run-script): mismo
Python, bibliotecas y código empaquetados que usa el usuario.

Cada suite imprime su resumen "N/M OK"; aquí se junta todo. Cada suite corre con su propia
carpeta de datos temporal (PLIEGUE_DATA_DIR) y sin datos viejos que migrar
(PLIEGUE_LEGACY_DIRS vacío): nunca toca la configuración ni los registros del usuario.
`verify_frozen_v131.py` (el exe) y `tools/release_check.py` van aparte: ver README.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SUITES = [
    "regression_existing", "core_editsign_test", "core_new_tools_test", "editor_core_test",
    "core_v131_test", "editor_v132_test", "editor_v134_test", "editor_v135_test", "editor_v141_test",
    "gui_v12_test", "gui_v13_editor_test", "gui_v131_test", "gui_v132_test",
    "gui_v133_nombre_test", "gui_v134_test", "gui_v136_test", "gui_v137_editor_test", "gui_v141_test", "gui_about_test",
    "migration_test",
]


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    exe = None
    if "--exe" in argv:
        i = argv.index("--exe")
        exe = Path(argv[i + 1]).resolve()
        argv = argv[:i] + argv[i + 2:]
    wanted = [s for s in SUITES if not argv or any(a in s for a in argv)]
    sandbox = Path(tempfile.mkdtemp(prefix="pliegue_pruebas_"))
    summary = []
    for suite in wanted:
        start = time.time()
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PLIEGUE_DATA_DIR=str(sandbox / suite),
                   PLIEGUE_LEGACY_DIRS="")
        try:
            if exe is None:
                proc = subprocess.run([sys.executable, str(HERE / f"{suite}.py")], cwd=HERE, env=env,
                                      capture_output=True, text=True, encoding="utf-8", errors="replace",
                                      timeout=900)
                stdout = proc.stdout
            else:
                out = HERE / "_capturas" / f"exe_{suite}.txt"
                out.parent.mkdir(exist_ok=True)
                proc = subprocess.run([str(exe), "--run-script", str(HERE / f"{suite}.py"), "--salida", str(out)],
                                      cwd=HERE, env=env, capture_output=True, text=True, timeout=900)
                stdout = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
                proc.stderr = proc.stderr or ""
            lines = [l for l in stdout.splitlines() if l.strip()]
            fails = [l for l in lines if l.lstrip().startswith("FAIL")]
            last = lines[-1] if lines else (proc.stderr.strip().splitlines() or ["(sin salida)"])[-1]
            ok = proc.returncode == 0
        except subprocess.TimeoutExpired:
            fails, last, ok = [], "se pasó de 15 minutos", False
        summary.append((suite, ok))
        print(f"{'OK   ' if ok else 'FALLA'} {suite:<24} {time.time() - start:6.0f} s  {last}", flush=True)
        for line in fails:
            print("      " + line.strip(), flush=True)
    bad = [s for s, ok in summary if not ok]
    print(f"\n{len(summary) - len(bad)}/{len(summary)} suites OK" + (f" — fallan: {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
