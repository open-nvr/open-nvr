# ros2-bridge example app

**OpenNVR's events on ROS 2 topics, for robots and fleet managers.**
Phase 1 of the ROS 2 bridge RFC
([discussion #985](https://github.com/open-nvr/open-nvr/discussions/985)):
OpenNVR perceives, records and decides; the robot's own stack (ROS 2,
Nav2) owns motion and safety. This phase is **read-only**: detections,
plates, visits, occupancy and alerts go out, and nothing comes back, so
nothing on a robot can be moved by it.

```
cameras ─► OpenNVR box ─► apps bus (NATS, never leaves the box)
                              │
                              ▼
                         ros2-bridge ──DDS──► robots, fleet manager, rviz
```

## Topics

| Topic | What | QoS |
|---|---|---|
| `/<ns>/<site>/<camera>/detection_observed_v1` | Tier-0 tracks, every published frame | best effort, depth 5 |
| `/<ns>/<site>/<camera>/plate_recognized_v1` | a plate read | reliable, depth 50 |
| `/<ns>/<site>/<camera>/visit_recorded_v1` | a visit, when it closes | reliable, depth 50 |
| `/<ns>/<site>/<camera>/occupancy_changed_v1` | a zone's count changed | reliable, depth 50 |
| `/<ns>/<site>/alerts` | every alert | reliable, depth 100 |

`<ns>` is `opennvr`, `<site>` your site's name and `<camera>` the camera,
as ROS 2 name tokens: `cam23`, never `23` (a token cannot start with a
digit), and `dock-east` becomes `dock_east`. The schema's version is in
the name, so a `v2` payload gets its own topic. Detections are high-rate
and stale a moment later, so a subscriber should ask for best effort too;
a reliable subscriber on a best-effort topic gets nothing.

**Messages** are `std_msgs/String` holding JSON, so the robot needs no
custom message package for phase 1:

```json
{"schema": "plate.recognized.v1", "id": "evt_3f9a", "camera_id": "cam23",
 "stamp": "2026-11-25T09:59:58Z", "stamp_source": "observed_at",
 "ts": "2026-11-25T10:00:01Z", "frame_id": "gate_camera",
 "producer": "kai-c", "correlation_id": "c1", "payload": {"plate_text": "..."}}
```

`stamp` is when it happened, and `stamp_source` says which time that is:
`observed_at` (the capture time plate and face reads carry), else `ts`
(when the event was published, which is all a detection carries), or an
alert's `fired_at`. Keep the box and the robots on NTP (or PTP) so the
two clocks agree. `frame_id` is the camera's frame on the robot's side
(`frame_ids` in config), default the camera's topic name.

Faces are not bridged by default (biometric). Add `face.recognized.v1`
to `events` deliberately, with its `events:face.recognized` scope.

## Reaching the robot

The bridge runs on the OpenNVR box, so the bus stays local; towards the
robots it is ordinary ROS 2 networking:

- **Host networking** (`--network host`): DDS discovery does not cross
  Docker's default bridge network.
- **`ROS_DOMAIN_ID`**: the same on the bridge and the robots.
- **Another subnet or Wi-Fi**: multicast discovery rarely gets there. Use
  a discovery server (`ROS_DISCOVERY_SERVER`) or `rmw_zenoh` with a Zenoh
  router.
- **SROS2** once the robots are on a network you do not control.

## Run

```bash
cd examples/ros2-bridge && uv sync --extra dev
uv run pytest -q                       # no ROS 2 needed
cp config.example.yml config.yml       # site, events, cameras
# On a box with no ROS 2, see what would be published:
#   dry_run: true  in config.yml, then
python ros2_bridge.py --config config.yml
```

With ROS 2, the image (built on `ros:jazzy-ros-base`):

```bash
docker build -f examples/ros2-bridge/Dockerfile -t opennvr/ros2-bridge:0.1.0 .
docker run --rm --network host -e ROS_DOMAIN_ID=7 \
  -v $(pwd)/config.yml:/app/config.yml:ro opennvr/ros2-bridge:0.1.0
# on the robot:
ros2 topic echo /opennvr/dock_east/alerts
```

## What is not in phase 1

From the RFC, in order:

- **The robot in:** its position as a `robot.pose.v1` event (map
  coordinates indoors, GPS outdoors), its state, its cameras recorded.
  Apps run on a robot camera only while it is stopped at an inspection
  point: motion gates, masks, zones and lines assume a fixed view.
- **Intents** (`go_to`, `inspect`, `return_to_dock`) as app actions behind
  the workflow safety rule, with mission state, cancel and expiry.
- **`opennvr_msgs`** (typed messages, `Detection2DArray` with the track id
  in `Detection2D.id`) once the JSON shape has settled.
- **Video:** the robot sends its own cameras over RTSP, not as
  `sensor_msgs/Image`.

| | |
|---|---|
| Pattern | domain-event and alert subscriber → rclpy publisher |
| Files | `ros2_bridge.py` (app), `ros2_mapping.py` (names, QoS, bodies; no ROS), `ros2_publisher.py` (the only rclpy code) |
| Tests | 24, without ROS 2 |
