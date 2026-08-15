# Test fixtures

## `face_astronaut.jpg`

A real photograph of a human face, used so the face-detection tests are not
validated exclusively against drawings.

- **Subject**: Eileen Collins, astronaut.
- **Source**: NASA Great Images database, via
  [scikit-image](https://github.com/scikit-image/scikit-image)'s
  `skimage/data/astronaut.png` (tag `v0.22.0`).
- **Licence**: no known copyright restrictions; released into the public
  domain. NASA imagery is not subject to copyright.
- **Provenance chain**: the upstream PNG has
  sha256 `88431cd9653ccd539741b555fb0a46b61558b301d4110412b5bc28b5e3ea6cb5`,
  which matches scikit-image's own `_registry.py`. This file is that image
  cropped to the head with margin, resized to 256x256 and saved as JPEG q92.

## Why both real and rendered faces

`faces.py` builds video fixtures from two kinds of face: this photograph, and
faces drawn with OpenCV primitives. Both are needed and neither is sufficient.

The photograph is the ground truth that the detector works on a **real human
face** — a rendered face proves the pipeline runs, not that it would ever fire
on a podcast. The drawn faces are what make the *behavioural* tests possible:
they can be placed at an exact pixel, moved along a known path, occluded on a
known schedule, and given a mouth that opens and closes on cue. A stock video
of two people talking has none of that, so every assertion against it would
have to be hand-labelled and approximate.

So the split is deliberate: the photograph answers "does this detect a face",
and the constructed videos answer "does the tracker do the right thing when a
face moves, is hidden, arrives, or leaves".

What neither proves is detection rates on real-world footage — lighting,
motion blur, profile views, small faces in a wide shot. That needs a labelled
dataset and is stated as a limitation in the README rather than implied by a
green suite.
