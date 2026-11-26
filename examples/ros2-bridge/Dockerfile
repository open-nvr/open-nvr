# syntax=docker/dockerfile:1.7
# ros2-bridge example app
#
# Build (from the open-nvr repo root — the context must include
# sdk/opennvr-app-sdk, which this app depends on):
#   docker build -f examples/ros2-bridge/Dockerfile -t opennvr/ros2-bridge:0.1.0 .
#
# Run on the OpenNVR box with host networking, so DDS discovery reaches
# the robots (README: "Reaching the robot"):
#   docker run --rm --network host \
#     -e ROS_DOMAIN_ID=7 \
#     -v $(pwd)/config.yml:/app/config.yml:ro \
#     opennvr/ros2-bridge:0.1.0

# ROS 2 Jazzy (LTS, Ubuntu 24.04, Python 3.12): rclpy and std_msgs come
# from here, not from PyPI.
FROM ros:jazzy-ros-base AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3-pip \
        python-is-python3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# The SDK owns the bus loop and the app contract. Ubuntu 24.04 marks its
# Python as externally managed; this image is the app's alone.
COPY sdk/opennvr-app-sdk /opt/opennvr-app-sdk
RUN pip install --no-cache-dir --break-system-packages /opt/opennvr-app-sdk

COPY examples/ros2-bridge/ros2_bridge.py        ./ros2_bridge.py
COPY examples/ros2-bridge/ros2_mapping.py       ./ros2_mapping.py
COPY examples/ros2-bridge/ros2_publisher.py     ./ros2_publisher.py
COPY examples/ros2-bridge/config.example.yml    ./config.example.yml

ENV PYTHONUNBUFFERED=1

# ros_entrypoint.sh sources the ROS 2 environment, then runs the command.
ENTRYPOINT ["/ros_entrypoint.sh", "python", "ros2_bridge.py"]
CMD ["--config", "config.yml"]
