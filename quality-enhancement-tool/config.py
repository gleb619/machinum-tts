# config.py
import os
import tempfile

class Config:
    """Flask application configuration class."""
    # Set a secret key for session management and other security features
    SECRET_KEY = os.environ.get('SECRET_KEY', 'a_default_secret_key_for_development')

    # Configure max content length for uploads (500MB)
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 500 * 1024 * 1024))

    # Use system's temporary directory for uploads
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", tempfile.gettempdir())

    # URL for the external Text-to-Speech service
    TTS_SERVER_URL = os.getenv("SERVER_URL", "http://localhost:5001/api/tts")

# Audio processing presets configuration
PRESETS = {
    'tts_basic': {
        'bitrate': '64k',
        'sample_rate': '22050',
        'channels': '1',
        'loudnorm': {'I': -18, 'LRA': 6, 'TP': -2.0},
        'description': 'Minimal voice preset for long TTS content, smallest size.'
    },
    'tts_balanced': {
        'bitrate': '96k',
        'sample_rate': '32000',
        'channels': '1',
        'loudnorm': {'I': -16, 'LRA': 8, 'TP': -1.5},
        'description': 'Balanced preset for speech: good quality, compact size.'
    },
    'tts_hi_res': {
        'bitrate': '128k',
        'sample_rate': '44100',
        'channels': '1',
        'loudnorm': {'I': -16, 'LRA': 10, 'TP': -1.0},
        'description': 'High-res voice with controlled size for narrations and audiobooks.'
    },
    'podcast': {
        'bitrate': '128k',
        'sample_rate': '44100',
        'channels': '1', # Mono for voice
        'loudnorm': {'I': -16, 'LRA': 11, 'TP': -1.5},
        'description': 'Optimized for spoken word and podcasts.'
    },
    'music': {
        'bitrate': '256k',
        'sample_rate': '44100',
        'channels': '2', # Stereo for music
        'loudnorm': {'I': -14, 'LRA': 7, 'TP': -1.0},
        'description': 'High-quality with a wide dynamic range for music.'
    },
    'high_quality': {
        'bitrate': '320k',
        'sample_rate': '44100',
        'channels': '2', # Stereo for music
        'loudnorm': {'I': -16, 'LRA': 11, 'TP': -1.5},
        'description': 'Maximum quality preset for archival purposes.'
    }
}