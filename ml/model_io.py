"""Safe persistence helpers for ML artifacts.

joblib / pickle deserialization is intrinsically unsafe — loading a
crafted .pkl file can execute arbitrary code. We can't eliminate that
risk while we still use joblib, but we can confine the attack surface
by refusing to load any file that resolves outside ``ml/models/``.

``atomic_dump_model`` writes via a temporary sibling and an
``os.replace`` so a crash mid-write (disk full, OOM) never leaves the
real artifact half-written. joblib.dump writes incrementally, so
without this an interrupted nightly run would silently poison the
next EntryFilter load.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

MODEL_PATH = Path(__file__).parent / "models"


def safe_load_model(
    model_file: Path,
    allowed_root: Optional[Path] = None,
) -> Optional[Any]:
    """Load a joblib/pickle model from ``ml/models/`` only.

    ``allowed_root`` defaults to ``ml/models/`` — pass the caller's own
    module-level MODEL_PATH when you want tests to be able to swap the
    directory with monkeypatch without also having to reach into
    ml.model_io. Production callers don't need to set it.

    Returns ``None`` when:
      * the resolved path is outside ``allowed_root`` (path traversal / symlink),
      * the file does not exist,
      * joblib.load raises (missing dep, corrupt file, unpickleable payload).

    Callers treat ``None`` as "no usable model" and fall back to their
    baseline behaviour — mirroring the fail-open contract of EntryFilter
    and detect_current_regime.
    """
    try:
        resolved = Path(model_file).resolve()
        model_root = Path(allowed_root if allowed_root is not None else MODEL_PATH).resolve()

        # is_relative_to lands false for symlinks that escape the dir too —
        # Path.resolve() dereferences them before the comparison.
        if not resolved.is_relative_to(model_root):
            logger.error("Model path outside %s: %s", model_root, resolved)
            return None

        if not resolved.exists():
            return None

        import joblib
        return joblib.load(resolved)

    except Exception as e:
        logger.warning("Failed to load model %s: %s", model_file, str(e)[:200])
        return None


def atomic_dump_model(model: Any, model_file: Path) -> None:
    """Persist ``model`` to ``model_file`` atomically.

    Writes to ``<model_file>.tmp`` first, then ``os.replace`` into place.
    If joblib.dump raises the tmp file is cleaned up so a failed run
    doesn't leave .tmp droppings around ``ml/models/``.

    Raises whatever joblib.dump raises — this is a training-time helper,
    not a fail-open path. The caller (nightly_pipeline) is expected to
    log and move on.
    """
    model_file = Path(model_file)
    tmp_file = model_file.with_suffix(model_file.suffix + ".tmp")

    import joblib

    try:
        joblib.dump(model, tmp_file)
        os.replace(tmp_file, model_file)
    except Exception:
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except Exception:
                pass
        raise
