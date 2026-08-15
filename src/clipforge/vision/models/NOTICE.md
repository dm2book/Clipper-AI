# Bundled model

## face_detection_yunet_2023mar.onnx

- **Model**: YuNet, a compact CNN face detector.
- **Source**: [opencv/opencv_zoo](https://github.com/opencv/opencv_zoo),
  `models/face_detection_yunet/face_detection_yunet_2023mar.onnx`.
- **Upstream project**: [ShiqiYu/libfacedetection](https://github.com/ShiqiYu/libfacedetection)
- **Licence**: MIT, © 2020 Shiqi Yu <shiqi.yu@gmail.com>.
- **sha256**: `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`

Committed rather than downloaded at runtime, for three reasons. It is 227 KB,
which is smaller than several source files here. A download on first use makes
the first render of a new deployment fail in a way that looks like a code bug.
And a model fetched at runtime is a model whose version nobody pinned — the
detector's behaviour would then change under a deployment that changed nothing,
which is the hardest class of regression to attribute.

`vision.config.resolve_model()` prefers `CLIPFORGE_FACE_MODEL` when set, so a
deployment wanting a different or newer model does not need this file replaced.
