import os
from typing import Optional


def detect_device() -> str:
    """Detect available device and return 'cuda', 'mps', or 'cpu'."""
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


def resolve_device(cfg_device: Optional[str] = None) -> str:
    """
    Decide which device to use.
    Priority: env COUNT_CAR_DEVICE > cfg_device (if not auto) > auto-detect.
    Sets COUNT_CAR_DEVICE env var for downstream use.
    """
    env_dev = os.environ.get("COUNT_CAR_DEVICE")
    if env_dev:
        return env_dev

    if cfg_device and cfg_device.lower() != "auto":
        os.environ["COUNT_CAR_DEVICE"] = cfg_device
        return cfg_device

    detected = detect_device()
    os.environ["COUNT_CAR_DEVICE"] = detected
    return detected
