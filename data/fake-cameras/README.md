# Fake-camera clips

Drop video files here. Each one becomes its own looping RTSP camera —
see the README.md at the root of this bundle.

`.mp4 .m4v .mkv .mov .avi .ts .webm` are picked up, including one level
of subfolders. The file name becomes the stream name, so name your files
the way you want the cameras named.

Video files in this folder are ignored by git (the repo's `.gitignore`
already covers `*.mp4`).
