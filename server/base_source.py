from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple
import numpy as np

class BaseVideoSource(ABC):
    """
    Abstract Base Class for video capture sources in the PHF streaming pipeline.
    Subclasses can implement Webcam capture, Desktop/Screen capture, File video capture, etc.
    """

    @abstractmethod
    def list_devices(self) -> List[Dict[str, Any]]:
        """
        Enumerates available video input devices.
        Returns a list of dicts: [{"id": 0, "name": "Camera 0", "resolutions": [(1280, 720), ...]}]
        """
        pass

    @abstractmethod
    def start(self, device_id: Any, width: int = 1280, height: int = 720, fps: int = 30) -> bool:
        """
        Starts capture on the given device ID with the requested parameters.
        Returns True on success, False otherwise.
        """
        pass

    @abstractmethod
    def read_frame(self) -> Optional[Tuple[float, np.ndarray]]:
        """
        Reads the latest captured frame.
        Returns (timestamp_seconds, rgb_uint8_array_HxWx3), or None if no frame is available.
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        Stops the capture source and releases all hardware resources.
        """
        pass

    @abstractmethod
    def get_info(self) -> Dict[str, Any]:
        """
        Returns runtime status information about the active stream.
        """
        pass
