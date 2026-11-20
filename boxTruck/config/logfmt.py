import logging
import time


class UTCFormatter(logging.Formatter):
    """Log times in UTC. Django sets the process time zone to TIME_ZONE (Central), and the
    Dev console reads every service's log times as UTC."""

    converter = time.gmtime
