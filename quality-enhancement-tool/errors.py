# errors.py
from flask import jsonify

class AppError(Exception):
    """
    Custom exception class for handling application-specific errors.
    Contains a message, a status code, and an optional payload.
    """
    def __init__(self, description, status_code=500, payload=None):
        super().__init__(description)
        self.description = description
        self.status_code = status_code
        self.payload = payload

    def to_dict(self):
        """Serializes the error to a dictionary for JSON responses."""
        rv = dict(self.payload or ())
        rv['error'] = self.description
        return rv

class FFmpegError(AppError):
    """Specific error for failures related to FFmpeg commands."""
    def __init__(self, description, status_code=500):
        super().__init__(description, status_code)

class TTSError(AppError):
    """Specific error for failures related to the TTS service."""
    def __init__(self, description, status_code=502): # 502 Bad Gateway is appropriate here
        super().__init__(description, status_code)