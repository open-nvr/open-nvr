# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Publishing to ROS 2: the only module that touches rclpy.

``rclpy`` comes from the ROS 2 install (the image is built on
``ros:jazzy-ros-base``), not from PyPI, so it is imported when the bridge
starts publishing, never at import time: the rest of the app and its
tests run without ROS.

One node, ``opennvr_bridge``; one publisher per topic, made the first
time the topic is used. Publishing needs no executor spin: nothing is
subscribed on this side (phase 1 is read-only).
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from ros2_mapping import Qos

logger = logging.getLogger("ros2-bridge")


class Publisher(Protocol):
    def publish(self, topic: str, qos: Qos, text: str) -> None: ...
    def close(self) -> None: ...


class Ros2Publisher:
    """``std_msgs/String`` on each topic, through rclpy."""

    def __init__(self, node_name: str = "opennvr_bridge") -> None:
        import rclpy
        from rclpy.qos import HistoryPolicy, QoSProfile, ReliabilityPolicy
        from std_msgs.msg import String

        self._rclpy = rclpy
        self._String = String
        self._profile = lambda q: QoSProfile(
            history=HistoryPolicy.KEEP_LAST, depth=q.depth,
            reliability=(ReliabilityPolicy.BEST_EFFORT if q.reliability == "best_effort"
                         else ReliabilityPolicy.RELIABLE))
        if not rclpy.ok():
            rclpy.init()
        self._node = rclpy.create_node(node_name)
        self._pubs: dict[str, Any] = {}
        logger.info("ROS 2 node %s up", node_name)

    def publish(self, topic: str, qos: Qos, text: str) -> None:
        pub = self._pubs.get(topic)
        if pub is None:
            pub = self._pubs[topic] = self._node.create_publisher(
                self._String, topic, self._profile(qos))
            logger.info("publishing on %s (%s, depth %d)", topic, qos.reliability, qos.depth)
        msg = self._String()
        msg.data = text
        pub.publish(msg)

    def close(self) -> None:
        self._node.destroy_node()
        if self._rclpy.ok():
            self._rclpy.shutdown()


class LogPublisher:
    """``dry_run``: what would be published, to the log. For trying the
    bridge on a box with no ROS 2."""

    def __init__(self) -> None:
        self.count = 0

    def publish(self, topic: str, qos: Qos, text: str) -> None:
        self.count += 1
        logger.info("dry run: %s <- %s", topic, text[:200])

    def close(self) -> None:
        pass
