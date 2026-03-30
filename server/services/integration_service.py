# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from models import Integration

logger = logging.getLogger(__name__)


class IntegrationService:
    @staticmethod
    async def test_integration(integration: Integration) -> dict:
        """
        Test the integration connection based on its type.
        Returns a dict with success=True/False and a message.
        """
        try:
            if integration.type == "email":
                return await IntegrationService._test_email(integration.config)
            elif integration.type in ["webhook", "slack", "teams"]:
                return await IntegrationService._test_webhook(integration)
            else:
                # For other types, we just acknowledge for now
                return {
                    "success": True,
                    "message": f"Simulation: Test event sent to {integration.type} integration.",
                }
        except Exception as e:
            logger.error(f"Integration test failed: {e!s}")
            return {"success": False, "message": f"Test failed: {e!s}"}

    @staticmethod
    async def _test_email(config: dict) -> dict:
        smtp_host = config.get("smtp_host")
        smtp_port = config.get("smtp_port", 587)
        username = config.get("username")
        password = config.get("password")
        use_tls = config.get("use_tls", True)
        from_addr = config.get("from_addr")
        to_addrs = config.get("to_addrs")

        if not all([smtp_host, from_addr, to_addrs]):
            return {
                "success": False,
                "message": "Missing SMTP configuration (host, from, or to)",
            }

        msg = MIMEMultipart()
        msg["From"] = from_addr
        msg["To"] = to_addrs
        msg["Subject"] = "OpenNVR - Integration Test"
        body = "This is a test email from your OpenNVR system. If you received this, your email integration is working correctly."
        msg.attach(MIMEText(body, "plain"))

        try:
            # We use a synchronous SMTP call here. In a high-throughput scenario,
            # this should be offloaded to a thread pool or use an async SMTP lib (aiosmtplib).
            # For a "Test" button, sync is acceptable if not blocking main loop for too long,
            # but ideally we run it in a thread.
            import asyncio

            def send_sync():
                with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                    if use_tls:
                        server.starttls()
                    if username and password:
                        server.login(username, password)
                    server.send_message(msg)

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, send_sync)

            return {"success": True, "message": f"Test email sent to {to_addrs}"}

        except smtplib.SMTPAuthenticationError:
            return {
                "success": False,
                "message": "SMTP Authentication failed. Check username/password.",
            }
        except smtplib.SMTPConnectError:
            return {"success": False, "message": "Could not connect to SMTP server."}
        except Exception as e:
            return {"success": False, "message": f"SMTP Error: {e!s}"}

    @staticmethod
    async def _test_webhook(integration: Integration) -> dict:
        config = integration.config
        url = ""

        if integration.type == "webhook":
            url = config.get("url")
        elif integration.type in ["slack", "teams"]:
            url = config.get("webhook_url")

        if not url:
            return {"success": False, "message": "Missing Webhook URL"}

        payload = {
            "event": "integration.test",
            "message": "This is a test event from OpenNVR.",
            "timestamp": "2024-01-01T00:00:00Z",
        }

        # Slack/Teams specific formatting could go here, but raw JSON usually works or gives a 400
        if integration.type == "slack" or integration.type == "teams":
            payload = {"text": "🔔 OpenNVR: Integration Test Successful!"}

        async with httpx.AsyncClient() as client:
            try:
                # Some webhooks need headers
                headers = {}
                if integration.type == "webhook" and config.get("secret"):
                    headers["X-Auth-Token"] = config.get("secret")

                resp = await client.post(
                    url, json=payload, headers=headers, timeout=10.0
                )
                resp.raise_for_status()
                return {
                    "success": True,
                    "message": f"Webhook delivered successfully (HTTP {resp.status_code})",
                }
            except httpx.HTTPStatusError as e:
                return {
                    "success": False,
                    "message": f"Webhook endpoint returned error: {e.response.status_code}",
                }
            except httpx.RequestError as e:
                return {
                    "success": False,
                    "message": f"Webhook connection failed: {e!s}",
                }
