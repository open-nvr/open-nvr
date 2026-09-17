# Fake-camera clips

Drop video files here and each one becomes a looping RTSP camera — see
`docs/FAKE_CAMERAS.md` for the full guide.

`.mp4 .m4v .mkv .mov .avi .ts .webm` are picked up. Two layouts:

```
.
├── gate-entry.mp4        one file   -> camera "gate-entry"
└── parking/              one folder -> camera "parking", playing every
    ├── 01-morning.mp4                 clip in it back to back, forever
    ├── 02-noon.mp4
    └── 03-night.mp4
```

The file name (loose file) or folder name (folder) becomes the camera name, so
name them the way you want the cameras named. Clips inside a folder play in
sorted filename order — prefix them when the order matters.

This folder is rescanned every few seconds while the rig is running: anything
you add here shows up as a stream on its own, and anything you delete goes
away. No restart needed.

Video files in this folder are ignored by git (the repo's `.gitignore`
already covers `*.mp4`).
