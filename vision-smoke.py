#!/usr/bin/env python3
"""Smoke test: Pi Camera Module 3 (IMX708) -> Hailo-8 YOLOv8s, person + dog only.

Captures one frame via GStreamer (libcamerasrc), runs YOLOv8s on Hailo,
decodes the (80, 5, 100) NMS output by hand, prints person/dog detections,
then loops for a few seconds to confirm sustained FPS.

Run on the Pi:  python3 vision-smoke.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst  # noqa: E402

from hailo_platform import HEF, VDevice  # noqa: E402

HEF_PATH    = "/usr/share/hailo-models/yolov8s_h8.hef"
INFERENCE_W = INFERENCE_H = 640
CAM_W, CAM_H, CAM_FPS = 1280, 720, 30

# COCO classes we care about
PERSON, DOG = 0, 16
WANTED = {PERSON: "person", DOG: "dog"}
CONF_THRESHOLD = 0.45

GST_PIPELINE = (
    f"libcamerasrc ! "
    f"video/x-raw,width={CAM_W},height={CAM_H},format=RGB,framerate={CAM_FPS}/1 ! "
    f"appsink name=sink sync=false max-buffers=1 drop=true"
)


class Camera:
    def __init__(self) -> None:
        Gst.init(None)
        self.pipe = Gst.parse_launch(GST_PIPELINE)
        self.sink = self.pipe.get_by_name("sink")
        self.pipe.set_state(Gst.State.PLAYING)
        # warm-up: discard a couple of frames so exposure settles
        for _ in range(5):
            self.read()

    def read(self) -> np.ndarray:
        sample = self.sink.emit("pull-sample")
        if sample is None:
            raise RuntimeError("camera: no sample (pipeline stalled?)")
        buf = sample.get_buffer()
        caps = sample.get_caps()
        w = caps.get_structure(0).get_value("width")
        h = caps.get_structure(0).get_value("height")
        ok, info = buf.map(Gst.MapFlags.READ)
        if not ok:
            raise RuntimeError("camera: buffer map failed")
        try:
            arr = np.frombuffer(info.data, dtype=np.uint8).reshape(h, w, 3)
            return arr.copy()
        finally:
            buf.unmap(info)

    def close(self) -> None:
        self.pipe.set_state(Gst.State.NULL)


def letterbox(frame: np.ndarray, size: int) -> tuple[np.ndarray, float, int, int]:
    """Resize+pad to (size,size,3) uint8, return (image, scale, pad_x, pad_y)."""
    h, w = frame.shape[:2]
    scale = size / max(h, w)
    nh, nw = int(round(h * scale)), int(round(w * scale))
    resized = _resize(frame, nw, nh)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    pad_y = (size - nh) // 2
    pad_x = (size - nw) // 2
    canvas[pad_y:pad_y + nh, pad_x:pad_x + nw] = resized
    return canvas, scale, pad_x, pad_y


def _resize(frame: np.ndarray, w: int, h: int) -> np.ndarray:
    import cv2
    return cv2.resize(frame, (w, h), interpolation=cv2.INTER_LINEAR)


def decode_yolov8_nms(out: np.ndarray, scale: float, pad_x: int, pad_y: int,
                      frame_w: int, frame_h: int, conf: float):
    """yolov8 NMS postprocess: shape (num_classes, 5, max_dets) -> list of boxes.

    Layout per class row [x_center, y_center, w, h, confidence], in *input*
    (640x640) coords. Convert back to original-frame coords.
    """
    boxes = []
    for cls in WANTED:
        if cls >= out.shape[0]:
            continue
        row = out[cls]  # (5, max_dets)
        scores = row[4]  # (max_dets,)
        for det in range(row.shape[1]):
            s = float(scores[det])
            if s < conf:
                continue
            cx, cy, bw, bh = (float(v) for v in row[:4, det])
            # back to original frame
            cx = (cx - pad_x) / scale
            cy = (cy - pad_y) / scale
            bw = bw / scale
            bh = bh / scale
            x1 = max(0.0, cx - bw / 2); y1 = max(0.0, cy - bh / 2)
            x2 = min(float(frame_w), cx + bw / 2); y2 = min(float(frame_h), cy + bh / 2)
            boxes.append({"cls": WANTED[cls], "conf": round(s, 3),
                          "x1": int(x1), "y1": int(y1),
                          "x2": int(x2), "y2": int(y2)})
    return boxes


def main() -> int:
    print("[1/3] opening camera...")
    cam = Camera()
    print("[2/3] loading Hailo HEF and creating inference vdevice...")
    hef = HEF(HEF_PATH)
    vdev = VDevice()
    ng, _ = hef.get_network_group_names()[0], None
    network_group = vdev.configure(hef)
    input_vstreams  = network_group.get_input_vstreams()
    output_vstreams = network_group.get_output_vstreams()

    # warm up
    frame = cam.read()
    print(f"      first frame: {frame.shape} dtype={frame.dtype}")
    img, scale, pad_x, pad_y = letterbox(frame, INFERENCE_W)
    with network_group.activate():
        # warm-up inference
        for _ in range(2):
            input_vstreams[0].send(img)
            out = output_vstreams[0].recv()
        out = np.asarray(out)
        boxes = decode_yolov8_nms(out, scale, pad_x, pad_y, CAM_W, CAM_H, CONF_THRESHOLD)
        print(f"[3/3] single frame: {len(boxes)} detections "
              f"({sum(1 for b in boxes if b['cls']=='person')} person, "
              f"{sum(1 for b in boxes if b['cls']=='dog')} dog)")
        for b in boxes:
            print(f"      {b['cls']:<6} conf={b['conf']:.2f} "
                  f"({b['x1']},{b['y1']}) -> ({b['x2']},{b['y2']})")

        # sustained loop
        print("\n[loop] running 5s at full camera FPS, reporting detections + fps...")
        t0 = time.monotonic()
        frames = 0
        inf_us_total = 0
        while time.monotonic() - t0 < 5.0:
            frame = cam.read()
            img, scale, pad_x, pad_y = letterbox(frame, INFERENCE_W)
            ti = time.monotonic()
            input_vstreams[0].send(img)
            out = output_vstreams[0].recv()
            inf_us_total += (time.monotonic() - ti)
            out = np.asarray(out)
            boxes = decode_yolov8_nms(out, scale, pad_x, pad_y, CAM_W, CAM_H, CONF_THRESHOLD)
            frames += 1
            p = sum(1 for b in boxes if b["cls"] == "person")
            d = sum(1 for b in boxes if b["cls"] == "dog")
            print(f"      t={time.monotonic()-t0:4.2f}s frame={frames:3d} "
                  f"inf={(inf_us_total/frames)*1000:.1f}ms  person={p} dog={d}")
        elapsed = time.monotonic() - t0
        print(f"\n[done] {frames} frames in {elapsed:.2f}s "
              f"= {frames/elapsed:.1f} fps overall "
              f"({(inf_us_total/frames)*1000:.1f} ms/inf)")

    cam.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())