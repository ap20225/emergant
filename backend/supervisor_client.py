"""
Supervisor client for managing the openclaw gateway process.

This module provides a clean interface for starting, stopping, and
checking the status of the gateway process managed by supervisord.
"""

import subprocess
import logging

logger = logging.getLogger(__name__)


class SupervisorClient:
    """Client for interacting with supervisord to manage the gateway process."""

    PROGRAM = "openclaw-gateway"

    @classmethod
    def start(cls) -> bool:
        """
        Start the gateway via supervisor.

        Returns:
            True if the start command succeeded, False otherwise.
        """
        try:
            result = subprocess.run(
                ['supervisorctl', 'start', cls.PROGRAM],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                logger.info(f"Started {cls.PROGRAM} via supervisor")
                return True
            else:
                # supervisorctl reports errors on stdout, not stderr
                error_msg = result.stdout.strip() or result.stderr.strip() or "(no output)"
                logger.error(f"Failed to start {cls.PROGRAM}: {error_msg}")
                return False
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout starting {cls.PROGRAM}")
            return False
        except Exception as e:
            logger.error(f"Error starting {cls.PROGRAM}: {e}")
            return False

    @classmethod
    def stop(cls) -> bool:
        """
        Stop the gateway via supervisor.

        Returns:
            True if the stop command succeeded, False otherwise.
        """
        try:
            result = subprocess.run(
                ['supervisorctl', 'stop', cls.PROGRAM],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0 or 'NOT RUNNING' in result.stdout:
                logger.info(f"Stopped {cls.PROGRAM} via supervisor")
                return True
            else:
                logger.error(f"Failed to stop {cls.PROGRAM}: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout stopping {cls.PROGRAM}")
            return False
        except Exception as e:
            logger.error(f"Error stopping {cls.PROGRAM}: {e}")
            return False

    @classmethod
    def status(cls) -> bool:
        """
        Check if the gateway is running via supervisor.

        Returns:
            True if the process is running (RUNNING state), False otherwise.
        """
        try:
            result = subprocess.run(
                ['supervisorctl', 'status', cls.PROGRAM],
                capture_output=True,
                text=True,
                timeout=10
            )
            # Check for RUNNING state in output
            # Output format: "openclaw-gateway            RUNNING   pid 12345, uptime 0:01:23"
            return 'RUNNING' in result.stdout
        except Exception as e:
            logger.error(f"Error checking {cls.PROGRAM} status: {e}")
            return False

    @classmethod
    def get_pid(cls) -> int | None:
        """
        Get the PID of the running gateway process.

        Returns:
            The PID if running, None otherwise.
        """
        try:
            result = subprocess.run(
                ['supervisorctl', 'status', cls.PROGRAM],
                capture_output=True,
                text=True,
                timeout=10
            )
            # Parse PID from output like: "openclaw-gateway            RUNNING   pid 12345, uptime 0:01:23"
            if 'RUNNING' in result.stdout and 'pid' in result.stdout:
                # Extract pid number
                parts = result.stdout.split('pid')
                if len(parts) > 1:
                    pid_part = parts[1].strip().split(',')[0].strip()
                    return int(pid_part)
            return None
        except Exception as e:
            logger.error(f"Error getting {cls.PROGRAM} PID: {e}")
            return None

    @classmethod
    def restart(cls) -> bool:
        """
        Restart the gateway via supervisor.

        Returns:
            True if the restart command succeeded, False otherwise.
        """
        try:
            result = subprocess.run(
                ['supervisorctl', 'restart', cls.PROGRAM],
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                logger.info(f"Restarted {cls.PROGRAM} via supervisor")
                return True
            else:
                logger.error(f"Failed to restart {cls.PROGRAM}: {result.stderr}")
                return False
        except subprocess.TimeoutExpired:
            logger.error(f"Timeout restarting {cls.PROGRAM}")
            return False
        except Exception as e:
            logger.error(f"Error restarting {cls.PROGRAM}: {e}")
            return False

    @classmethod
    def reload_config(cls) -> bool:
        """
        Reload supervisor configuration.

        Call this after modifying supervisor config files.

        Returns:
            True if reload succeeded, False otherwise.
        """
        try:
            result = subprocess.run(
                ['supervisorctl', 'reread'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                logger.error(f"Failed to reread supervisor config: {result.stderr}")
                return False

            result = subprocess.run(
                ['supervisorctl', 'update'],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                logger.error(f"Failed to update supervisor: {result.stderr}")
                return False

            logger.info("Supervisor configuration reloaded")
            return True
        except Exception as e:
            logger.error(f"Error reloading supervisor config: {e}")
            return False
