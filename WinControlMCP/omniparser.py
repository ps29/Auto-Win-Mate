"""Optional: find clickable regions in a screenshot with OmniParser V2's icon_detect_v3 (MIT).

For apps that expose no UI Automation elements (games, custom-drawn apps, remote desktops).
Needs torch and the model weights; setup_omniparser.cmd installs both. The decoding follows
the model card at huggingface.co/microsoft/OmniParser-v2.0.
"""
import importlib.util
import os
import warnings
from pathlib import Path

MODEL = Path(os.environ.get("WINCONTROL_OMNIPARSER_MODEL",
                            Path(__file__).resolve().parent / "omniparser" / "icon_detect_v3" / "model.pt"))
IMGSZ, STRIDES = 1280, (8, 16, 32)
CONF_THRESHOLD, IOU_THRESHOLD = 0.05, 0.45
_model = None


def available():
    return MODEL.exists() and importlib.util.find_spec("torch") is not None


def _nms(boxes, scores, iou):
    try:
        from torchvision.ops import nms
        return nms(boxes, scores, iou)
    except ImportError:  # plain-torch fallback, fine for a few hundred boxes
        import torch
        order, keep = scores.argsort(descending=True), []
        while order.numel():
            i = order[0]
            keep.append(i)
            if order.numel() == 1:
                break
            rest = boxes[order[1:]]
            lt = torch.maximum(boxes[i, :2], rest[:, :2])
            rb = torch.minimum(boxes[i, 2:], rest[:, 2:])
            inter = (rb - lt).clamp(min=0).prod(1)
            area = lambda b: (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])  # noqa: E731
            order = order[1:][inter / (area(boxes[i]) + area(rest) - inter + 1e-9) <= iou]
        return torch.stack(keep) if keep else torch.empty(0, dtype=torch.long)


def detect(image):
    """PIL image -> list of (x0, y0, x1, y1, score) in image pixels, most confident first."""
    global _model
    if not available():
        raise RuntimeError("OmniParser isn't installed. Run WinControlMCP\\setup_omniparser.cmd once.")
    import numpy as np
    import torch
    from PIL import Image

    if _model is None:
        with warnings.catch_warnings():  # torch warns that TorchScript is deprecated on Python 3.14; it still works
            warnings.simplefilter("ignore", FutureWarning)
            _model = torch.jit.load(str(MODEL), map_location="cpu").eval()
    image = image.convert("RGB")
    width, height = image.size
    scale = min(IMGSZ / width, IMGSZ / height)
    canvas = Image.new("RGB", (IMGSZ, IMGSZ), (114, 114, 114))
    canvas.paste(image.resize((round(width * scale), round(height * scale)), Image.BILINEAR), (0, 0))
    x = torch.from_numpy(np.array(canvas)).permute(2, 0, 1).float()[None] / 255.0
    with torch.no_grad():
        outputs = _model(x)  # (cls_logits, box_ltrb) per stride
    boxes, scores = [], []
    for i, stride in enumerate(STRIDES):
        cls = outputs[2 * i].sigmoid()[0, 0]
        left, top, right, bottom = outputs[2 * i + 1][0]
        grid = cls.shape[-1]
        gy, gx = torch.meshgrid(torch.arange(grid, dtype=torch.float32),
                                torch.arange(grid, dtype=torch.float32), indexing="ij")
        cx, cy = gx + 0.5, gy + 0.5
        boxes.append(torch.stack([(cx - left) * stride, (cy - top) * stride,
                                  (cx + right) * stride, (cy + bottom) * stride], dim=-1).reshape(-1, 4))
        scores.append(cls.reshape(-1))
    boxes, scores = torch.cat(boxes), torch.cat(scores)
    keep = scores > CONF_THRESHOLD
    boxes, scores = boxes[keep], scores[keep]
    keep = _nms(boxes, scores, IOU_THRESHOLD)
    boxes, scores = boxes[keep] / scale, scores[keep]
    boxes[:, 0::2] = boxes[:, 0::2].clamp(0, width)
    boxes[:, 1::2] = boxes[:, 1::2].clamp(0, height)
    return [(*map(float, b), float(s)) for b, s in zip(boxes.tolist(), scores.tolist())
            if b[2] - b[0] >= 4 and b[3] - b[1] >= 4]  # drop slivers clamped to the image edge
