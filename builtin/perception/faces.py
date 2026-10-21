"""Faces with InsightFace's buffalo_l models on onnxruntime: SCRFD, ArcFace, 2d106.

The same steps as ``insightface.app.FaceAnalysis`` with the modules detection,
recognition and landmark_2d_106, written out here so the engine needs only
onnxruntime, OpenCV and NumPy (the insightface package pins the CPU build of
onnxruntime, which clashes with the GPU one).

For each face: its box and score, the five key points, the normalised ArcFace
embedding (512 numbers), and from the 106 landmarks the mouth gap (inner-lip
opening / mouth width, for who is talking) and a rough head yaw (-1 left .. 1
right; 0 facing the camera).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np

# Where ArcFace expects the five key points in its 112 x 112 crop.
ARCFACE_DST = np.array(
    [
        [38.2946, 51.6963],
        [73.5318, 51.5014],
        [56.0252, 71.7366],
        [41.5493, 92.3655],
        [70.7299, 92.2041],
    ],
    dtype=np.float32,
)
MODEL_FILES = {
    "detection": "det_10g.onnx",
    "recognition": "w600k_r50.onnx",
    "landmarks": "2d106det.onnx",
}


@dataclass
class Face:
    box: list[float]  # x0, y0, x1, y1 in pixels
    score: float
    kps: np.ndarray  # 5 x 2
    embedding: np.ndarray  # 512, unit length
    landmarks: np.ndarray | None = None  # 106 x 2

    @property
    def mouth(self) -> float | None:
        """Inner-lip gap over mouth width (landmarks 60/62 and 52/61), as tested."""
        lm = self.landmarks
        if lm is None:
            return None
        width = float(np.linalg.norm(lm[61] - lm[52])) + 1e-6
        return float(np.linalg.norm(lm[60] - lm[62]) / width)

    @property
    def yaw(self) -> float:
        """Nose offset from the middle of the eyes, in eye distances (about -1..1)."""
        left, right, nose = self.kps[0], self.kps[1], self.kps[2]
        eyes = float(np.linalg.norm(right - left)) + 1e-6
        return float((nose[0] - (left[0] + right[0]) / 2) / eyes)


def umeyama(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """2 x 3 similarity transform (rotation, scale, shift) taking src onto dst."""
    n = src.shape[0]
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    src_c, dst_c = src - src_mean, dst - dst_mean
    cov = dst_c.T @ src_c / n
    d = np.ones(2)
    if np.linalg.det(cov) < 0:
        d[1] = -1
    u, s, vt = np.linalg.svd(cov)
    rank = np.linalg.matrix_rank(cov)
    if rank == 0:
        return np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float64)
    if rank == 1:
        if np.linalg.det(u) * np.linalg.det(vt) > 0:
            r = u @ vt
        else:
            s1 = d[1]
            d[1] = -1
            r = u @ np.diag(d) @ vt
            d[1] = s1
    else:
        r = u @ np.diag(d) @ vt
    var = src_c.var(axis=0).sum()
    scale = (s * d).sum() / var
    t = dst_mean - scale * (r @ src_mean)
    m = np.zeros((2, 3))
    m[:, :2] = scale * r
    m[:, 2] = t
    return m


def norm_crop(img: np.ndarray, kps: np.ndarray, size: int = 112) -> np.ndarray:
    m = umeyama(kps.astype(np.float64), ARCFACE_DST.astype(np.float64))
    return cv2.warpAffine(img, m, (size, size), borderValue=0.0)


def _nms(dets: np.ndarray, threshold: float) -> list[int]:
    x1, y1, x2, y2, scores = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
    areas = (x2 - x1 + 1) * (y2 - y1 + 1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = int(order[0])
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1 + 1)
        h = np.maximum(0.0, yy2 - yy1 + 1)
        inter = w * h
        ovr = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[np.where(ovr <= threshold)[0] + 1]
    return keep


class FaceAnalyzer:
    """Detect faces in a BGR image and describe each one."""

    def __init__(
        self,
        folder: Path,
        providers: list[str],
        *,
        det_size: int = 640,
        det_thresh: float = 0.5,
        nms_thresh: float = 0.4,
    ) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.log_severity_level = 3

        def session(name: str) -> Any:
            return ort.InferenceSession(
                str(folder / MODEL_FILES[name]), options, providers=providers
            )

        self.det = session("detection")
        self.rec = session("recognition")
        self.lmk = session("landmarks")
        self.providers = self.det.get_providers()
        self.det_size = det_size
        self.det_thresh = det_thresh
        self.nms_thresh = nms_thresh
        self._det_out = [o.name for o in self.det.get_outputs()]
        self._det_in = self.det.get_inputs()[0].name
        self._batched = len(self.det.get_outputs()[0].shape) == 3
        self._anchors: dict[tuple[int, int, int], np.ndarray] = {}

    # -- detection (SCRFD) ---------------------------------------------------

    def detect(self, img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        size = self.det_size
        ratio = img.shape[0] / img.shape[1]
        if ratio > 1.0:
            new_h, new_w = size, int(size / ratio)
        else:
            new_w, new_h = size, int(size * ratio)
        scale = new_h / img.shape[0]
        canvas = np.zeros((size, size, 3), dtype=np.uint8)
        canvas[:new_h, :new_w] = cv2.resize(img, (new_w, new_h))
        blob = cv2.dnn.blobFromImage(
            canvas, 1.0 / 128.0, (size, size), (127.5, 127.5, 127.5), swapRB=True
        )
        outs = self.det.run(self._det_out, {self._det_in: blob})
        scores_all, boxes_all, kps_all = [], [], []
        for idx, stride in enumerate((8, 16, 32)):
            scores = outs[idx][0] if self._batched else outs[idx]
            box_pred = (outs[idx + 3][0] if self._batched else outs[idx + 3]) * stride
            kps_pred = (outs[idx + 6][0] if self._batched else outs[idx + 6]) * stride
            h, w = size // stride, size // stride
            key = (h, w, stride)
            centers = self._anchors.get(key)
            if centers is None:
                grid = np.stack(np.mgrid[:h, :w][::-1], axis=-1).astype(np.float32)
                centers = (grid * stride).reshape(-1, 2)
                centers = np.stack([centers] * 2, axis=1).reshape(-1, 2)
                self._anchors[key] = centers
            keep = np.where(scores >= self.det_thresh)[0]
            boxes = np.stack(
                [
                    centers[:, 0] - box_pred[:, 0],
                    centers[:, 1] - box_pred[:, 1],
                    centers[:, 0] + box_pred[:, 2],
                    centers[:, 1] + box_pred[:, 3],
                ],
                axis=-1,
            )
            kps = np.stack(
                [
                    np.stack(
                        [centers[:, 0] + kps_pred[:, i], centers[:, 1] + kps_pred[:, i + 1]], -1
                    )
                    for i in range(0, 10, 2)
                ],
                axis=1,
            )
            scores_all.append(scores[keep])
            boxes_all.append(boxes[keep])
            kps_all.append(kps[keep])
        scores = np.vstack(scores_all).ravel()
        if scores.size == 0:
            return np.zeros((0, 5), np.float32), np.zeros((0, 5, 2), np.float32)
        order = scores.argsort()[::-1]
        boxes = np.vstack(boxes_all) / scale
        kps = np.vstack(kps_all) / scale
        dets = np.hstack((boxes, scores[:, None])).astype(np.float32)[order]
        kps = kps[order]
        keep = _nms(dets, self.nms_thresh)
        return dets[keep], kps[keep]

    # -- recognition (ArcFace) -------------------------------------------------

    def embed(self, img: np.ndarray, kps: np.ndarray) -> np.ndarray:
        crop = norm_crop(img, kps)
        blob = cv2.dnn.blobFromImages([crop], 1.0 / 127.5, (112, 112), (127.5,) * 3, swapRB=True)
        out = self.rec.run(None, {self.rec.get_inputs()[0].name: blob})[0][0]
        return (out / (np.linalg.norm(out) + 1e-12)).astype(np.float32)

    # -- 106 landmarks -----------------------------------------------------------

    def landmarks(self, img: np.ndarray, box: np.ndarray) -> np.ndarray:
        w, h = box[2] - box[0], box[3] - box[1]
        cx, cy = (box[2] + box[0]) / 2, (box[3] + box[1]) / 2
        out_size = 192
        s = out_size / (max(w, h) * 1.5)
        m = np.array([[s, 0, out_size / 2 - s * cx], [0, s, out_size / 2 - s * cy]], np.float64)
        crop = cv2.warpAffine(img, m, (out_size, out_size), borderValue=0.0)
        blob = cv2.dnn.blobFromImage(crop, 1.0, (out_size, out_size), (0.0, 0.0, 0.0), swapRB=True)
        pred = self.lmk.run(None, {self.lmk.get_inputs()[0].name: blob})[0][0]
        pred = pred.reshape(-1, 2)[-106:].astype(np.float64)
        pred = (pred + 1) * (out_size // 2)
        inv = cv2.invertAffineTransform(m)
        return (pred @ inv[:, :2].T + inv[:, 2]).astype(np.float32)

    def analyze(
        self, img: np.ndarray, *, landmarks: bool = True, max_faces: int = 16
    ) -> list[Face]:
        dets, kpss = self.detect(img)
        faces = []
        for det, kps in list(zip(dets, kpss, strict=True))[:max_faces]:
            faces.append(
                Face(
                    box=[float(v) for v in det[:4]],
                    score=float(det[4]),
                    kps=kps,
                    embedding=self.embed(img, kps),
                    landmarks=self.landmarks(img, det[:4]) if landmarks else None,
                )
            )
        return faces
